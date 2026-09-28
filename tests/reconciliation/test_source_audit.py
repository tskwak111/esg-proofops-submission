from __future__ import annotations

import hashlib

import pytest

from evaluation.reconciliation_source_audit import account_hint, audit_sources, main
from tests.reconciliation.test_candidates import CORP, FY, RECEIPT, collection, prepare


def run_audit(store, manifest, **kwargs):
    return audit_sources(
        manifest,
        store.root,
        corp_code=CORP,
        fy=FY,
        rcept_no=RECEIPT,
        consolidation="consolidated",
        **kwargs,
    )


def test_original_replay_does_not_approve_mapping_or_policy(tmp_path):
    store, manifest = collection(tmp_path)
    report = run_audit(store, manifest)
    assert report["candidate_count"] >= 3
    assert report["verified_byte_locator_count"] == report["candidate_count"]
    draft = report["account_mapping_draft"]
    assert draft["inventory"][0]["raw_amount"] == "123"
    assert draft["allowed_capex_account_ids"] == []
    assert draft["inventory"][0]["include_in_capex"] is None
    assert draft["approved"] is False
    assert report["semantic_accuracy"] is None
    assert report["authenticated_review_created"] is False
    candidate = prepare(store, manifest).catalog["candidates"][0]
    assert (
        report["checks"][0]["quote_sha256"]
        == hashlib.sha256(candidate["source"]["quote"].encode("utf-8")).hexdigest()
    )
    assert len(report["evaluator_sha256"]) == 64


def test_changed_original_is_rejected(tmp_path):
    store, manifest = collection(tmp_path)
    entry = manifest["artifacts"][0]
    (store.root / entry["locator"]).write_bytes(b"tampered")
    with pytest.raises(ValueError):
        run_audit(store, manifest)


def test_identity_mismatch_is_rejected(tmp_path):
    store, manifest = collection(tmp_path)
    manifest["artifacts"][1]["corp_code"] = "00999999"
    with pytest.raises(ValueError):
        run_audit(store, manifest)


def test_truncation_remains_explicit(tmp_path):
    store, manifest = collection(tmp_path)
    report = run_audit(store, manifest, max_candidates=1)
    assert report["candidate_count"] == 1
    assert report["candidate_limit"]["truncated"] is True
    assert report["search_complete"] is False


@pytest.mark.parametrize(
    ("name", "statement", "expected"),
    [
        ("유형자산의 취득", "CF", "cash_flow_asset_purchase_candidate"),
        ("유형자산", "BS", "balance_sheet_stock_not_cash_flow"),
        ("투자활동현금흐름", "CF", "broad_investing_flow_requires_disaggregation"),
        ("유형자산의 취득", "BS", "balance_sheet_stock_not_cash_flow"),
        ("매출액", "IS", "unclassified"),
    ],
)
def test_mapping_hints_do_not_conflate_stocks_and_flows(name, statement, expected):
    assert account_hint({"account_nm": name, "sj_div": statement}) == expected


def test_cli_refuses_existing_output_without_touching_it(tmp_path):
    output = tmp_path / "report.json"
    output.write_text("existing", encoding="utf-8")
    assert (
        main(
            [
                "--manifest",
                "missing",
                "--store",
                "missing",
                "--corp-code",
                CORP,
                "--fy",
                str(FY),
                "--rcept-no",
                RECEIPT,
                "--consolidation",
                "consolidated",
                "--output",
                str(output),
            ]
        )
        == 2
    )
    assert output.read_text("utf-8") == "existing"
