"""Trusted document identity, version and as-of validation tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from proofops.adapters.reconciliation import FileSourceReader
from proofops.application.reconciliation import service

ROOT = Path(__file__).parents[2]
CONTRACT = ROOT / "contracts" / "reconciliation"


def _case(tmp_path: Path):
    packet = json.loads((CONTRACT / "example-input.json").read_text(encoding="utf-8"))
    policy = json.loads((CONTRACT / "example-policy.json").read_text(encoding="utf-8"))
    quote = packet["claim"]["quote"]
    payload = quote.encode()
    digest = hashlib.sha256(payload).hexdigest()
    artifacts = {}
    documents = {}
    for document_id, role, source_id in (
        ("sr-v1", "sustainability", "sr-scope"),
        ("fs-v1", "financial", "fs-scope"),
    ):
        path = tmp_path / f"{document_id}.txt"
        path.write_bytes(payload)
        artifacts[document_id] = {"path": path.name, "format": "text", "sha256": digest}
        source = next(value for value in packet["sources"] if value["source_id"] == source_id)
        source.update(artifact_sha256=digest, locator=f"chars:0:{len(quote)}")
        fact = packet[role]
        roles = [f"{role}_fact"]
        if packet["claim"]["source_id"] == source_id:
            roles.append("claim")
        documents[document_id] = {
            "synthetic": True,
            "tenant_id": "fixture-tenant",
            "company_id": "fixture-company",
            "package_id": "fixture-package",
            "document_version_id": document_id,
            "document_role": role,
            "artifact_sha256": digest,
            "corp_code": "00000000",
            "fiscal_year": 2024,
            "rcept_no": "synthetic-receipt" if role == "financial" else None,
            "consolidation": "consolidated",
            "published_at": "2025-03-31" if role == "financial" else "2025-06-30",
            "available_on": "2025-03-31" if role == "financial" else "2025-06-30",
            "as_of_date": "2025-06-30",
            "period_start": "2024-01-01",
            "period_end": "2024-12-31",
            "relevant_items": ["C1"],
            "decision_binding": {
                "item": packet["item"],
                "comparability": packet["comparability"],
                "claim": {
                    **packet["claim"],
                    "trigger_elements": list(packet["claim"]["trigger_elements"]),
                },
                "c3_context": packet["c3_context"],
                "c4_context": packet["c4_context"],
                "claim_id": packet["identity"]["claim_id"],
            },
            "source_bindings": {
                source_id: {"locator": source["locator"], "quote": source["quote"], "roles": roles}
            },
            "fact_bindings": {
                source_id: {key: fact[key] for key in ("raw", "normalized", "kind", "unit")}
            },
        }
    packet["search"].update(
        state="complete",
        coverage_policy_id=policy["coverage_policy_id"],
        receipt_id="r",
        reviewed_source_ids=["sr-scope", "fs-scope"],
    )
    coverage = {
        "r": {
            "state": "complete",
            "coverage_policy_id": policy["coverage_policy_id"],
            "tenant_id": "fixture-tenant",
            "company_id": "fixture-company",
            "package_id": "fixture-package",
            "required_document_ids": ["sr-v1", "fs-v1"],
            "reviewed_source_ids": ["sr-scope", "fs-scope"],
            "failed_document_ids": [],
        }
    }
    approval = {
        "approved": True,
        "approved_by": "reviewer",
        "approved_on": "2026-09-21",
        "version": policy["version"],
        "source_policy_sha256": policy["source_policy_sha256"],
        "synthetic_only": True,
    }
    return (
        packet,
        policy,
        FileSourceReader(tmp_path, artifacts),
        documents,
        coverage,
        {service.canonical_sha256(policy): approval},
    )


@pytest.mark.parametrize(
    ("document_id", "field", "value", "reason"),
    [
        ("sr-v1", "tenant_id", "other", "document_identity_mismatch"),
        ("fs-v1", "company_id", "other", "document_identity_mismatch"),
        ("fs-v1", "fiscal_year", 2023, "document_identity_mismatch"),
        ("fs-v1", "rcept_no", "wrong", "document_identity_mismatch"),
        ("fs-v1", "document_version_id", "fs-v2", "document_identity_mismatch"),
        ("fs-v1", "consolidation", "separate", "document_identity_mismatch"),
        ("fs-v1", "available_on", "2025-07-01", "document_not_available_as_of"),
        ("fs-v1", "as_of_date", "2025-07-01", "document_as_of_mismatch"),
        ("fs-v1", "published_at", "2025-04-01", "document_publication_mismatch"),
        ("fs-v1", "period_start", "2023-01-01", "document_period_mismatch"),
        ("sr-v1", "relevant_items", [], "document_not_relevant"),
        ("sr-v1", "artifact_sha256", "f" * 64, "document_artifact_mismatch"),
    ],
)
def test_registry_identity_mismatch_blocks(
    document_id, field, value, reason, tmp_path: Path, monkeypatch
):
    packet, policy, reader, documents, coverage, policies = _case(tmp_path)
    documents[document_id][field] = value
    monkeypatch.setattr(service, "_domain_evaluate", lambda *_: pytest.fail("domain called"))
    result = service.reconcile(
        packet,
        policy,
        source_reader=reader,
        explanation_search=lambda _: [],
        policy_registry=policies,
        coverage_registry=coverage,
        document_registry=documents,
    )
    assert result["execution_state"] == "blocked"
    assert reason in result["reason_codes"]


def test_valid_registry_reaches_domain_with_trusted_facts(tmp_path: Path, monkeypatch):
    packet, policy, reader, documents, coverage, policies = _case(tmp_path)
    captured = {}
    monkeypatch.setattr(
        service,
        "_domain_evaluate",
        lambda p, r: captured.update(packet=p, policy=r) or {"ok": True},
    )
    result = service.reconcile(
        packet,
        policy,
        source_reader=reader,
        explanation_search=lambda _: [],
        policy_registry=policies,
        coverage_registry=coverage,
        document_registry=documents,
    )
    assert result["ok"] is True
    assert captured["packet"]["financial"]["normalized"] == '["A","B"]'
    assert captured["packet"] is not packet


def test_claim_fiscal_year_mismatch_is_malformed(tmp_path: Path):
    packet, policy, reader, documents, coverage, policies = _case(tmp_path)
    packet["claim"]["fiscal_year"] = 2023
    with pytest.raises(ValueError, match="claim_identity_mismatch"):
        service.reconcile(
            packet,
            policy,
            source_reader=reader,
            explanation_search=lambda _: [],
            policy_registry=policies,
            coverage_registry=coverage,
            document_registry=documents,
        )
