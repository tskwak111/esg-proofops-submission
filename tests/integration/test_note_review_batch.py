"""All-table orchestration, transport stop and immutable source-bound replay."""

import json
from dataclasses import replace
from hashlib import sha256

import pytest
from proofops.application.ingest.graph_fusion import fuse_candidates

from tests.acceptance.test_parsing import TENANT, pdf
from tests.acceptance.test_tables import table


def sample():
    source = pdf()
    batches = []
    for page in (1, 2, 3):
        batch = table([["Metric"], ["Value"]])
        batches.append(
            replace(
                batch,
                source_sha256=sha256(source).hexdigest(),
                blocks=tuple(
                    replace(b, source=replace(b.source, physical_page=page)) for b in batch.blocks
                ),
            )
        )
    return fuse_candidates(batches, tenant_id=TENANT), source


@pytest.mark.parametrize("stop", [None, "BUDGET_EXHAUSTED", "UPSTAGE_REQUEST_FAILED", "malformed"])
def test_all_tables_remain_held_and_completed_batch_replays_without_calls(tmp_path, stop):
    from proofops.adapters.local.note_review_batch import review_tables
    from proofops.adapters.local.table_notes import replay_note_reviews

    graph, source = sample()

    class Client:
        model = "synthetic-test"
        calls = 0

        def summary(self):
            return {"calls": self.calls}

        def complete(self, *args, **kwargs):
            self.calls += 1
            if stop == "malformed" and self.calls == 1:
                return {"content": "{bad json", "provider_model": self.model}
            if stop and stop != "malformed":
                raise ValueError(stop)
            return {"content": '{"notes":[]}', "provider_model": self.model}

    client = Client()
    artifacts = review_tables(graph, source, client, tmp_path / "batch", tenant_id=TENANT)
    assert len(artifacts) == 3
    assert client.calls == (1 if stop in {"BUDGET_EXHAUSTED", "UPSTAGE_REQUEST_FAILED"} else 3)
    view = replay_note_reviews(artifacts, graph, source, tenant_id=TENANT)
    assert len([i for i in view.issues if i.kind == "table_note_review"]) == 3
    assert view.blocks == graph.blocks and view.edges == graph.edges
    rows = [json.loads(a) for a in artifacts]
    assert all(r["checked"]["coverage_status"] == "unknown" for r in rows)
    assert all(r["checked"]["decision"] is None for r in rows)
    if stop in {"BUDGET_EXHAUSTED", "UPSTAGE_REQUEST_FAILED"}:
        assert [r["extracted"]["status"] for r in rows] == [
            "invalid_or_failed",
            "not_run",
            "not_run",
        ]
    before = client.calls
    with pytest.raises(ValueError, match="BASE_GRAPH"):
        review_tables(view, source, client, tmp_path / "already-reviewed", tenant_id=TENANT)
    assert client.calls == before
    assert review_tables(graph, source, client, tmp_path / "batch", tenant_id=TENANT) == artifacts
    assert client.calls == before
    client.model = "different-model"
    with pytest.raises(ValueError):
        review_tables(graph, source, client, tmp_path / "batch", tenant_id=TENANT)
    client.model = "synthetic-test"
    with pytest.raises(ValueError):
        review_tables(graph, source + b"changed", client, tmp_path / "batch", tenant_id=TENANT)
    with pytest.raises(ValueError):
        review_tables(graph, source, client, tmp_path / "batch", tenant_id="foreign")
    manifest = tmp_path / "batch" / "review.json"
    value = json.loads(manifest.read_text())
    value["artifacts"].pop()
    from proofops.domain.provenance import canonical_hash

    value["sha256"] = canonical_hash({k: v for k, v in value.items() if k != "sha256"})
    manifest.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        review_tables(graph, source, client, tmp_path / "batch", tenant_id=TENANT)
    assert client.calls == before


def test_incomplete_batch_never_repeats_an_uncertain_call(tmp_path):
    from proofops.adapters.local.note_review_batch import review_tables

    graph, source = sample()

    class Client:
        model = "synthetic-test"
        calls = 0

        def summary(self):
            return {}

        def complete(self, *args, **kwargs):
            self.calls += 1
            raise RuntimeError("simulated process interruption")

    client = Client()
    with pytest.raises(RuntimeError):
        review_tables(graph, source, client, tmp_path / "batch", tenant_id=TENANT)
    with pytest.raises(ValueError, match="INCOMPLETE"):
        review_tables(graph, source, client, tmp_path / "batch", tenant_id=TENANT)
    assert client.calls == 1
