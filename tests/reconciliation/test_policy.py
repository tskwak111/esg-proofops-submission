"""Trusted policy approval must override self-attested packet policy fields."""

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
        filename = f"{document_id}.txt"
        (tmp_path / filename).write_bytes(payload)
        artifacts[document_id] = {"path": filename, "format": "text", "sha256": digest}
        source = next(item for item in packet["sources"] if item["source_id"] == source_id)
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
        receipt_id="receipt-1",
        reviewed_source_ids=["sr-scope", "fs-scope"],
    )
    coverage = {
        "receipt-1": {
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
    return packet, policy, FileSourceReader(tmp_path, artifacts), documents, coverage


def _approval(policy: dict, *, approved: bool = True) -> dict:
    return {
        "approved": approved,
        "approved_by": "trusted-reviewer",
        "approved_on": "2026-09-21",
        "version": policy["version"],
        "source_policy_sha256": policy["source_policy_sha256"],
        "synthetic_only": policy["synthetic_only"],
    }


def test_registry_approval_overrides_untrusted_policy_boolean(tmp_path: Path, monkeypatch):
    packet, policy, reader, documents, coverage = _case(tmp_path)
    policy["approved"] = False
    policy_hash = service.canonical_sha256(policy)
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
        policy_registry={policy_hash: _approval(policy)},
        coverage_registry=coverage,
        document_registry=documents,
    )
    assert result["ok"] is True
    assert result["policy_sha256"] == policy_hash
    assert captured["policy"]["approved"] is True
    assert captured["policy"]["approved_by"] == "trusted-reviewer"


@pytest.mark.parametrize("registry", [None, {}])
def test_missing_policy_registry_blocks(registry, tmp_path: Path, monkeypatch):
    packet, policy, reader, documents, coverage = _case(tmp_path)
    monkeypatch.setattr(service, "_domain_evaluate", lambda *_: pytest.fail("domain called"))
    result = service.reconcile(
        packet,
        policy,
        source_reader=reader,
        explanation_search=lambda _: [],
        policy_registry=registry,
        coverage_registry=coverage,
        document_registry=documents,
    )
    assert result["execution_state"] == "blocked"
    assert "policy_unapproved" in result["reason_codes"]


def test_registry_rejection_overrides_packet_approved(tmp_path: Path, monkeypatch):
    packet, policy, reader, documents, coverage = _case(tmp_path)
    policy_hash = service.canonical_sha256(policy)
    monkeypatch.setattr(service, "_domain_evaluate", lambda *_: pytest.fail("domain called"))
    result = service.reconcile(
        packet,
        policy,
        source_reader=reader,
        explanation_search=lambda _: [],
        policy_registry={policy_hash: _approval(policy, approved=False)},
        coverage_registry=coverage,
        document_registry=documents,
    )
    assert "policy_unapproved" in result["reason_codes"]


def test_synthetic_policy_cannot_approve_live_packet(tmp_path: Path, monkeypatch):
    packet, policy, reader, documents, coverage = _case(tmp_path)
    packet["synthetic"] = False
    policy_hash = service.canonical_sha256(policy)
    monkeypatch.setattr(service, "_domain_evaluate", lambda *_: pytest.fail("domain called"))
    result = service.reconcile(
        packet,
        policy,
        source_reader=reader,
        explanation_search=lambda _: [],
        policy_registry={policy_hash: _approval(policy)},
        coverage_registry=coverage,
        document_registry=documents,
    )
    assert "policy_scope_mismatch" in result["reason_codes"]


def test_canonical_hash_is_ascii_escaped_v11_compatible():
    value = {"한글": "값", "z": 1}
    expected = hashlib.sha256(
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert service.canonical_sha256(value) == expected
    assert (
        service.canonical_sha256(value)
        != hashlib.sha256(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
