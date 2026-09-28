"""Submitted annotations remain references through real SQLite, HTTP and exports."""

import csv
import json
import sqlite3
from dataclasses import replace
from io import BytesIO
from zipfile import ZipFile

import pytest

from tests.acceptance.test_upload import FOREIGN, TENANT
from tests.integration.test_local_extract_runner import extraction_setup
from tests.integration.test_run_lifecycle import client, validate


def test_submission_roundtrip_preserves_pending_claim_and_frozen_export(tmp_path, monkeypatch):
    from proofops.adapters.local.export_store import LocalExportStore
    from proofops.adapters.local.submitted_review import attach_submission
    from proofops.application.exports import build_export, create_snapshot
    from proofops_agent.extraction import StructuredClaimExtractor, SyntheticClaimExtractor
    from proofops_api.routers.claims import build_claims_router

    synthetic = SyntheticClaimExtractor()
    seen = set()

    def unique_fixture(packet):
        result = synthetic.extract(packet)
        for span in result["spans"]:
            if span["quote"] in seen:
                span.update(kind="unknown", reason="synthetic fixture duplicate")
            seen.add(span["quote"])
        return result

    service, run_id, runner, now, _ = extraction_setup(
        tmp_path,
        monkeypatch,
        extractor=StructuredClaimExtractor(synthetic.profile, unique_fixture),
    )
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
    candidates = runner.claims.list(TENANT, run_id)
    claim = next(
        c
        for c in candidates
        if sum(
            c.quote in other.quote and c.source_refs[0].page_num == other.source_refs[0].page_num
            for other in candidates
        )
        == 1
    )
    assert claim.source_quality == "unverified"
    source_hash = service.store.snapshot(TENANT, run_id)["document"]["sha256"]
    directory = tmp_path / "submission"
    directory.mkdir()
    rows = {
        "claims": [
            dict(
                claim_id="external-1",
                document_id="DOC-1",
                physical_page=str(claim.source_refs[0].page_num),
                quote=claim.quote,
            )
        ],
        "elements": [dict(claim_id="external-1", state="conflict", quote="<script>bad</script>")],
        "numeric": [dict(claim_id="external-1", expected_check="unresolved")],
        "assurance": [dict(claim_id="external-1", expected_status="undetermined")],
    }
    for name, values in rows.items():
        with (directory / f"{name}.csv").open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=list(values[0]))
            writer.writeheader()
            writer.writerows(values)

    def attach(**changes):
        args = dict(
            tenant_id=TENANT,
            run_id=run_id,
            directory=directory,
            document_id="DOC-1",
            source_sha256=source_hash,
            origin="ai_corrected_submission",
            operator="test-operator",
        )
        return attach_submission(runner.claims, **(args | changes))

    jobs = service.store.jobs
    before = jobs.get_run(TENANT, run_id)["mutation_epoch"]
    with pytest.raises(ValueError, match="SOURCE_HASH"):
        attach(source_sha256="0" * 64)
    with pytest.raises(ValueError, match="RESOURCE_NOT_FOUND"):
        attach(tenant_id=FOREIGN)
    assert jobs.get_run(TENANT, run_id)["mutation_epoch"] == before
    attached = attach()
    assert attach() == attached
    assert jobs.get_run(TENANT, run_id)["mutation_epoch"] == before + 1
    assert runner.claims.get(TENANT, run_id, claim.claim_id) == claim
    assert runner.claims.current_tag(TENANT, run_id, claim.claim_id) is None

    http, auth = client(service)
    http.app.include_router(build_claims_router(runner.claims, auth, clock=lambda: now[0]))
    url = f"/v1/runs/{run_id}/claims/{claim.claim_id}"
    detail = http.get(url)
    assert detail.status_code == 200, detail.text
    validate("ClaimDetail", detail.json())
    assert detail.json()["claim"]["decision"] is None
    assert detail.json()["elements"] == []
    assert detail.json()["tag_status"] == "untagged"
    reference = detail.json()["submitted_reviews"][0]
    assert reference["status"] == "reference_only"
    assert reference["claim_source_quality"] == "unverified"
    assert reference["tables"] == rows
    auth.sessions.put(replace(auth.sessions.get("admin-session"), active_tenant_id=FOREIGN))
    assert http.get(url).status_code == 404

    from proofops.application.authorization import AuthContext

    actor = AuthContext("admin-user", TENANT, "admin", frozenset(), "admin-session")
    store = LocalExportStore(service.store, runner.claims)
    export_id = store.reserve(
        actor,
        run_id,
        dict(formats=["json", "html"], allow_partial=True),
        "submission-export-01",
        now=now[0],
    )
    snapshot = create_snapshot(store, TENANT, export_id)
    content, _, partial = build_export(snapshot, ["json", "html"])
    assert partial
    with ZipFile(BytesIO(content)) as archive:
        exported = json.loads(archive.read("submitted-reviews.json"))
        assert exported == [reference]
        assert all(
            c["decision_status"] == "not_run"
            for c in json.loads(archive.read("report.json"))["claims"]
        )
    (directory / "numeric.csv").write_text(
        "claim_id,expected_check\nexternal-1,changed-draft\n", encoding="utf-8"
    )
    attach()
    # The old frozen export cannot acquire later annotations.
    assert build_export(store.frozen(TENANT, export_id), ["json", "html"])[0] == content
    with sqlite3.connect(jobs.path) as db:
        for statement in (
            "UPDATE job_records SET value=value WHERE kind='submission_reference'",
            "DELETE FROM job_records WHERE kind='submission_reference'",
            "INSERT OR REPLACE INTO job_records SELECT * FROM job_records "
            "WHERE kind='submission_reference'",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(statement)
    (directory / "claims.csv").write_text(
        "claim_id,document_id,physical_page,quote\nexternal-1,DOC-1,1,missing quote\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="CLAIM_MATCH"):
        attach()
