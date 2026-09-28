"""Runnable local numeric-consistency check over a frozen run.

This is the missing executable seam for
``proofops.application.numeric_analysis.analyze_numeric_consistency``. It reads a
frozen local run state directory (pilot.json/settings.json/parser.json/
state.sqlite3), replays the mutually verified snapshot/claims/graph through
``LocalClaimStore.load_evidence``, normalizes table observations with
``normalize_tables``, and runs the pure domain check.

Typed ``ClaimBinding`` objects are OPTIONAL and, when supplied via ``--bindings``,
are treated as explicit operator tagging, not automatic approval: the pure domain
check still validates every accepted binding against verified original/claim
provenance and rejects anything unverified. With no bindings supplied the honest
default is one ``binding_absent`` outcome per discovered claim, with per-claim
source holds shown. Foreign source ids, unknown JSON keys, and out-of-run
observation ids are rejected; metric/period/entity are never manufactured from
text. The printed JSON report carries snapshot identity, hashes and per-claim
state, never grades. Use --propose-bindings NEW.json to generate pending source-linked
comparison proposals and explicit holds, then review individual entries and import
the reviewed file through the unchanged --bindings path. Targets/intensity and
incomplete dimensions are deferred; no proposed entry is automatically accepted.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from dataclasses import asdict
from pathlib import Path

MAX_INPUT_BYTES = 1024 * 1024

_PILOT_REQUIRED = ("tenant_id", "run_id")
_BINDINGS_ROOT_KEYS = frozenset({"bindings"})
_SOURCE_REF_KEYS = frozenset(
    {
        "source_id",
        "document_version_id",
        "parse_manifest_id",
        "page_num",
        "printed_page_label",
        "bbox",
        "raw_text_sha256",
        "quote",
        "char_start",
        "char_end",
        "location_quality",
        "verification_state",
    }
)
_AGGREGATION_KEYS = frozenset(
    {
        "target_claim_id",
        "observation_ids",
        "binding_sha256",
        "source_refs",
        "relation",
        "acceptance_state",
    }
)
_BINDING_KEYS = frozenset(
    {
        "claim_id",
        "tenant_id",
        "document_version_id",
        "parse_manifest_id",
        "kind",
        "observation_ids",
        "reported_value",
        "metric_raw",
        "scope",
        "subject",
        "scope2_basis",
        "organizational_boundary",
        "unit",
        "denominator",
        "source_refs",
        "reporting_period",
        "quantity_kind",
        "baseline_period",
        "reported_value_ref",
        "binding_accepted",
        "aggregation",
        # R30: same-period different-product relative reduction inputs. Optional;
        # absent keys keep the existing legacy default (no product comparison).
        "baseline_subject",
        "subject_ref",
        "baseline_subject_ref",
        "product_comparison_accepted",
        "conditions",
    }
)


class InputRejected(ValueError):
    """A supplied JSON input is malformed, foreign, or out of the run's scope."""


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise InputRejected("duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(_value: str) -> object:
    raise InputRejected("non-finite JSON number")


def _load_json(path: Path) -> object:
    try:
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_INPUT_BYTES:
            raise InputRejected("JSON input must be a bounded regular file")
        raw = path.read_bytes()
        if len(raw) != metadata.st_size:
            raise InputRejected("JSON file changed while reading")
        return json.loads(
            raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_reject_constant
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InputRejected("invalid JSON file") from exc


def _only_known(obj: object, allowed: frozenset[str], label: str) -> dict:
    if not isinstance(obj, dict):
        raise InputRejected(f"{label} must be a JSON object")
    unknown = set(obj) - allowed
    if unknown:
        raise InputRejected(f"unknown {label} keys: {sorted(unknown)}")
    return obj


def _configure_env(state: Path) -> None:
    """Point the local composition at the frozen run; synthetic, never live."""
    os.environ.update(
        APP_ENV=os.environ.get("APP_ENV", "local"),
        MODEL_ADAPTER=os.environ.get("MODEL_ADAPTER", "synthetic"),
        LOCAL_DATABASE_PATH=str(state / "state.sqlite3"),
        LOCAL_PARSER_PROFILE_PATH=str(state / "parser.json"),
        LOCAL_RUN_SETTINGS_PATH=str(state / "settings.json"),
        LOCAL_TAGGING_MODE="",
    )


def _build_source_ref(raw: object, *, known_source_ids: set[str], label: str):
    from proofops.domain.values import SourceRef

    data = _only_known(raw, _SOURCE_REF_KEYS, label)
    if data.get("source_id") not in known_source_ids:
        raise InputRejected(f"{label} references a foreign source id")
    bbox = data.get("bbox")
    try:
        return SourceRef(
            source_id=data["source_id"],
            document_version_id=data["document_version_id"],
            parse_manifest_id=data["parse_manifest_id"],
            page_num=data["page_num"],
            printed_page_label=data.get("printed_page_label"),
            bbox=tuple(bbox) if bbox is not None else None,
            raw_text_sha256=data["raw_text_sha256"],
            quote=data["quote"],
            char_start=data["char_start"],
            char_end=data["char_end"],
            location_quality=data["location_quality"],
            verification_state=data["verification_state"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise InputRejected(f"invalid {label}: {exc}") from exc


def _build_binding(
    raw: object,
    *,
    tenant_id: str,
    known_source_ids: set[str],
    known_observation_ids: set[str],
):
    from proofops.domain.numeric import AggregationRelation, ClaimBinding, NumericCondition

    data = _only_known(raw, _BINDING_KEYS, "binding")
    if data.get("tenant_id") != tenant_id:
        raise InputRejected("binding tenant does not match the run")
    observation_ids = tuple(data.get("observation_ids") or ())
    foreign = set(observation_ids) - known_observation_ids
    if foreign:
        raise InputRejected(f"binding references out-of-run observation ids: {sorted(foreign)}")

    source_refs = tuple(
        _build_source_ref(ref, known_source_ids=known_source_ids, label="binding source_ref")
        for ref in data.get("source_refs") or ()
    )
    number = data.get("reported_value_ref")
    reported_value_ref = (
        _build_source_ref(number, known_source_ids=known_source_ids, label="reported_value_ref")
        if number is not None
        else None
    )
    aggregation = None
    if data.get("aggregation") is not None:
        agg = _only_known(data["aggregation"], _AGGREGATION_KEYS, "aggregation")
        aggregation = AggregationRelation(
            target_claim_id=agg["target_claim_id"],
            observation_ids=tuple(agg.get("observation_ids") or ()),
            binding_sha256=agg["binding_sha256"],
            source_refs=tuple(
                _build_source_ref(
                    ref, known_source_ids=known_source_ids, label="aggregation source_ref"
                )
                for ref in agg.get("source_refs") or ()
            ),
            relation=agg["relation"],
            acceptance_state=agg["acceptance_state"],
        )
    subject_ref = data.get("subject_ref")
    subject_ref = (
        _build_source_ref(subject_ref, known_source_ids=known_source_ids, label="subject_ref")
        if subject_ref is not None
        else None
    )
    baseline_subject_ref = data.get("baseline_subject_ref")
    baseline_subject_ref = (
        _build_source_ref(
            baseline_subject_ref, known_source_ids=known_source_ids, label="baseline_subject_ref"
        )
        if baseline_subject_ref is not None
        else None
    )
    conditions = []
    for raw_condition in data.get("conditions") or ():
        condition = _only_known(
            raw_condition,
            frozenset(
                {
                    "source_ref",
                    "observation_ids",
                    "binding_sha256",
                    "relation",
                    "acceptance_state",
                    "reviewed_by",
                }
            ),
            "numeric condition",
        )
        try:
            conditions.append(
                NumericCondition(
                    source_ref=_build_source_ref(
                        condition.get("source_ref"),
                        known_source_ids=known_source_ids,
                        label="condition source_ref",
                    ),
                    observation_ids=tuple(condition.get("observation_ids") or ()),
                    binding_sha256=condition["binding_sha256"],
                    relation=condition["relation"],
                    acceptance_state=condition["acceptance_state"],
                    reviewed_by=condition["reviewed_by"],
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise InputRejected(f"invalid numeric condition: {exc}") from exc
    try:
        return ClaimBinding(
            claim_id=data["claim_id"],
            tenant_id=data["tenant_id"],
            document_version_id=data["document_version_id"],
            kind=data["kind"],
            observation_ids=observation_ids,
            reported_value=data.get("reported_value"),
            metric_raw=data["metric_raw"],
            scope=data.get("scope"),
            subject=data.get("subject"),
            scope2_basis=data.get("scope2_basis"),
            organizational_boundary=data.get("organizational_boundary"),
            unit=data.get("unit"),
            denominator=data.get("denominator"),
            source_refs=source_refs,
            parse_manifest_id=data.get("parse_manifest_id"),
            reporting_period=data.get("reporting_period"),
            quantity_kind=data.get("quantity_kind"),
            baseline_period=data.get("baseline_period"),
            reported_value_ref=reported_value_ref,
            binding_accepted=data.get("binding_accepted", False),
            aggregation=aggregation,
            baseline_subject=data.get("baseline_subject"),
            subject_ref=subject_ref,
            baseline_subject_ref=baseline_subject_ref,
            product_comparison_accepted=data.get("product_comparison_accepted", False),
            conditions=tuple(conditions),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise InputRejected(f"invalid binding: {exc}") from exc


def _load_bindings(
    path: Path | None,
    *,
    tenant_id: str,
    known_source_ids: set[str],
    known_observation_ids: set[str],
) -> tuple:
    if path is None:
        return ()
    root = _only_known(_load_json(path), _BINDINGS_ROOT_KEYS, "bindings file")
    items = root.get("bindings")
    if not isinstance(items, list):
        raise InputRejected("bindings must be a JSON list under 'bindings'")
    return tuple(
        _build_binding(
            item,
            tenant_id=tenant_id,
            known_source_ids=known_source_ids,
            known_observation_ids=known_observation_ids,
        )
        for item in items
    )


def _source_ref_dict(ref) -> dict:
    data = asdict(ref)
    data["bbox"] = list(ref.bbox) if ref.bbox else None
    return data


def _result_dict(result) -> dict | None:
    if result is None:
        return None
    return dict(
        claim_id=result.claim_id,
        kind=result.kind,
        status=result.status,
        reported_value=result.reported_value,
        computed_value=result.computed_value,
        observation_ids=list(result.observation_ids),
        reason=result.reason,
        exact_ratio=list(result.exact_ratio) if result.exact_ratio else None,
        source_refs=[_source_ref_dict(ref) for ref in result.source_refs],
    )


def _report_dict(report) -> dict:
    return dict(
        # Snapshot identity + hashes, so the report is pinned to one frozen run.
        tenant_id=report.tenant_id,
        document_version_id=report.document_version_id,
        parse_manifest_id=report.parse_manifest_id,
        source_sha256=report.source_sha256,
        has_findings=report.has_findings,
        outcome_count=len(report.outcomes),
        # Per-claim state only; never a grade or label.
        outcomes=[
            dict(
                claim_id=o.claim_id,
                status=o.status,
                reason=o.reason,
                result=_result_dict(o.result),
                holds=[dict(h) for h in o.holds],
            )
            for o in report.outcomes
        ],
    )


def _read_pilot(state_dir: Path) -> dict:
    """pilot.json is a trusted frozen artifact; require only the run ids."""
    pilot = _load_json(state_dir / "pilot.json")
    if not isinstance(pilot, dict):
        raise InputRejected("pilot.json must be a JSON object")
    for key in _PILOT_REQUIRED:
        if not isinstance(pilot.get(key), str):
            raise InputRejected(f"pilot.json missing '{key}'")
    return pilot


def run(state_dir: Path, bindings_path: Path | None, *, propose: bool = False) -> dict:
    _configure_env(state_dir)
    from proofops.application.ingest.normalize import normalize_tables
    from proofops.application.numeric_analysis import analyze_numeric_consistency
    from proofops.domain.numeric import observation_source_holds
    from proofops_worker.composition import build_composition
    from proofops_worker.tag_runner import LocalTagRunner

    pilot = _read_pilot(state_dir)
    tenant_id, run_id = pilot["tenant_id"], pilot["run_id"]

    composition = build_composition(stage="tag")
    assert isinstance(composition, LocalTagRunner)
    _envelope, discovery, graph = composition.claims.load_evidence(tenant_id, run_id)
    normalized = normalize_tables(graph, tenant_id=tenant_id)
    observations = normalized.observations

    known_source_ids = {block.source_id for block in graph.blocks}
    known_observation_ids = {obs.observation_id for obs in observations}
    bindings = _load_bindings(
        bindings_path,
        tenant_id=tenant_id,
        known_source_ids=known_source_ids,
        known_observation_ids=known_observation_ids,
    )

    report = analyze_numeric_consistency(
        tenant_id=tenant_id,
        original=graph,
        observations=observations,
        bindings=bindings,
        claims=discovery.claims,
    )
    payload = _report_dict(report)
    # Show source holds for every observation regardless of binding, so an
    # unbound run still surfaces original-lineage diagnostics for review.
    payload["observation_holds"] = [
        dict(observation_source_holds(obs, graph)) for obs in observations
    ]
    payload["observation_count"] = len(observations)
    payload["claim_count"] = len(discovery.claims)
    payload["binding_count"] = len(bindings)
    if propose:
        from proofops.adapters.local.tag_store import LocalTagStore
        from proofops.application.evidence.binding import ClaimContext
        from proofops.application.numeric_analysis import propose_numeric_bindings
        from proofops.domain.provenance import canonical_hash
        from proofops.domain.values import SourceRef

        tags = LocalTagStore(composition.store, composition.uploads, composition.parser)
        contexts, context_errors = [], {}
        try:
            snapshot = tags.load_snapshot(tenant_id, run_id)
        except (KeyError, ValueError) as exc:
            snapshot = None
            context_errors["snapshot"] = str(exc)
        if snapshot is not None:
            by_claim = {claim.claim_id: claim for claim in discovery.claims}
            for item in snapshot["claims"]:
                raw = item.get("review_inputs")
                if raw is None:
                    continue
                try:
                    contexts.append(
                        ClaimContext(
                            by_claim[item["claim_id"]],
                            {
                                key: SourceRef(**ref) if ref else None
                                for key, ref in raw["dimensions"].items()
                            },
                        )
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    context_errors[item["claim_id"]] = str(exc)
        proposals = propose_numeric_bindings(
            tenant_id=tenant_id,
            original=graph,
            observations=observations,
            claims=discovery.claims,
            contexts=tuple(contexts),
        )
        payload["proposals"] = [asdict(proposal) for proposal in proposals]
        payload["normalization_issues"] = [asdict(issue) for issue in normalized.conflicts]
        payload["proposal_context_snapshot_sha256"] = (
            canonical_hash(snapshot) if snapshot is not None else None
        )
        payload["proposal_context_errors"] = context_errors
        payload["proposed_binding_count"] = sum(p.binding is not None for p in proposals)
        payload["proposal_status"] = "pending_explicit_review"
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the pure numeric-consistency check over a frozen local run."
    )
    parser.add_argument(
        "--state",
        required=True,
        type=Path,
        help="Frozen run state dir (pilot.json/settings.json/parser.json/state.sqlite3).",
    )
    parser.add_argument(
        "--bindings",
        type=Path,
        default=None,
        help="Optional operator-tagged typed ClaimBinding JSON (explicit, not auto-approval).",
    )
    parser.add_argument(
        "--propose-bindings",
        type=Path,
        help="Write a NEW pending binding JSON for review, then import with --bindings. "
        "Prints source-linked proposals/absent reasons; never accepts candidates.",
    )
    args = parser.parse_args(argv)
    try:
        report = run(args.state, args.bindings, propose=args.propose_bindings is not None)
        if args.propose_bindings is not None:
            pending = {"bindings": [p["binding"] for p in report["proposals"] if p["binding"]]}
            # Exclusive create protects previously reviewed input from accidental overwrite.
            with args.propose_bindings.open("x", encoding="utf-8") as output:
                json.dump(pending, output, ensure_ascii=False, indent=2)
    except (InputRejected, FileExistsError) as exc:
        print(json.dumps({"error": "input_rejected", "detail": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
