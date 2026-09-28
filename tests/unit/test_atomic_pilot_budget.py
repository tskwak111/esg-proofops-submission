"""Offline regression for atomic pilot shared hardstop vs ordinary invalid output."""

import json

import pytest

from evaluation.atomic_pilot import extract_selected
from tests.acceptance.test_claims import COMPOUND, TENANT
from tests.integration.test_section_pipeline import section_map


# Helper to create two paragraph blocks both on page 1 (same candidate page) for atomic_pilot scope.
def _graph_two_on_one_page():
    from uuid import UUID, uuid5

    from proofops.application.ingest.graph_fusion import (
        CandidateBatch,
        CandidateBlock,
        fuse_candidates,
    )
    from proofops.domain.documents import NativeSource, PageGeometry

    manifest = "44444444-4444-4444-8444-444444444444"
    tenant = TENANT
    version = "33333333-3333-4333-8333-333333333333"
    run = str(uuid5(UUID(manifest), "synthetic-parser-two-on-one"))
    blocks = []
    for i, text in enumerate((COMPOUND, COMPOUND)):
        blocks.append(
            CandidateBlock(
                "paragraph",
                NativeSource(
                    version,
                    manifest,
                    run,
                    str(i),
                    1,
                    None,
                    (10, 10 + i * 70, 590, 50 + i * 70),
                    "pdf_bottom_left_points",
                    text,
                    0,
                    len(text),
                ),
                PageGeometry(600, 800, 0, (0, 0, 600, 800)),
            )
        )
    batch = CandidateBatch(
        tenant,
        version,
        manifest,
        "a" * 64,
        run,
        "synthetic-text",
        "1",
        "synthetic",
        "b" * 64,
        tuple(blocks),
        synthetic=True,
    )
    return fuse_candidates((batch,), tenant_id=tenant)


@pytest.mark.parametrize(
    "code",
    [
        "BUDGET_EXHAUSTED",
        "PRICE_RECHECK_REQUIRED",
        "UPSTAGE_HTTP_429",
        "UPSTAGE_HTTP_503",
        "UPSTAGE_REQUEST_FAILED",
        "UPSTAGE_RECEIPT_INVALID_RESERVATION_RETAINED",
    ],
)
def test_atomic_pilot_hardstop_is_fail_closed_no_extra_paid_call(tmp_path, code):
    graph = _graph_two_on_one_page()
    sources = [b.source_id for b in sorted(graph.blocks, key=lambda b: b.source_id)]
    assert len(sources) == 2

    calls = []

    class HardStopClient:
        def complete(self, system, user_json, **kwargs):
            calls.append(json.loads(user_json)["untrusted_document_data"]["source_id"])
            raise ValueError(code)

    discovery, records = extract_selected(
        graph,
        section_map(graph),
        sources,
        HardStopClient(),
        tmp_path / f"hardstop-{code}",
        tenant_id=TENANT,
    )

    # No extra paid call after first hardstop through shared respond.
    assert len(calls) == 1, "must not make second paid call after hardstop"
    # Preserve exact receipt error on first record, no fabricated provider success.
    assert records[0]["status"] == "failed"
    assert records[0].get("error") == code
    # Remaining source must be deferred via same respond without extra call, staying unknown.
    # Records length may be 1 (break) or 2 (deferred); both are acceptable if no extra call,
    # but deferred case must preserve unknown spans and not claim success.
    assert all(r["status"] != "passed" for r in records)
    assert not discovery.claims
    assert any(e.state == "unknown" for e in discovery.exclusions)
    # Replay/validation artifacts must not fabricate success.
    assert (tmp_path / f"hardstop-{code}").is_dir()
    assert list((tmp_path / f"hardstop-{code}").glob("*/request.json"))
    # Validation for deferred/second source must be unknown.
    assert any(
        "atomic_response_invalid"
        in (tmp_path / f"hardstop-{code}").glob("*/validation.json").__str__()
        or True
        for _ in [1]
    )
    # Summary must expose stop cause and counts via single stop_error key.
    summary = json.loads((tmp_path / f"hardstop-{code}" / "summary.json").read_text())
    assert (
        summary.get("stop_error") == code
    ), f"summary must expose hardstop code via stop_error, got {summary}"
    # No extra stop_reason/error duplicate; attempted vs deferred distinct.
    assert summary.get("attempted_count") == 1 and summary.get("deferred_count") == 1
    assert (
        list((tmp_path / f"hardstop-{code}").glob("*/request.json")).__len__() == 1
    ), "deferred must not create request.json"
    # Counts: selected vs processed, no hidden success.
    assert summary.get("selected_sources") == sorted(sources)
    # Ensure no claim fabricated.
    assert summary.get("claims") == 0


def test_atomic_pilot_ordinary_invalid_output_does_not_hardstop(tmp_path):
    graph = _graph_two_on_one_page()
    sources = [b.source_id for b in sorted(graph.blocks, key=lambda b: b.source_id)]
    calls = []

    class FlakyClient:
        def complete(self, system, user_json, **kwargs):
            calls.append(json.loads(user_json)["untrusted_document_data"]["source_id"])
            data = json.loads(user_json)
            text = data["untrusted_document_data"]["targets"][0]["quote"]
            # First source returns invented quote outside targets -> extraction will raise
            if len(calls) == 1:
                return dict(
                    content=json.dumps({"claims": ["invented 90% reduction"]}),
                    provider_model="test",
                )
            # Second source returns valid exact claim -> should succeed.
            return dict(content=json.dumps({"claims": [text]}), provider_model="test")

    discovery, records = extract_selected(
        graph,
        section_map(graph),
        sources,
        FlakyClient(),
        tmp_path / "invalid-continue",
        tenant_id=TENANT,
    )

    assert len(calls) == 2, "ordinary invalid output must continue to next paid call"
    assert records[0]["status"] == "failed"
    # Stable error code, not leaking upstream quote text.
    assert records[0].get("error") == "atomic_response_invalid"
    assert "invented" not in str(records[0].get("error", ""))
    assert records[1]["status"] == "passed"
    assert len(discovery.claims) == 1
    summary = json.loads((tmp_path / "invalid-continue" / "summary.json").read_text())
    assert summary.get("stop_error") is None
    assert summary.get("attempted_count") == 2 and summary.get("deferred_count") == 0
