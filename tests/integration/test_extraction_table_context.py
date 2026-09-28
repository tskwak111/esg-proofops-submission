"""R12 extraction table context: opt-in wire only, no paid calls, no new claim spans.

The table axes replace the nearest-neighbour blocks ONLY when the canonical graph
actually resolves an unambiguous table lineage. The legacy and context profiles
keep their exact hashes so existing receipts replay unchanged.
"""

import json
from dataclasses import asdict, replace

import pytest
from proofops.application.tagging.preliminary import SYSTEM_PROMPT as PRELIMINARY_PROMPT
from proofops_agent.upstage_extraction import (
    CONTEXT_SYSTEM_SUFFIX,
    SYSTEM_PROMPT,
    TABLE_CONTEXT_SYSTEM_SUFFIX,
    UpstageClaimExtractor,
    _profile,
    _profile_with_options,
    _profile_with_year_notation,
)

from tests.acceptance.test_citations import TENANT
from tests.acceptance.test_preliminary_table_sources import table_corpus
from tests.integration.test_upstage_extraction import FakeProbe

assert PRELIMINARY_PROMPT  # imported to prove the two prompts stay separate


def _packet(graph, block):
    return dict(
        tenant_id=TENANT,
        document_version_id=graph.document_version_id,
        parse_manifest_id=graph.parse_manifest_id,
        source_sha256=graph.source_sha256,
        untrusted_document_data=dict(
            source_id=block.source_id,
            text=block.normalized_text,
            kind=block.kind,
            page_num=block.page_num,
        ),
    )


def _extractor(tmp_path, *, table):
    probe = FakeProbe(json.dumps({"claims": []}, ensure_ascii=False))
    extractor = UpstageClaimExtractor(
        probe,
        tmp_path / "receipts",
        extraction_context=True,
        extraction_table_context=table,
    )
    return probe, extractor


def _sent_context(probe):
    user = json.loads(probe.calls[-1]["user_json"])
    return [
        (block["role"], block["text"])
        for block in user["untrusted_document_data"]["context_blocks"]
    ]


def test_every_pre_existing_profile_hash_is_unchanged():
    """Frozen receipts must keep replaying under their own validator."""
    legacy = _profile()
    assert legacy == _profile_with_options()
    assert legacy.prompt_sha256 != _profile_with_options(extraction_context=True).prompt_sha256
    year = _profile_with_year_notation()
    assert year == _profile_with_options(year_notation=True)
    context = _profile_with_options(extraction_context=True)
    table = _profile_with_options(extraction_context=True, extraction_table_context=True)
    assert len({legacy.rule_sha256, year.rule_sha256, context.rule_sha256, table.rule_sha256}) == 4
    assert table.prompt_sha256 != context.prompt_sha256
    assert (
        SYSTEM_PROMPT + CONTEXT_SYSTEM_SUFFIX + TABLE_CONTEXT_SYSTEM_SUFFIX
    ) != SYSTEM_PROMPT + CONTEXT_SYSTEM_SUFFIX


def test_table_context_cannot_be_enabled_without_context(tmp_path):
    with pytest.raises(ValueError, match="UPSTAGE_EXTRACTION_TABLE_CONTEXT_INVALID"):
        UpstageClaimExtractor(
            FakeProbe("{}"),
            tmp_path / "receipts",
            extraction_table_context=True,
        )
    with pytest.raises(ValueError, match="UPSTAGE_PROFILE_OPTION_INVALID"):
        _profile_with_options(extraction_table_context=True)


def test_a_table_value_receives_its_row_and_column_headers_instead_of_neighbour_numbers(tmp_path):
    graph, claim, block = table_corpus()
    focal = block("r1_2025-atom")
    probe, extractor = _extractor(tmp_path, table=True)
    packet = _packet(graph, focal)
    packet["extraction_profile"] = asdict(extractor.profile)
    assert extractor.extract(packet, context_graph=graph) == {"spans": []}
    sent = _sent_context(probe)
    assert ("table_row_header", "온실가스 배출량") in sent
    assert ("table_column_header", "2025") in sent
    # The adjacent row's value, which plain nearest-neighbour context used to
    # send, is never offered as context for this cell.
    assert not [text for role, text in sent if text in {"4,800", "5,000", "2024"}]
    assert probe.calls[-1]["system"].endswith(TABLE_CONTEXT_SYSTEM_SUFFIX)


def test_a_block_outside_any_table_keeps_the_unchanged_neighbour_context(tmp_path):
    graph, claim, block = table_corpus()
    loose = block("loose")
    table_probe, table_extractor = _extractor(tmp_path / "table", table=True)
    plain_probe, plain_extractor = _extractor(tmp_path / "plain", table=False)
    for probe, extractor in ((table_probe, table_extractor), (plain_probe, plain_extractor)):
        packet = _packet(graph, loose)
        packet["extraction_profile"] = asdict(extractor.profile)
        extractor.extract(packet, context_graph=graph)
    assert _sent_context(table_probe) == _sent_context(plain_probe)


def test_table_axes_preserve_the_declared_section_heading(tmp_path):
    from proofops.application.ingest.graph_fusion import CanonicalEdge

    graph, _, block = table_corpus()
    focal, heading = block("r1_2025-atom"), block("loose")
    graph = replace(
        graph,
        blocks=tuple(
            replace(b, kind="heading") if b.source_id == heading.source_id else b
            for b in graph.blocks
        ),
        edges=(*graph.edges, CanonicalEdge(focal.source_id, heading.source_id, "section_parent")),
    )
    probe, extractor = _extractor(tmp_path, table=True)
    packet = _packet(graph, focal)
    packet["extraction_profile"] = asdict(extractor.profile)
    extractor.extract(packet, context_graph=graph)
    assert ("heading", heading.raw_text) in _sent_context(probe)
    assert ("table_column_header", "2025") in _sent_context(probe)


def test_a_conflicted_cell_lineage_falls_back_instead_of_guessing_a_row(tmp_path):
    """A cell with no selected winner has no coordinates: no table context."""
    graph, claim, block = table_corpus()
    cell_id = block("r1_2025").source_id
    conflicted = replace(
        graph,
        blocks=tuple(
            replace(b, winner=None) if b.source_id == cell_id else b for b in graph.blocks
        ),
    )
    probe, extractor = _extractor(tmp_path, table=True)
    packet = _packet(conflicted, block("r1_2025-atom"))
    packet["extraction_profile"] = asdict(extractor.profile)
    extractor.extract(packet, context_graph=conflicted)
    assert not [role for role, _ in _sent_context(probe) if role.startswith("table_")]
