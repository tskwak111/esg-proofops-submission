"""Actual bytes -> source validation -> engine -> CLI, without live services."""

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from evaluation.reconciliation_cli import main
from evaluation.reconciliation_fixtures import EXAMPLES, build_case, canonical_hash

ROOT = Path(__file__).resolve().parents[2]
CASES = sorted(p.stem for p in EXAMPLES.glob("*.json"))


def run_case(bundle, root):
    from proofops.adapters.reconciliation import FileSourceReader
    from proofops.application.reconciliation.service import reconcile

    return reconcile(
        bundle["packet"],
        bundle["policy"],
        source_reader=FileSourceReader(root, bundle["artifacts"]),
        explanation_search=lambda _: [],
        policy_registry=bundle["policies"],
        coverage_registry=bundle["coverage"],
        document_registry=bundle["documents"],
    )


@pytest.mark.parametrize("name", CASES)
def test_actual_synthetic_originals_match_contract_outcomes(tmp_path, name):
    assert len(CASES) == 8
    bundle = build_case(name, tmp_path)
    before = copy.deepcopy(bundle)
    result = run_case(bundle, tmp_path)
    expected = json.loads((EXAMPLES / f"{name}.json").read_text(encoding="utf-8"))["expected"]
    assert (result["execution_state"], result["status"]) == (
        expected["execution_state"],
        expected["status"],
    )
    schema = json.loads((ROOT / "contracts/reconciliation/output.schema.json").read_text("utf-8"))
    Draft202012Validator(schema).validate(result)
    assert bundle == before
    assert run_case(bundle, tmp_path) == result
    assert not {"label", "evidence_grade"} & result.keys()


@pytest.mark.parametrize(
    "mutation", ["hash", "quote", "locator", "tenant", "normalized", "approval"]
)
def test_tampering_cannot_produce_matched(tmp_path, mutation):
    bundle = build_case("c1-same-entities", tmp_path)
    if mutation == "hash":
        bundle["packet"]["sources"][0]["artifact_sha256"] = "a" * 64
    elif mutation == "quote":
        next(
            source
            for source in bundle["packet"]["sources"]
            if source["source_id"] == bundle["packet"]["financial"]["source_id"]
        )["quote"] = "forged quote"
    elif mutation == "locator":
        bundle["packet"]["sources"][0]["locator"] = "chars:999:1000"
    elif mutation == "tenant":
        bundle["documents"]["fs-v1"]["tenant_id"] = "other-tenant"
    elif mutation == "normalized":
        bundle["packet"]["sustainability"]["normalized"] = '["FORGED"]'
        bundle["packet"]["financial"]["normalized"] = '["FORGED"]'
    else:
        bundle["policies"] = {}
    result = run_case(bundle, tmp_path)
    assert result["execution_state"] == "blocked"
    assert result["status"] is None


def test_raw_search_complete_is_not_authority(tmp_path):
    bundle = build_case("c1-difference-no-explanation", tmp_path)
    bundle["coverage"] = {}
    result = run_case(bundle, tmp_path)
    assert result["execution_state"] == "blocked"
    assert result["status"] is None


@pytest.mark.parametrize("mutation", ["comparability", "period", "published", "role"])
def test_decision_identity_cannot_be_self_attested(tmp_path, mutation):
    bundle = build_case("c1-same-entities", tmp_path)
    packet = bundle["packet"]
    if mutation == "comparability":
        packet["comparability"] = "not_comparable"
    elif mutation == "period":
        packet["identity"]["financial_period_start"] = "2023-01-01"
    elif mutation == "published":
        packet["identity"]["financial_published_at"] = "2024-01-01"
    else:
        packet["sustainability"]["source_id"] = packet["financial"]["source_id"]
    result = run_case(bundle, tmp_path)
    assert result["execution_state"] == "blocked"
    assert result["status"] is None


def test_c3_cannot_bypass_policy_by_changing_claim_trigger(tmp_path):
    bundle = build_case("c3-policy-unresolved", tmp_path)
    bundle["packet"]["claim"]["trigger_elements"] = []
    result = run_case(bundle, tmp_path)
    assert result["execution_state"] == "blocked"
    assert result["status"] is None


