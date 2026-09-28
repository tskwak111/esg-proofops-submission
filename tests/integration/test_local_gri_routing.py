"""GRI routing reads explicit PDF labels, never pypdf's physical-page fallback."""

from dataclasses import replace
from io import BytesIO

import pytest
from pypdf import PdfWriter
from pypdf.constants import PageLabelStyle

from tests.acceptance.test_gri import TENANT, graph


def pdf(*, labels=True):
    writer = PdfWriter()
    for _ in range(4):
        writer.add_blank_page(width=600, height=800)
    if labels:
        writer.set_page_label(0, 3, style=PageLabelStyle.DECIMAL, start=34)
    stream = BytesIO()
    writer.write(stream)
    return stream.getvalue()


def document(content):
    from hashlib import sha256

    original = graph(
        (3, "GRI 305-1\t온실가스 직접 배출량\t34"),
        (4, "GRI 403-1\t산업 안전보건\t35"),
        (1, "온실가스 직접 배출량 데이터"),
        (2, "산업 안전보건 데이터"),
    )
    digest = sha256(content).hexdigest()
    return replace(
        original,
        source_sha256=digest,
        candidates=tuple(replace(c, source_sha256=digest) for c in original.candidates),
    )


def test_explicit_labels_route_related_index_rows_without_promoting_quality():
    from proofops.adapters.local.gri_routing import LocalGRIRouting

    content = pdf()
    original = document(content)
    routing = LocalGRIRouting(original, content, tenant_id=TENANT, pages=[1, 2, 3, 4])
    entries, codes = routing.for_claim("온실가스 직접 배출량을 공개합니다")
    assert codes == ("305-1",)
    assert next(e for e in entries if e.indicator_code == "305-1").resolved_physical_pages == (1,)
    assert all(b.quality == "unverified" for b in original.blocks)


def test_missing_labels_remain_unresolved_and_excluded_pages_do_not_route():
    from proofops.adapters.local.gri_routing import LocalGRIRouting

    content = pdf(labels=False)
    routing = LocalGRIRouting(document(content), content, tenant_id=TENANT, pages=[1, 2, 3, 4])
    entries, codes = routing.for_claim("온실가스 직접 배출량")
    assert codes == ("305-1",)
    assert all(e.resolution_state == "unresolved" for e in entries)
    assert all(e.resolved_physical_pages == () for e in entries)
    routing = LocalGRIRouting(document(content), content, tenant_id=TENANT, pages=[1, 2])
    assert routing.for_claim("온실가스 직접 배출량") == ((), ())


def test_original_hash_and_tenant_are_checked_before_routing():
    from proofops.adapters.local.gri_routing import LocalGRIRouting

    content = pdf()
    with pytest.raises(ValueError, match="SOURCE_INTEGRITY_MISMATCH"):
        LocalGRIRouting(document(content), content + b"changed", tenant_id=TENANT, pages=[1])
    with pytest.raises(ValueError, match="NOT_FOUND"):
        LocalGRIRouting(document(content), content, tenant_id="foreign", pages=[1])


def test_live_worker_calls_gri_router_and_freezes_route_generation(tmp_path, monkeypatch):
    from proofops.adapters.local.gri_routing import LocalGRIRouting

    from tests.integration.test_live_tagging_pipeline import _pipeline_setup

    calls = []
    original = LocalGRIRouting.for_claim

    def observe(self, quote):
        result = original(self, quote)
        calls.append((quote, result))
        return result

    monkeypatch.setattr(LocalGRIRouting, "for_claim", observe)
    ctx = _pipeline_setup(tmp_path, monkeypatch)
    runner, tenant, run_id = ctx["tag_runner"], ctx["tenant"], ctx["run_id"]
    assert runner.run_once(tenant_id=tenant, run_id=run_id) == "needs_review"
    assert len(calls) == 1
    claim = runner.claims.list(tenant, run_id)[0]
    packet = runner.tags.load_inputs(tenant, run_id, claim.claim_id).original_packet.to_dict()
    assert packet["index_generation"].startswith("local-lexical-gri-v1:")
    assert packet["search_coverage"]["lexical_status"] == "bounded"
    assert packet["search_coverage"]["not_found_state"] == "unknown"


def test_visible_margin_number_routes_without_metadata_but_ambiguous_numbers_do_not():
    from proofops.adapters.local.gri_routing import LocalGRIRouting
    from pypdf import PdfReader
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    def with_footer(text):
        writer = PdfWriter(clone_from=PdfReader(BytesIO(pdf(labels=False))))
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page = writer.pages[0]
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 10 Tf 20 20 Td ({text}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
        output = BytesIO()
        writer.write(output)
        return output.getvalue()

    for text, expected in (("034", (1,)), ("34 35", ())):
        content = with_footer(text)
        routing = LocalGRIRouting(document(content), content, tenant_id=TENANT, pages=[1, 2, 3, 4])
        entries, _ = routing.for_claim("온실가스 직접 배출량")
        assert entries[0].resolved_physical_pages == expected
