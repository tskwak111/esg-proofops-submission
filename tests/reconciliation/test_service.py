"""Application orchestration rejects untrusted provenance and search assertions."""

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
        (tmp_path / f"{document_id}.txt").write_bytes(payload)
        artifacts[document_id] = {"path": f"{document_id}.txt", "format": "text", "sha256": digest}
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
        receipt_id="receipt",
        reviewed_source_ids=["sr-scope", "fs-scope"],
    )
    coverage = {
        "receipt": {
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
        "approved_by": "trusted",
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


def _call(case, **overrides):
    packet, policy, reader, documents, coverage, policies = case
    return service.reconcile(
        packet,
        policy,
        source_reader=overrides.get("source_reader", reader),
        explanation_search=overrides.get("explanation_search", lambda _: []),
        policy_registry=overrides.get("policy_registry", policies),
        coverage_registry=overrides.get("coverage_registry", coverage),
        document_registry=overrides.get("document_registry", documents),
    )


def test_coverage_registry_overrides_packet_complete(tmp_path: Path, monkeypatch):
    case = _case(tmp_path)
    case[4]["receipt"]["state"] = "incomplete"
    captured = {}
    monkeypatch.setattr(
        service, "_domain_evaluate", lambda p, r: captured.update(packet=p) or {"ok": True}
    )
    assert _call(case)["ok"] is True
    assert captured["packet"]["search"]["state"] == "incomplete"
    assert captured["packet"]["explanation"]["search_complete"] is False


def test_missing_coverage_receipt_blocks(tmp_path: Path, monkeypatch):
    case = _case(tmp_path)
    monkeypatch.setattr(service, "_domain_evaluate", lambda *_: pytest.fail("domain called"))
    result = _call(case, coverage_registry={})
    assert "coverage_unverified" in result["reason_codes"]


def test_tampered_normalized_fact_blocks(tmp_path: Path, monkeypatch):
    case = _case(tmp_path)
    case[0]["sustainability"]["normalized"] = '["A","EVIL"]'
    monkeypatch.setattr(service, "_domain_evaluate", lambda *_: pytest.fail("domain called"))
    result = _call(case)
    assert "fact_binding_mismatch" in result["reason_codes"]


def test_invalid_explanation_candidate_blocks_instead_of_becoming_absence(
    tmp_path: Path, monkeypatch
):
    case = _case(tmp_path)
    packet, _, _, documents, coverage, _ = case
    source = dict(packet["sources"][0], source_id="bad-explanation", quote="fabricated")
    coverage["receipt"]["reviewed_source_ids"].append("bad-explanation")
    documents[source["document_id"]]["source_bindings"]["bad-explanation"] = {
        "locator": source["locator"],
        "quote": source["quote"],
        "roles": ["explanation"],
    }
    monkeypatch.setattr(service, "_domain_evaluate", lambda *_: pytest.fail("domain called"))
    result = _call(case, explanation_search=lambda _: [source])
    assert "explanation_source_unverified" in result["reason_codes"]


def test_search_failure_is_blocked_not_no_explanation(tmp_path: Path, monkeypatch):
    case = _case(tmp_path)
    monkeypatch.setattr(service, "_domain_evaluate", lambda *_: pytest.fail("domain called"))

    def failed(_):
        raise RuntimeError("model unavailable")

    result = _call(case, explanation_search=failed)
    assert "explanation_search_failed" in result["reason_codes"]


def test_valid_explanation_is_verified_and_bound_to_coverage(tmp_path: Path, monkeypatch):
    case = _case(tmp_path)
    packet, _, _, documents, coverage, _ = case
    source = dict(packet["sources"][0], source_id="explanation")
    coverage["receipt"]["reviewed_source_ids"].append("explanation")
    documents[source["document_id"]]["source_bindings"]["explanation"] = {
        "locator": source["locator"],
        "quote": source["quote"],
        "roles": ["explanation"],
    }
    captured = {}
    monkeypatch.setattr(
        service, "_domain_evaluate", lambda p, r: captured.update(packet=p) or {"ok": True}
    )
    assert _call(case, explanation_search=lambda _: [source])["ok"] is True
    assert captured["packet"]["explanation"]["source_id"] == "explanation"
    assert any(value["source_id"] == "explanation" for value in captured["packet"]["sources"])


def test_bad_packet_reference_raises_value_error(tmp_path: Path):
    case = _case(tmp_path)
    case[0]["financial"]["source_id"] = "missing"
    with pytest.raises(ValueError, match="source_reference_missing"):
        _call(case)


def test_comparability_must_match_trusted_decision_binding(tmp_path: Path, monkeypatch):
    case = _case(tmp_path)
    case[0]["comparability"] = "not_comparable"
    monkeypatch.setattr(service, "_domain_evaluate", lambda *_: pytest.fail("domain called"))
    result = _call(case)
    assert "decision_binding_mismatch" in result["reason_codes"]


def test_fact_source_must_have_the_correct_semantic_role(tmp_path: Path, monkeypatch):
    case = _case(tmp_path)
    case[0]["sustainability"]["source_id"] = case[0]["financial"]["source_id"]
    monkeypatch.setattr(service, "_domain_evaluate", lambda *_: pytest.fail("domain called"))
    result = _call(case)
    assert "source_role_mismatch" in result["reason_codes"]


def test_valid_but_unbound_quote_cannot_inherit_fact_binding(tmp_path: Path, monkeypatch):
    case = _case(tmp_path)
    source = next(
        value
        for value in case[0]["sources"]
        if value["source_id"] == case[0]["financial"]["source_id"]
    )
    source["quote"] = "공시 대상 법인은"
    source["locator"] = f"chars:0:{len(source['quote'])}"
    monkeypatch.setattr(service, "_domain_evaluate", lambda *_: pytest.fail("domain called"))
    result = _call(case)
    assert "source_binding_mismatch" in result["reason_codes"]


def test_c5_is_dispatched_as_not_implemented_before_schema(tmp_path: Path):
    case = _case(tmp_path)
    case[0]["item"] = "C5"
    with pytest.raises(NotImplementedError):
        _call(case)