def test_c3_explicit_synthetic_approval_reaches_comparison(tmp_path):
    bundle = build_case("c3-policy-unresolved", tmp_path)
    policy = bundle["policy"]
    policy.update(
        c3_threshold="20",
        c3_account_mapping_approved=True,
        allowed_capex_account_ids=["synthetic-PPE"],
    )
    approval = next(iter(bundle["policies"].values()))
    bundle["policies"] = {canonical_hash(policy): approval}
    result = run_case(bundle, tmp_path)
    assert result["execution_state"] == "completed"
    assert result["status"] == "matched"
    bundle["packet"]["c3_context"]["capex_period_start"] = "2023-01-01"
    assert run_case(bundle, tmp_path)["execution_state"] == "blocked"


def test_c4_unrelated_source_cannot_become_calculation_basis(tmp_path):
    bundle = build_case("c4-search-incomplete", tmp_path)
    packet = bundle["packet"]
    packet["c4_context"]["calculation_source_ids"] = [packet["financial"]["source_id"]]
    result = run_case(bundle, tmp_path)
    assert result["execution_state"] == "blocked"
    assert result["status"] is None


def test_valid_unrelated_quote_cannot_inherit_fact_binding(tmp_path):
    bundle = build_case("c1-same-entities", tmp_path)
    packet = bundle["packet"]
    source = next(
        s for s in packet["sources"] if s["source_id"] == packet["financial"]["source_id"]
    )
    source["quote"] = "SYNTHETIC FIXTURE ONLY"
    source["locator"] = f"chars:0:{len(source['quote'])}"
    result = run_case(bundle, tmp_path)
    assert result["execution_state"] == "blocked"
    assert result["status"] is None


def test_policy_registry_must_not_implicitly_widen_synthetic_scope(tmp_path):
    bundle = build_case("c1-same-entities", tmp_path)
    next(iter(bundle["policies"].values())).pop("synthetic_only")
    assert run_case(bundle, tmp_path)["execution_state"] == "blocked"


def test_synthetic_document_cannot_be_relabelled_as_real(tmp_path):
    bundle = build_case("c1-same-entities", tmp_path)
    bundle["packet"]["synthetic"] = False
    bundle["policy"]["synthetic_only"] = False
    approval = next(iter(bundle["policies"].values()))
    approval["synthetic_only"] = False
    bundle["policies"] = {canonical_hash(bundle["policy"]): approval}
    assert run_case(bundle, tmp_path)["execution_state"] == "blocked"


def test_application_hashes_preserve_submitted_revision(tmp_path):
    bundle = build_case("c1-same-entities", tmp_path)
    result = run_case(bundle, tmp_path)
    assert result["packet_sha256"] == canonical_hash(bundle["packet"])
    assert result["policy_sha256"] == canonical_hash(bundle["policy"])


def test_cli_roundtrip_and_c5_dispatch(tmp_path):
    artifacts = tmp_path / "originals"
    bundle = build_case("c1-same-entities", artifacts)
    paths = {}
    for key, value in bundle.items():
        target = tmp_path / f"{key}.json"
        target.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        paths[key] = target
    output = tmp_path / "result.json"
    argv = [
        "--packet",
        str(paths["packet"]),
        "--policy",
        str(paths["policy"]),
        "--output",
        str(output),
        "--artifacts",
        str(artifacts),
        "--artifact-index",
        str(paths["artifacts"]),
        "--documents",
        str(paths["documents"]),
        "--policy-registry",
        str(paths["policies"]),
        "--coverage-registry",
        str(paths["coverage"]),
    ]
    assert main(argv) == 0
    assert json.loads(output.read_text("utf-8"))["status"] == "matched"
    bundle["packet"]["item"] = "C5"
    paths["packet"].write_text(json.dumps(bundle["packet"]), encoding="utf-8")
    argv[argv.index("--output") + 1] = str(tmp_path / "c5.json")
    assert main(argv) == 0
    event = json.loads((tmp_path / "c5.json").read_text("utf-8"))
    assert event["execution_state"] == "not_run"
    assert event["reason_codes"] == ["stage_disabled"]
    assert event["dispatch_schema_version"] == "1.0"
