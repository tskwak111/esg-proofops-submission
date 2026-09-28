"""The service package owns original-PDF validation, without evaluation imports."""

import pytest


def test_evaluation_reuses_packaged_note_validation():
    from proofops.adapters.local import table_layout_context as layout
    from proofops.adapters.local import table_notes as notes

    from evaluation import table_layout_context, table_notes

    assert table_notes.prepare is notes.prepare
    assert table_notes.validate is notes.validate
    assert table_notes.marker_targets is notes.marker_targets
    assert table_layout_context.table_layout_context is layout.table_layout_context
    assert table_layout_context.cell_source_matches is layout.cell_source_matches


@pytest.mark.parametrize("detected", [True, False])
def test_note_artifact_replay_preserves_original_and_registers_table_issue(detected):
    import json
    from dataclasses import replace
    from hashlib import sha256

    import pytest
    from proofops.adapters.local.table_notes import (
        freeze_note_review,
        prepare,
        replay_note_reviews,
        validate,
    )
    from proofops.application.ingest.graph_fusion import fuse_candidates

    from tests.acceptance.test_parsing import TENANT, pdf
    from tests.acceptance.test_tables import table

    source = pdf()
    graph = fuse_candidates(
        (replace(table([["Metric"], ["Value"]]), source_sha256=sha256(source).hexdigest()),),
        tenant_id=TENANT,
    )
    table_id = next(b.source_id for b in graph.blocks if b.kind == "table")
    packet = prepare(graph, source, [table_id], tenant_id=TENANT)
    payload = {
        "notes": [
            {
                "fragment_ids": [packet["untrusted_document_data"]["fragments"][0]["id"]],
                "target_ids": [],
                "kind": "unknown",
            }
        ]
    }
    if not detected:
        payload = {"notes": []}
    result = validate(payload, packet, graph, source, tenant_id=TENANT)
    artifact = freeze_note_review(graph, source, packet, result, tenant_id=TENANT)
    view = replay_note_reviews((artifact,), graph, source, tenant_id=TENANT)
    assert not graph.issues
    assert view.blocks == graph.blocks and view.candidates == graph.candidates
    assert view.edges == graph.edges
    from proofops.domain.numeric import unresolved_source_issue_ids

    cell = next(b for b in graph.blocks if b.kind == "table_cell")
    assert not unresolved_source_issue_ids(graph, {cell.source_id})
    assert unresolved_source_issue_ids(view, {cell.source_id}) == {view.issues[0].issue_id}
    assert len(view.issues) == 1 and view.issues[0].state == "open"
    assert set(view.issues[0].source_ids) == {b.source_id for b in graph.blocks}
    assert view == replay_note_reviews((artifact,), graph, source, tenant_id=TENANT)
    tampered = json.loads(artifact)
    tampered["checked"]["coverage_status"] = "complete"
    tampered["extracted"]["coverage_status"] = "complete"
    from proofops.domain.provenance import canonical_hash
    from proofops.domain.rulepacks import canonical_json

    tampered["artifact_sha256"] = canonical_hash(
        {k: v for k, v in tampered.items() if k != "artifact_sha256"}
    )
    with pytest.raises(ValueError):
        replay_note_reviews((canonical_json(tampered),), graph, source, tenant_id=TENANT)
    with pytest.raises(ValueError):
        replay_note_reviews((artifact, artifact), graph, source, tenant_id=TENANT)
    with pytest.raises(ValueError):
        replay_note_reviews((artifact,), graph, source + b"changed", tenant_id=TENANT)

    with pytest.raises(ValueError):
        replay_note_reviews((artifact,), view, source, tenant_id=TENANT)
    with pytest.raises(ValueError):
        replay_note_reviews(
            (artifact,), graph, source, tenant_id="22222222-2222-4222-8222-222222222222"
        )

    previous = json.loads(artifact)
    previous["validator_sha256"] = (
        "e89854b5e54c5af4450a053fc3939249be3642dc1a84f663845782bf3ca95b6c"
    )
    previous["artifact_sha256"] = canonical_hash(
        {k: v for k, v in previous.items() if k != "artifact_sha256"}
    )
    historical = canonical_json(previous)
    assert (
        replay_note_reviews((historical,), graph, source, tenant_id=TENANT).blocks == graph.blocks
    )
    assert canonical_json(previous) == historical


def test_packaged_note_extraction_owns_prompts_and_run_without_evaluation_imports():
    import inspect

    import proofops.adapters.local.note_extraction as packaged

    from evaluation import table_notes as compat

    assert compat.SYSTEM is packaged.SYSTEM
    assert compat.DISCOVERY_SYSTEM is packaged.DISCOVERY_SYSTEM
    assert compat.join_note_lines is packaged.join_note_lines
    assert compat.run is packaged.run
    assert "evaluation" not in inspect.getsource(packaged)
    assert "prefilter_comparison" not in inspect.getsource(packaged)
    assert "Path(__file__)" not in inspect.getsource(packaged)
    assert "importlib" in inspect.getsource(packaged) and "files(" in inspect.getsource(packaged)


def test_pre_upgrade_runtime_artifact_replays_without_rewriting(monkeypatch):
    import json
    from dataclasses import replace
    from hashlib import sha256
    from pathlib import Path
    from uuid import UUID

    from proofops.adapters.local.table_notes import replay_note_reviews
    from proofops.application.ingest.graph_fusion import fuse_candidates
    from proofops.domain.provenance import canonical_hash
    from proofops.domain.rulepacks import canonical_json

    from tests.acceptance.test_parsing import TENANT, pdf
    from tests.acceptance.test_tables import table

    monkeypatch.setattr(
        "tests.acceptance.test_parsing.uuid4", lambda: UUID("99999999-9999-4999-8999-999999999999")
    )
    source = pdf()
    graph = fuse_candidates(
        (replace(table([["Metric"], ["Value"]]), source_sha256=sha256(source).hexdigest()),),
        tenant_id=TENANT,
    )
    path = Path("fixtures/table_notes/legacy-resolved-review.json")
    artifact = path.read_text()
    view = replay_note_reviews((artifact,), graph, source, tenant_id=TENANT)
    assert len(view.issues) == 1 and view.blocks == graph.blocks
    assert path.read_text() == artifact
    # Unknown code hashes are not a blanket compatibility exemption.
    body = json.loads(artifact)
    body["validator_sha256"] = "0" * 64
    body["artifact_sha256"] = canonical_hash(
        {k: v for k, v in body.items() if k != "artifact_sha256"}
    )
    with pytest.raises(ValueError, match="mismatch"):
        replay_note_reviews((canonical_json(body),), graph, source, tenant_id=TENANT)
