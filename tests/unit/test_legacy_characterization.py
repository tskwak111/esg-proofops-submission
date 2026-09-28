"""TASK-043 legacy characterization and selective migration tests.

Frozen source: tskwak111/esg-evidence-audit @ 70da628a401b89b5ea2c08ee4243523ce99acb50
Docs: 26_LEGACY_REUSE_AUDIT.md R-01..R-05, 27/28, fixtures/edge_cases.json.  # noqa: E501

Acceptance (task_catalog): A1/B1/B2 dangling-edge 사례를 테스트로 추가하고
raw 정책·gold label의 무단 이식을 차단한다.

Tests run frozen graph/parser behavior through a minimal model import shim;
they make no AWS, network, or file mutations. Snapshot-dependent checks skip
when the ignored local snapshot is unavailable. They also verify that new runtime
does NOT import prohibited legacy policies/scorer/gold labels. Migration of pure
utilities is recorded as deferred due to missing repo license (see
evidence/legacy_access.md: no LICENSE at commit) — no legacy code is copied into
packages/proofops in this dispatch.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import pathlib
import re
import sys
import types
from dataclasses import dataclass, replace
from typing import Any

import pytest

LEGACY_COMMIT = "70da628a401b89b5ea2c08ee4243523ce99acb50"
LEGACY_SNAPSHOT = pathlib.Path(__file__).resolve().parents[2] / "legacy_reference" / "snapshot"
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
PACKAGES_ROOT = REPO_ROOT / "packages" / "proofops"
FROZEN_SHA256 = {
    "run_signature.py": "30c6edbac0531d6a30aae433219bda34cffef476bf164438b15a0a03bdd76985",
    "graph_ensemble.py": "96b71e8b0b4a551d450c0d71dcc107c4875a8c2fa050377b0e809209a91985ac",
    "document_graph.py": "3afc26ac8a845e19553c1cb471738dcd6d4e7e2ebd37bef2dc4178ce3b7d1bc7",
    "parser_adapters.py": "ccddd25fae512f6446de19656886a0d03d1d205f7275b0f6361eb92b42883f19",
}


def _require_snapshot() -> pathlib.Path:
    if not LEGACY_SNAPSHOT.is_dir():
        pytest.skip("not_run: ignored legacy_reference snapshot is unavailable")
    return LEGACY_SNAPSHOT


# ---------------------------------------------------------------------------
# Frozen-source loader.  The upstream package is not installed: these small
# value objects supply only the model surface that its real functions call.
# The behavior under test is always executed from the verified snapshot.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FrozenBlock:
    block_id: str
    page_num: int
    block_type: str
    text: str
    bbox: tuple[float, float, float, float]
    source_parser: str
    source_block_id: str = ""
    metadata: dict[str, Any] | None = None
    table_structure: Any = None

    def model_copy(self, *, update: dict[str, Any]) -> _FrozenBlock:
        return replace(self, **update)


@dataclass(frozen=True)
class _FrozenRelation:
    relation_type: str
    source_block_id: str
    target_block_id: str
    confidence: float = 1.0
    notes: str = ""

    def model_copy(self, *, update: dict[str, Any]) -> _FrozenRelation:
        return replace(self, **update)


@dataclass
class _FrozenGraph:
    report_id: str
    source_pdf: str
    parser_runs: list[Any]
    blocks: list[_FrozenBlock]
    relations: list[_FrozenRelation]


@dataclass
class _FrozenTableStructure:
    raw_rows: list[list[Any]]
    header: list[Any]
    source_table_id: str
    column_headers: list[str]


def _load_frozen_module(source_name: str) -> types.ModuleType:
    """Execute one frozen source file with only its model import surface."""
    names = (
        "esg_pipeline",
        "esg_pipeline.parser",
        "esg_pipeline.schemas",
        "esg_pipeline.research_v4",
        "esg_pipeline.research_v4.document_graph",
        "esg_pipeline.research_v4.table_reconstruction",
    )
    saved = {name: sys.modules.get(name) for name in names}
    package = types.ModuleType("esg_pipeline")
    research = types.ModuleType("esg_pipeline.research_v4")
    graph = types.ModuleType("esg_pipeline.research_v4.document_graph")
    setattr(graph, "GraphBlock", _FrozenBlock)
    setattr(graph, "BlockRelation", _FrozenRelation)
    setattr(graph, "DocumentGraph", _FrozenGraph)
    setattr(graph, "TableStructure", _FrozenTableStructure)
    setattr(graph, "GraphBlockType", str)
    setattr(graph, "ParserRunMetadata", object)
    parser = types.ModuleType("esg_pipeline.parser")
    setattr(parser, "parse_pdf", lambda _path: [])
    schemas = types.ModuleType("esg_pipeline.schemas")
    setattr(schemas, "ParsedBlock", object)
    reconstruction = types.ModuleType("esg_pipeline.research_v4.table_reconstruction")
    setattr(reconstruction, "reconstruct_table_blocks", lambda _blocks: [])
    sys.modules.update(
        {
            "esg_pipeline": package,
            "esg_pipeline.parser": parser,
            "esg_pipeline.schemas": schemas,
            "esg_pipeline.research_v4": research,
            "esg_pipeline.research_v4.document_graph": graph,
            "esg_pipeline.research_v4.table_reconstruction": reconstruction,
        }
    )
    try:
        path = _require_snapshot() / source_name
        assert hashlib.sha256(path.read_bytes()).hexdigest() == FROZEN_SHA256[source_name]
        spec = importlib.util.spec_from_file_location(f"task_043_{source_name}", path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for name, prior in saved.items():
            if prior is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = prior


def _frozen_graph(*, blocks: list[_FrozenBlock], relations: list[_FrozenRelation]) -> _FrozenGraph:
    return _FrozenGraph(
        report_id="r", source_pdf="frozen.pdf", parser_runs=[], blocks=blocks, relations=relations
    )


# ---------------------------------------------------------------------------
# Frozen SHA / read-only snapshot checks
# ---------------------------------------------------------------------------


def test_legacy_commit_is_frozen_and_matches_snapshot():
    _require_snapshot()
    commit_file = REPO_ROOT / "legacy_reference" / "COMMIT"
    assert commit_file.exists(), "legacy_reference/COMMIT must exist"
    first_line = commit_file.read_text().splitlines()[0].strip()
    assert first_line == LEGACY_COMMIT, f"COMMIT mismatch: {first_line}"
    meta = REPO_ROOT / "legacy_reference" / "commit_meta.json"
    assert meta.exists()
    data = json.loads(meta.read_text())
    assert data["sha"] == LEGACY_COMMIT
    for name, sha in FROZEN_SHA256.items():
        p = LEGACY_SNAPSHOT / name
        assert p.exists(), f"missing snapshot {name}"
        got = hashlib.sha256(p.read_bytes()).hexdigest()
        assert got == sha, f"sha mismatch for {name}: {got} != {sha}"


def test_legacy_snapshot_files_cover_required_originals():
    _require_snapshot()
    for name in [
        "run_signature.py",
        "graph_ensemble.py",
        "document_graph.py",
        "parser_adapters.py",
    ]:
        assert (LEGACY_SNAPSHOT / name).exists()


# ---------------------------------------------------------------------------
# R-01: dangling edge A1/B1/B2 — FX-GRAPH-001
# ---------------------------------------------------------------------------


def test_r01_legacy_dangling_edge_characterization():
    """Actual frozen merge leaves B2 -> dropped B1 dangling (FX-GRAPH-001)."""
    legacy = _load_frozen_module("graph_ensemble.py")
    graph_a = _frozen_graph(
        blocks=[_FrozenBlock("A1", 1, "table", "합계 1,234", (0, 0, 100, 20), "pymupdf")],
        relations=[],
    )
    graph_b = _frozen_graph(
        blocks=[
            _FrozenBlock("B1", 1, "table", "합계 1,234", (0, 0, 100, 20), "pdfplumber"),
            _FrozenBlock("B2", 1, "table_row", "세부", (0, 20, 100, 40), "pdfplumber"),
        ],
        relations=[_FrozenRelation("derived_from", "B2", "B1")],
    )
    merged = legacy.merge_document_graphs([graph_a, graph_b])
    assert [block.block_id for block in merged.blocks] == ["A1", "B2"]
    assert [(edge.source_block_id, edge.target_block_id) for edge in merged.relations] == [
        ("B2", "B1")
    ]
    assert {edge.target_block_id for edge in merged.relations} - {
        block.block_id for block in merged.blocks
    } == {"B1"}


# ---------------------------------------------------------------------------
# R-02: same string but different region must NOT be merged (FX-GRAPH-002)
# ---------------------------------------------------------------------------


def test_r02_legacy_text_key_merges_distinct_regions_bug():
    """Actual frozen merge drops non-overlapping same-text table blocks (FX-GRAPH-002)."""
    legacy = _load_frozen_module("graph_ensemble.py")
    graph = _frozen_graph(
        blocks=[
            _FrozenBlock("T1", 5, "table", "합계", (10, 10, 50, 30), "pymupdf"),
            _FrozenBlock("T2", 5, "table", "합계", (200, 10, 250, 30), "pymupdf"),
        ],
        relations=[],
    )
    merged = legacy.merge_document_graphs([graph])
    assert [block.block_id for block in merged.blocks] == ["T1"]


# ---------------------------------------------------------------------------
# R-03: bbox fallback to (0,0,0,0) is prohibited — must be null/unlocated
# ---------------------------------------------------------------------------


def test_r03_legacy_bbox_fallback_is_zero_box():
    legacy = _load_frozen_module("parser_adapters.py")
    block = legacy._graph_block_from_table_record(
        {"page_num": 1, "table_index": 0, "rows": [["metric", "value"]]},
        source_parser="pdfplumber",
    )
    assert block.bbox == (0.0, 0.0, 0.0, 0.0)


def test_r03_new_contract_bbox_null_unlocated():
    """New domain SourceRef must represent missing bbox as None + unlocated, never zero bbox."""
    from proofops.domain.values import DomainValidationError, SourceRef

    sid = "00000000-0000-4000-8000-000000000001"
    dv = "00000000-0000-4000-8000-000000000002"
    pm = "00000000-0000-4000-8000-000000000003"
    h = "a" * 64
    ref = SourceRef(
        source_id=sid,
        document_version_id=dv,
        parse_manifest_id=pm,
        page_num=1,
        printed_page_label=None,
        bbox=None,
        raw_text_sha256=h,
        quote="표",
        char_start=0,
        char_end=1,
        location_quality="unlocated",
        verification_state="candidate",
    )
    assert ref.bbox is None
    with pytest.raises(DomainValidationError):
        SourceRef(
            source_id=sid,
            document_version_id=dv,
            parse_manifest_id=pm,
            page_num=1,
            printed_page_label=None,
            bbox=(0.0, 0.0, 0.0, 0.0),
            raw_text_sha256=h,
            quote="표",
            char_start=0,
            char_end=1,
            location_quality="located",
            verification_state="candidate",
        )


# ---------------------------------------------------------------------------
# R-04: raw policy / gold label / scorer must NOT be imported
# ---------------------------------------------------------------------------


def test_r04_no_raw_policy_gold_label_or_scorer_imported():
    forbidden = [
        "evidence_grader",
        "threshold_policy",
        "implementation_policy",
        "gold_backtest",
        "label_rules",
        "scorer_code",
    ]
    pkg_files = list(PACKAGES_ROOT.rglob("*.py"))
    haystack = ""
    for p in pkg_files:
        haystack += p.read_text(encoding="utf-8", errors="ignore") + "\n"
    for term in forbidden:
        assert term not in haystack, f"forbidden raw import '{term}' found in packages/proofops"
    # Guard runtime imports — ignore benign ignore-dir strings  # noqa: E501
    import_like = re.compile(
        r"^\s*(?:import|from)\s+.*legacy_reference|from\s+.*esg_pipeline\.research_v4", re.MULTILINE
    )
    snapshot_ref = re.compile(r"legacy_reference/snapshot")
    for p in pkg_files:
        text = p.read_text(encoding="utf-8", errors="ignore")
        # supply_chain.py legitimately lists legacy_reference in _IGNORE_DIRS
        if p.name == "supply_chain.py":
            # still forbid actual import, but allow the ignore-set string
            assert not import_like.search(text), f"forbidden import in {p}"
            continue
        assert "esg_pipeline.research_v4" not in text
        assert not import_like.search(text), f"forbidden legacy import in {p}"
        assert not snapshot_ref.search(text), f"forbidden snapshot path in {p}"
    assert not (PACKAGES_ROOT / "gold").exists()
    assert (
        not (PACKAGES_ROOT / "standards" / "v4").exists()
        if (PACKAGES_ROOT / "standards").exists()
        else True
    )


def test_r04_evidence_grader_not_migrated_as_rule():
    content = (_require_snapshot() / "run_signature.py").read_text()
    assert "scorer_code_sha256" in content
    # New runtime must not contain grading fields
    pkg_files = list(PACKAGES_ROOT.rglob("*.py"))
    for p in pkg_files:
        t = p.read_text(encoding="utf-8", errors="ignore")
        # LlmTags boundary must not carry grade/label (enforced in values.py)
        if "proofops/domain/values" in str(p):
            continue
        assert "scorer_code" not in t
    from proofops.domain import values

    payload = json.loads((REPO_ROOT / "fixtures" / "llm_tags_example.json").read_text())
    payload["evidence_grade"] = "E3"
    with pytest.raises(Exception):
        values.llm_tags_from_dict(payload)


# ---------------------------------------------------------------------------
# R-05: strict guard defaults false, replicate cache reuse
# ---------------------------------------------------------------------------


def test_r05_legacy_lacks_replicate_and_tenant_isolation():
    content = (_require_snapshot() / "run_signature.py").read_text()
    assert (
        "replicate_id" not in content
    ), "legacy run_signature should not already contain replicate_id (R-05 gap)"
    assert "tenant" not in content.lower() or "tenant_id" not in content


def test_r05_new_domain_enforces_replicate_isolation_via_tags():
    """LlmTags replicate_id 1/2/3 are distinct; reuse without replicate leaks must be detectable."""
    from proofops.domain import values

    base = json.loads((REPO_ROOT / "fixtures" / "llm_tags_example.json").read_text())
    t1 = values.llm_tags_from_dict({**base, "replicate_id": 1})
    t2 = values.llm_tags_from_dict({**base, "replicate_id": 2})
    t3 = values.llm_tags_from_dict({**base, "replicate_id": 3})
    assert t1.replicate_id != t2.replicate_id
    assert t2.replicate_id != t3.replicate_id
    # Same packet_sha256 but different replicate_id => distinct tags (no silent cache reuse)
    assert t1.packet_sha256 == t2.packet_sha256
    assert t1.replicate_id != t2.replicate_id
    with pytest.raises(Exception):
        values.llm_tags_from_dict({**base, "replicate_id": 4})


def test_synthetic_adapter_explicitly_marked():
    from proofops.composition import build_local_composition

    comp = build_local_composition(app_env="local", model_adapter="synthetic")
    assert comp.binding.synthetic is True
    assert comp.profile == "local-synthetic"


def test_source_less_present_is_rejected():
    from proofops.domain.values import DomainValidationError, LlmElement

    with pytest.raises(DomainValidationError, match="source-less present"):
        LlmElement(
            element_id="G1",
            state="present",
            evidence_refs=(),
            normalized_value=None,
            credited_from=None,
            reason_code=None,
        )


def test_unknown_not_cast_to_absent():
    from proofops.domain.values import LlmElement

    el = LlmElement(
        element_id="G3",
        state="unknown",
        evidence_refs=(),
        normalized_value=None,
        credited_from=None,
        reason_code=None,
    )
    assert el.state == "unknown"


def test_llm_grade_rejected_by_boundary():
    from proofops.domain import values

    payload = json.loads((REPO_ROOT / "fixtures" / "llm_tags_example.json").read_text())
    payload["label"] = "SUBSTANTIATED"
    with pytest.raises(Exception):
        values.llm_tags_from_dict(payload)
    payload2 = json.loads((REPO_ROOT / "fixtures" / "llm_tags_example.json").read_text())
    payload2["sublabel"] = "PERF"
    with pytest.raises(Exception):
        values.llm_tags_from_dict(payload2)


def test_overwritten_revision_is_prohibited_via_frozen_domain():
    import json

    payload = json.loads((REPO_ROOT / "fixtures" / "llm_tags_example.json").read_text())
    from proofops.domain import values

    tags = values.llm_tags_from_dict(payload)
    with pytest.raises(Exception):
        tags.replicate_id = 2  # type: ignore[misc]
