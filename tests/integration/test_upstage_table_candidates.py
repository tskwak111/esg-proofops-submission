"""Offline real transport receipt → original-bound table candidates → fusion."""

import io
from dataclasses import replace
from hashlib import sha256
from uuid import uuid4

import pytest
from proofops.adapters.local.upstage_parse import UpstageParseProbe
from proofops.application.ingest.graph_fusion import SourceArtifact, fuse_candidates
from pypdf import PdfReader, PdfWriter


def prepared(tmp_path, monkeypatch):
    from datetime import UTC, datetime

    from proofops.adapters.local import upstage_parse

    class FixedDate(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 13, tzinfo=UTC)

    monkeypatch.setattr(upstage_parse, "datetime", FixedDate)
    writer = PdfWriter()
    writer.add_blank_page(500, 700)
    writer.add_blank_page(600, 800)
    buf = io.BytesIO()
    writer.write(buf)
    content = buf.getvalue()
    source = SourceArtifact(
        *(str(uuid4()) for _ in range(3)),
        sha256(content).hexdigest(),
        "test-version",
        content,
        synthetic=True,
    )
    subset = PdfWriter()
    subset.add_page(PdfReader(io.BytesIO(content)).pages[1])
    buf = io.BytesIO()
    subset.write(buf)
    raw = dict(
        model="document-parse-260128",
        usage=dict(pages=1, standard=[1]),
        elements=[
            dict(
                id=1,
                page=1,
                category="table",
                content=dict(
                    text="Year\tValue\n2025\t95.0",
                    html="<table><tr><th>Year</th><th>Value</th></tr><tr><td>2025</td><td>95.0</td></tr></table>",
                ),
                coordinates=[
                    dict(x=x, y=y) for x, y in [(0.1, 0.2), (0.9, 0.2), (0.9, 0.4), (0.1, 0.4)]
                ],
            )
        ],
    )
    probe = UpstageParseProbe("offline-test", tmp_path / "ledger.sqlite3")
    monkeypatch.setattr(probe, "_post_parse", lambda body, mode: raw)
    receipt = probe.parse(buf.getvalue(), request_id=str(uuid4()), mode="standard")
    return source, receipt


def test_original_page_binding_and_whole_table_only_geometry(tmp_path, monkeypatch):
    from evaluation.upstage_table_candidates import build_table_batch

    source, receipt = prepared(tmp_path, monkeypatch)
    manifest = str(uuid4())
    batch = build_table_batch(
        source, receipt, physical_pages=(2,), parse_manifest_id=manifest, tenant_id=source.tenant_id
    )
    graph = fuse_candidates((batch,), tenant_id=source.tenant_id)
    assert {b.page_num for b in graph.blocks} == {2}
    table = next(b for b in graph.blocks if b.kind == "table")
    assert table.bbox == pytest.approx((60, 160, 540, 320))
    cells = [b for b in graph.blocks if b.kind == "table_cell"]
    assert len(cells) == 4 and {b.normalized_text for b in cells} == {
        "Year",
        "Value",
        "2025",
        "95.0",
    }
    assert all(b.bbox is None and b.source_ref().location_quality == "unlocated" for b in cells)
    assert all(
        b.quality != "verified" and b.source_ref().verification_state == "candidate"
        for b in graph.blocks
    )
    assert batch == build_table_batch(
        source, receipt, physical_pages=(2,), parse_manifest_id=manifest, tenant_id=source.tenant_id
    )


@pytest.mark.parametrize("mutation", ["tenant", "bytes", "page", "response", "coordinates"])
def test_candidate_binding_rejects_mismatches(tmp_path, monkeypatch, mutation):
    from proofops.domain.provenance import canonical_hash

    from evaluation.upstage_table_candidates import build_table_batch

    source, receipt = prepared(tmp_path, monkeypatch)
    tenant = source.tenant_id
    pages = (2,)
    if mutation == "tenant":
        tenant = str(uuid4())
    elif mutation == "bytes":
        source = replace(source, content=source.content + b"changed")
    elif mutation == "page":
        pages = (1,)
    elif mutation == "response":
        receipt["raw_response"]["elements"][0]["content"]["html"] = "changed"
    else:
        receipt["raw_response"]["elements"][0]["coordinates"][0]["x"] = True
        receipt["response_sha256"] = canonical_hash(receipt["raw_response"])
    with pytest.raises(ValueError):
        build_table_batch(
            source, receipt, physical_pages=pages, parse_manifest_id=str(uuid4()), tenant_id=tenant
        )


@pytest.mark.parametrize("mutation", ["receipt", "raw", "usage", "content", "mode"])
def test_malformed_provider_envelope_is_a_controlled_rejection(tmp_path, monkeypatch, mutation):
    from proofops.domain.provenance import canonical_hash

    from evaluation.upstage_table_candidates import build_table_batch

    source, receipt = prepared(tmp_path, monkeypatch)
    if mutation == "receipt":
        receipt = None
    elif mutation == "raw":
        receipt["raw_response"] = None
    elif mutation == "usage":
        receipt["raw_response"]["usage"] = None
    elif mutation == "content":
        receipt["raw_response"]["elements"][0]["content"] = None
    else:
        receipt["mode"] = []
    if isinstance(receipt, dict):
        receipt["response_sha256"] = canonical_hash(receipt["raw_response"])
    with pytest.raises(ValueError):
        build_table_batch(
            source,
            receipt,
            physical_pages=(2,),
            parse_manifest_id=str(uuid4()),
            tenant_id=source.tenant_id,
        )
