"""Prepare bounded DART review candidates and optional untrusted import drafts.

This command is offline.  It never reads credentials, calls DART, approves a
policy, marks search complete, or turns a candidate into a reviewed fact.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from proofops.adapters.dart import ArtifactStore
from proofops.adapters.dart.candidates import (
    CandidatePreparationError,
    PreparedCandidates,
    add_operator_sr_sources,
    build_candidate_catalog,
)
from proofops.application.reconciliation.schema import validate_schema

from evaluation.reconciliation_cli import InputRejected, load_json, write_json

SELECTION_SCHEMA = "reconciliation-operator-selections-1"
IMPORT_DRAFT_SCHEMA = "reconciliation-import-draft-1"


def _candidate_map(prepared: PreparedCandidates) -> dict[str, dict[str, Any]]:
    return {candidate["candidate_id"]: candidate for candidate in prepared.catalog["candidates"]}


def _selected(
    choices: Mapping[str, Any], candidates: Mapping[str, dict[str, Any]], role: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    selection = choices.get(role)
    if not isinstance(selection, dict):
        raise CandidatePreparationError(f"missing_{role}_selection")
    required = {"candidate_id", "raw", "normalized", "kind", "unit"}
    if set(selection) != required:
        raise CandidatePreparationError(f"invalid_{role}_selection")
    candidate = candidates.get(selection["candidate_id"])
    if candidate is None:
        raise CandidatePreparationError(f"unknown_{role}_candidate")
    if role == "sustainability" and candidate["candidate_type"] != "sustainability_source":
        raise CandidatePreparationError("sustainability_requires_sr_source")
    if role == "financial" and candidate["candidate_type"] == "sustainability_source":
        raise CandidatePreparationError("financial_requires_dart_source")
    if (
        selection["raw"] is not None
        and not isinstance(selection["raw"], str)
        or selection["normalized"] is not None
        and not isinstance(selection["normalized"], str)
        or selection["kind"]
        not in {
            "entity_set",
            "facility_set",
            "currency_amount",
            "period",
            "classification",
            "unknown",
        }
        or selection["unit"] is not None
        and not isinstance(selection["unit"], str)
    ):
        raise CandidatePreparationError(f"invalid_{role}_fact")
    raw_values = {str(value) for value in candidate.get("raw", {}).values() if value is not None}
    if selection["raw"] is not None and (
        selection["raw"] not in candidate["source"]["quote"] and selection["raw"] not in raw_values
    ):
        raise CandidatePreparationError(f"{role}_raw_not_candidate_bound")
    fact = {
        "raw": selection["raw"],
        "normalized": selection["normalized"],
        "kind": selection["kind"],
        "unit": selection["unit"],
        "source_id": candidate["source"]["source_id"],
    }
    return candidate, fact


def _source_ref(candidate: Mapping[str, Any]) -> dict[str, Any]:
    source = candidate["source"]
    return {
        key: source[key]
        for key in ("source_id", "document_id", "artifact_sha256", "locator", "quote")
    }


def _source_for_candidate_id(
    value: Any, candidates: Mapping[str, dict[str, Any]], *, nullable: bool = False
) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or value not in candidates:
        raise CandidatePreparationError("unknown_context_candidate")
    return candidates[value]["source"]["source_id"]


def _context(
    item: str, choices: Mapping[str, Any], candidates: Mapping[str, dict[str, Any]]
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[tuple[str, str]]]:
    roles: list[tuple[str, str]] = []
    raw_c3, raw_c4 = choices.get("c3_context"), choices.get("c4_context")
    c3 = c4 = None
    if item == "C3":
        required = {
            "currency",
            "target_period_start",
            "target_period_end",
            "capex_period_start",
            "capex_period_end",
            "capex_account_ids",
            "commitment_candidate_id",
            "funding_plan_candidate_id",
        }
        if not isinstance(raw_c3, dict) or set(raw_c3) != required:
            raise CandidatePreparationError("missing_or_invalid_c3_choices")
        commitment = _source_for_candidate_id(
            raw_c3["commitment_candidate_id"], candidates, nullable=True
        )
        funding = _source_for_candidate_id(
            raw_c3["funding_plan_candidate_id"], candidates, nullable=True
        )
        c3 = {
            key: copy.deepcopy(raw_c3[key])
            for key in (
                "currency",
                "target_period_start",
                "target_period_end",
                "capex_period_start",
                "capex_period_end",
                "capex_account_ids",
            )
        } | {"commitment_source_id": commitment, "funding_plan_source_id": funding}
        if commitment:
            roles.append((commitment, "c3_commitment"))
        if funding:
            roles.append((funding, "c3_funding"))
    elif raw_c3 is not None:
        raise CandidatePreparationError("unexpected_c3_choices")
    if item == "C4":
        required = {"classification_name", "definition_candidate_ids", "calculation_candidate_ids"}
        if not isinstance(raw_c4, dict) or set(raw_c4) != required:
            raise CandidatePreparationError("missing_or_invalid_c4_choices")
        if not all(isinstance(raw_c4[key], list) for key in required - {"classification_name"}):
            raise CandidatePreparationError("invalid_c4_candidate_lists")
        definitions = [
            _source_for_candidate_id(value, candidates)
            for value in raw_c4["definition_candidate_ids"]
        ]
        calculations = [
            _source_for_candidate_id(value, candidates)
            for value in raw_c4["calculation_candidate_ids"]
        ]
        c4 = {
            "classification_name": raw_c4["classification_name"],
            "definition_source_ids": definitions,
            "calculation_source_ids": calculations,
        }
        roles.extend((value, "c4_definition") for value in definitions if value)
        roles.extend((value, "c4_calculation") for value in calculations if value)
    elif raw_c4 is not None:
        raise CandidatePreparationError("unexpected_c4_choices")
    return c3, c4, roles


def prepare_review_draft(
    prepared: PreparedCandidates, choices: Mapping[str, Any]
) -> dict[str, Any]:
    """Bind explicit operator choices into a pending, unapproved import draft."""
    if not isinstance(choices, Mapping) or choices.get("schema_version") != SELECTION_SCHEMA:
        raise CandidatePreparationError("selection_schema_invalid")
    required = {
        "schema_version",
        "identity",
        "item",
        "claim",
        "comparability",
        "sustainability",
        "financial",
        "explanation",
        "search",
        "c3_context",
        "c4_context",
        "policy",
    }
    missing = sorted(required - set(choices))
    if missing:
        return {
            "schema_version": IMPORT_DRAFT_SCHEMA,
            "status": "candidate_only",
            "missing_choices": missing,
            "review_state": "pending",
            "policy_approved": False,
        }
    if set(choices) != required:
        raise CandidatePreparationError("selection_fields_invalid")
    identity = copy.deepcopy(choices["identity"])
    item = choices["item"]
    if not isinstance(identity, dict) or item not in {"C1", "C2", "C3", "C4"}:
        raise CandidatePreparationError("selection_identity_or_item_invalid")
    expected = prepared.catalog["identity"]
    if (
        identity.get("dart_corp_code") != expected["corp_code"]
        or identity.get("financial_fiscal_year") != expected["fy"]
        or identity.get("rcept_no") != expected["rcept_no"]
        or identity.get("consolidation") != expected["consolidation"]
        or identity.get("package_id") != prepared.catalog["package_id"]
    ):
        raise CandidatePreparationError("selection_collection_identity_mismatch")

    candidates = _candidate_map(prepared)
    sustainability_candidate, sustainability = _selected(choices, candidates, "sustainability")
    financial_candidate, financial = _selected(choices, candidates, "financial")
    if (
        identity.get("sustainability_document_version")
        != sustainability_candidate["source"]["document_id"]
        or identity.get("financial_document_version")
        != financial_candidate["source"]["document_id"]
    ):
        raise CandidatePreparationError("selected_document_version_mismatch")
    claim_choice = choices["claim"]
    if not isinstance(claim_choice, dict) or set(claim_choice) != {
        "candidate_id",
        "track",
        "trigger_elements",
        "fiscal_year",
    }:
        raise CandidatePreparationError("claim_selection_invalid")
    claim_candidate = candidates.get(claim_choice["candidate_id"])
    if claim_candidate is None:
        raise CandidatePreparationError("claim_candidate_unknown")
    if (
        claim_candidate["candidate_type"] != "sustainability_source"
        or claim_candidate["source"]["document_id"] != identity["sustainability_document_version"]
    ):
        raise CandidatePreparationError("claim_requires_selected_sr_document")
    claim = {
        "track": claim_choice["track"],
        "quote": claim_candidate["source"]["quote"],
        "source_id": claim_candidate["source"]["source_id"],
        "trigger_elements": copy.deepcopy(claim_choice["trigger_elements"]),
        "fiscal_year": claim_choice["fiscal_year"],
    }
    c3, c4, context_roles = _context(item, choices, candidates)

    explanation_choice = choices["explanation"]
    if not isinstance(explanation_choice, dict) or set(explanation_choice) != {
        "candidate_id",
        "search_complete",
    }:
        raise CandidatePreparationError("explanation_selection_invalid")
    explanation_source = _source_for_candidate_id(
        explanation_choice["candidate_id"], candidates, nullable=True
    )
    # This adapter cannot issue or attest a coverage receipt.  Even an operator
    # proposal stays untrusted and cannot set the packet's completion flag.
    if explanation_choice["search_complete"] is not False:
        raise CandidatePreparationError("search_complete_requires_authenticated_review")
    search = copy.deepcopy(choices["search"])
    if not isinstance(search, dict) or search.get("state") == "complete":
        raise CandidatePreparationError("complete_search_receipt_not_preparable")
    search["state"] = (
        "not_run" if search.get("state") not in {"not_run", "incomplete"} else search["state"]
    )
    search["receipt_id"] = None
    search["reviewed_source_ids"] = []

    chosen = [claim_candidate, sustainability_candidate, financial_candidate]
    if explanation_choice["candidate_id"] is not None:
        chosen.append(candidates[explanation_choice["candidate_id"]])
    for raw_context in (choices.get("c3_context"), choices.get("c4_context")):
        if isinstance(raw_context, dict):
            for key, value in raw_context.items():
                if key.endswith("_candidate_id") and isinstance(value, str):
                    chosen.append(candidates[value])
                elif key.endswith("_candidate_ids") and isinstance(value, list):
                    chosen.extend(candidates[item_id] for item_id in value)
    unique_candidates = {candidate["candidate_id"]: candidate for candidate in chosen}
    sources = [_source_ref(value) for value in unique_candidates.values()]

    packet = {
        "schema_version": "1.1",
        "synthetic": prepared.catalog["synthetic"],
        "identity": identity,
        "item": item,
        "sources": sources,
        "sustainability": sustainability,
        "financial": financial,
        "comparability": choices["comparability"],
        "explanation": {"source_id": explanation_source, "search_complete": False},
        "c3_context": c3,
        "claim": claim,
        "search": search,
        "c4_context": c4,
    }
    policy = copy.deepcopy(choices["policy"])
    if not isinstance(policy, dict):
        raise CandidatePreparationError("policy_draft_invalid")
    if (
        policy.get("approved") is not False
        or policy.get("approved_by") is not None
        or policy.get("approved_on") is not None
    ):
        raise CandidatePreparationError("imported_policy_approval_forbidden")
    validate_schema("input", packet)
    validate_schema("policy", policy)

    role_map: dict[str, set[str]] = {}
    source_roles = [
        (claim["source_id"], "claim"),
        (sustainability["source_id"], "sustainability_fact"),
        (financial["source_id"], "financial_fact"),
        *context_roles,
    ]
    if explanation_source:
        source_roles.append((explanation_source, "explanation"))
    for source_id, role in source_roles:
        role_map.setdefault(source_id, set()).add(role)
    decision_binding = {
        "item": item,
        "comparability": choices["comparability"],
        "claim": claim,
        "c3_context": c3,
        "c4_context": c4,
        "claim_id": identity["claim_id"],
    }
    documents: dict[str, Any] = {}
    operator_documents = prepared.catalog.get("operator_documents", {})
    for document_id in sorted({source["document_id"] for source in sources}):
        document_sources = [source for source in sources if source["document_id"] == document_id]
        financial_role = document_id == identity["financial_document_version"]
        sustainability_role = document_id == identity["sustainability_document_version"]
        if financial_role == sustainability_role:
            raise CandidatePreparationError("document_role_identity_ambiguous")
        source_bindings = {
            source["source_id"]: {
                "locator": source["locator"],
                "quote": source["quote"],
                "roles": sorted(role_map.get(source["source_id"], set())),
            }
            for source in document_sources
        }
        facts = {}
        for fact in (sustainability, financial):
            if fact["source_id"] in source_bindings:
                facts[fact["source_id"]] = {
                    key: fact[key] for key in ("raw", "normalized", "kind", "unit")
                }
        generated_document = {
            "tenant_id": identity["tenant_id"],
            "company_id": identity["company_id"],
            "package_id": identity["package_id"],
            "document_version_id": document_id,
            "document_role": "financial" if financial_role else "sustainability",
            "artifact_sha256": document_sources[0]["artifact_sha256"],
            "corp_code": identity["dart_corp_code"],
            "fiscal_year": identity["financial_fiscal_year"],
            "rcept_no": identity["rcept_no"] if financial_role else None,
            "consolidation": identity["consolidation"],
            "published_at": identity[
                "financial_published_at" if financial_role else "sr_published_at"
            ],
            "available_on": identity[
                "financial_published_at" if financial_role else "sr_published_at"
            ],
            "as_of_date": identity["as_of_date"],
            "period_start": identity[
                "financial_period_start" if financial_role else "period_start"
            ],
            "period_end": identity["financial_period_end" if financial_role else "period_end"],
            "relevant_items": [item],
            "synthetic": prepared.catalog["synthetic"],
            "decision_binding": decision_binding,
            "source_bindings": source_bindings,
            "fact_bindings": facts,
            "binding_state": "draft",
        }
        if document_id in operator_documents:
            supplied = copy.deepcopy(operator_documents[document_id])
            expected_metadata = {
                key: generated_document[key]
                for key in (
                    "tenant_id",
                    "company_id",
                    "package_id",
                    "document_version_id",
                    "document_role",
                    "artifact_sha256",
                    "corp_code",
                    "fiscal_year",
                    "rcept_no",
                    "consolidation",
                    "published_at",
                    "available_on",
                    "as_of_date",
                    "period_start",
                    "period_end",
                    "synthetic",
                )
            }
            if (
                not isinstance(supplied, dict)
                or set(supplied) != set(expected_metadata)
                or any(supplied.get(key) != value for key, value in expected_metadata.items())
            ):
                raise CandidatePreparationError("sr_document_metadata_mismatch")
            generated_document = {
                **supplied,
                "relevant_items": [item],
                "decision_binding": decision_binding,
                "source_bindings": source_bindings,
                "fact_bindings": facts,
                "binding_state": "draft",
            }
        documents[document_id] = generated_document
    artifact_by_document = prepared.artifact_index
    selected_artifacts = {
        document_id: artifact_by_document[document_id]
        for document_id in documents
        if document_id in artifact_by_document
    }
    import_sources = [
        {
            **source,
            "format": artifact_by_document[source["document_id"]]["format"],
            "path": artifact_by_document[source["document_id"]]["path"],
            "binding_state": "draft",
        }
        for source in sources
    ]
    coverage_draft = {
        "state": search["state"],
        "coverage_policy_id": search.get("coverage_policy_id"),
        "required_document_ids": copy.deepcopy(search.get("required_document_ids", [])),
        "reviewed_source_ids": [],
        "failed_document_ids": copy.deepcopy(search.get("failed_document_ids", [])),
        "receipt_id": None,
        "binding_state": "draft",
    }
    return {
        "schema_version": IMPORT_DRAFT_SCHEMA,
        "status": "draft_pending_review",
        "review_state": "pending",
        "policy_approved": False,
        "packet": packet,
        "policy": policy,
        "sources": import_sources,
        "documents": documents,
        "artifacts": selected_artifacts,
        "coverage": coverage_draft,
    }


def _write_artifacts(output: Path, prepared: PreparedCandidates) -> None:
    root = output / "artifacts"
    for artifact in prepared.artifacts:
        destination = root / artifact.path
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as stream:
            stream.write(artifact.payload)
            stream.flush()
            os.fsync(stream.fileno())


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--manifest", required=True, type=Path)
    cli.add_argument("--store", required=True, type=Path)
    cli.add_argument("--output", required=True, type=Path)
    cli.add_argument("--corp-code", required=True)
    cli.add_argument("--fy", required=True, type=int)
    cli.add_argument("--rcept-no", required=True)
    cli.add_argument("--consolidation", required=True, choices=["consolidated", "separate"])
    cli.add_argument("--max-candidates", type=int, default=500)
    cli.add_argument("--selections", type=Path)
    cli.add_argument(
        "--sr-sources",
        type=Path,
        help="Optional reconciliation-sr-sources-1 manifest; paths are relative to its directory",
    )
    return cli


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    created = False
    try:
        if args.output.exists():
            raise InputRejected("output_exists")
        manifest = load_json(args.manifest)
        prepared = build_candidate_catalog(
            manifest,
            ArtifactStore(args.store),
            corp_code=args.corp_code,
            fy=args.fy,
            rcept_no=args.rcept_no,
            consolidation=args.consolidation,
            max_candidates=args.max_candidates,
        )
        if args.sr_sources is not None:
            prepared = add_operator_sr_sources(
                prepared,
                load_json(args.sr_sources),
                args.sr_sources.resolve().parent,
            )
        args.output.mkdir(parents=True, exist_ok=False)
        created = True
        _write_artifacts(args.output, prepared)
        write_json(args.output / "candidates.json", prepared.catalog)
        write_json(args.output / "artifacts.json", prepared.artifact_index)
        if args.selections is not None:
            draft = prepare_review_draft(prepared, load_json(args.selections))
            write_json(args.output / "review-draft.json", draft)
            if draft["status"] == "draft_pending_review":
                write_json(args.output / "packet.json", draft["packet"])
                write_json(args.output / "policy.json", draft["policy"])
                write_json(args.output / "documents.json", draft["documents"])
                # Trusted approval and coverage registries intentionally stay empty.
                write_json(args.output / "coverage.json", {})
                write_json(args.output / "policies.json", {})
        return 0
    except (ValueError, OSError, PermissionError) as exc:
        if created:
            shutil.rmtree(args.output, ignore_errors=True)
        code = (
            str(exc)
            if isinstance(exc, InputRejected | CandidatePreparationError)
            else "prepare_rejected"
        )
        print(json.dumps({"error": code}), file=sys.stderr)
        return 2
    except Exception:
        if created:
            shutil.rmtree(args.output, ignore_errors=True)
        print('{"error":"prepare_failed"}', file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
