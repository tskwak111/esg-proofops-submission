"""Exercise the operator CLI against a genuinely verified run and SQLite store."""

import json
from types import SimpleNamespace

from proofops.adapters.local.reconciliation_store import LocalReconciliationStore

from evaluation.reconciliation_import import main
from tests.reconciliation.test_product_store import (
    TENANT,
    auth,
    build_verified_run,
    prepare_bundle,
)


def test_operator_import_preserves_pending_review_and_ignores_file_approvals(
    tmp_path, monkeypatch, capsys
):
    verified = build_verified_run(tmp_path, monkeypatch)
    database = verified["service"].store.path
    store = LocalReconciliationStore(
        database,
        tmp_path / "managed",
        run_store=verified["service"].store,
        claims=verified["claims"],
        tags=verified["tags"],
    )
    bundle, artifacts = prepare_bundle(tmp_path, verified)
    draft = tmp_path / "draft"
    draft.mkdir()
    for name in ("packet", "policy", "documents", "artifacts", "coverage"):
        (draft / f"{name}.json").write_text(json.dumps(bundle[name]), encoding="utf-8")
    # An operator file cannot grant approval, even if its contents are invalid.
    (draft / "policies.json").write_text("not a trusted registry", encoding="utf-8")
    closed = []
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("LOCAL_DATABASE_PATH", str(database))
    monkeypatch.setattr(
        "proofops_api.composition.build_composition",
        lambda: SimpleNamespace(
            reconciliation=store,
            uploads=SimpleNamespace(close=lambda: closed.append("uploads")),
            registry=SimpleNamespace(close=lambda: closed.append("registry")),
        ),
    )
    capsys.readouterr()
    code = main(
        [
            "--database",
            str(database),
            "--bundle",
            str(draft),
            "--artifacts",
            str(artifacts),
            "--tenant-id",
            TENANT,
            "--run-id",
            verified["run_id"],
            "--claim-id",
            verified["claim_id"],
            "--actor",
            "operator-test",
        ]
    )
    output = json.loads(capsys.readouterr().out)
    assert code == 0
    assert output["review_state"] == "pending"
    assert output["policy_approved"] is False
    assert closed == ["uploads", "registry"]
    evaluated = store.evaluate(auth(), output["case_id"], {}, '"1"', "operator-import-evaluate-1")
    result = evaluated["latest_result"]["result"]
    assert result["execution_state"] == "blocked"
    assert "policy_unapproved" in result["reason_codes"]
