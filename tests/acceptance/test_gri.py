"""AT-006: GRI codes route through printed labels, never a global page offset."""

from hashlib import sha256
from uuid import uuid4

import pytest
from proofops.application.ingest.graph_fusion import (
    CandidateBatch,
    CandidateBlock,
    CanonicalDocumentGraph,
    fuse_candidates,
)
from proofops.domain.documents import NativeSource, PageGeometry

TENANT = "11111111-1111-4111-8111-111111111111"
FOREIGN = "22222222-2222-4222-8222-222222222222"
VERSION = "33333333-3333-4333-8333-333333333333"
MANIFEST = "44444444-4444-4444-8444-444444444444"


def graph(*rows: tuple[int, str]) -> CanonicalDocumentGraph:
    run_id = str(uuid4())
    blocks = tuple(
        CandidateBlock(
            "table_row" if text.startswith("GRI ") else "paragraph",
            NativeSource(
                VERSION,
                MANIFEST,
                run_id,
                f"block-{index}",
                page,
                str(page),
                (10, 10, 500, 30),
                "pdf_bottom_left_points",
                text,
                0,
                len(text),
            ),
            PageGeometry(600, 800, 0, (0, 0, 600, 800)),
        )
        for index, (page, text) in enumerate(rows)
    )
    batch = CandidateBatch(
        tenant_id=TENANT,
        document_version_id=VERSION,
        parse_manifest_id=MANIFEST,
        source_sha256=sha256(b"synthetic-gri-fixture").hexdigest(),
        parser_run_id=run_id,
        parser_name="synthetic-gri-parser",
        parser_version="synthetic-fixture",
        parser_family="synthetic-gri-parser",
        config_hash="a" * 64,
        blocks=blocks,
        synthetic=True,
    )
    return fuse_candidates((batch,), tenant_id=TENANT)


def test_each_printed_label_maps_to_its_own_physical_page_candidates() -> None:
    from proofops.application.ingest.gri import build_gri_index

    document = graph(
        (100, "GRI 305-1\tDirect greenhouse gas emissions\t34, 36"),
        (40, "Direct greenhouse gas emissions"),
        (43, "Scope 1 emissions"),
        (44, "Scope 1 emissions continued"),
    )

    entries = build_gri_index(document, {"34": (40,), "36": (43, 44)}, tenant_id=TENANT)

    assert len(entries) == 1
    entry = entries[0]
    assert entry.tenant_id == TENANT and entry.document_version_id == VERSION
    assert entry.indicator_code == "305-1"
    assert entry.printed_page_refs == ("34", "36")
    assert entry.resolved_physical_pages == (40, 43, 44)
    assert entry.resolution_state == "resolved"
    assert entry.link_text == "GRI 305-1\tDirect greenhouse gas emissions\t34, 36"
    assert entry.source_ref.page_num == 100 and entry.source_ref.bbox is not None


def test_missing_printed_page_is_unresolved_without_discarding_known_candidates() -> None:
    from proofops.application.ingest.gri import build_gri_index

    entries = build_gri_index(
        graph((100, "GRI 305-2\tEnergy indirect emissions\t34, 999")),
        {"34": (40,)},
        tenant_id=TENANT,
    )

    assert entries[0].printed_page_refs == ("34", "999")
    assert entries[0].resolved_physical_pages == (40,)
    assert entries[0].resolution_state == "unresolved"
    assert entries[0].resolution_state != "mismatch"


def test_gri_index_rejects_foreign_tenant_and_ignores_non_table_mentions() -> None:
    from proofops.application.ingest.gri import build_gri_index

    document = graph(
        (10, "This paragraph mentions GRI 305-1 and page 34"),
        (100, "GRI 2-1\tOrganizational details\t5"),
    )

    with pytest.raises(ValueError, match="NOT_FOUND"):
        build_gri_index(document, {"5": (7,)}, tenant_id=FOREIGN)
    assert [entry.indicator_code for entry in build_gri_index(document, {7: "5"})] == ["2-1"]
