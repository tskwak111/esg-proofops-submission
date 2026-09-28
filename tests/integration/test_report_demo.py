from copy import deepcopy
from dataclasses import asdict

import pytest
from proofops.application.claims import ExtractionProfile
from proofops.domain.provenance import canonical_hash

from evaluation.report_demo import build_demo, render_demo
from tests.acceptance.test_claims import COMPOUND, graph_of
from tests.acceptance.test_rules import pack
from tests.integration.test_section_pipeline import section_map


def inputs():
    graph = graph_of(COMPOUND, "사회 본문", "온실가스 배출량 2024 100 tCO2e", "보증 의견")
    block = graph.blocks[0]
    profile = ExtractionProfile("a" * 64, "b" * 64, "c" * 64, synthetic=True)
    packet = dict(
        tenant_id=graph.tenant_id,
        document_version_id=graph.document_version_id,
        parse_manifest_id=graph.parse_manifest_id,
        source_sha256=graph.source_sha256,
        extraction_profile=asdict(profile),
        untrusted_document_data=dict(
            source_id=block.source_id,
            page_num=block.page_num,
            kind=block.kind,
            text=block.normalized_text,
        ),
    )
    result = dict(
        packet_sha256=canonical_hash(packet),
        profile=asdict(profile),
        spans=[
            dict(
                char_start=0,
                char_end=len(COMPOUND),
                quote=COMPOUND,
                kind="claim",
                reason=None,
                topic_ids=["environment"],
            )
        ],
    )
    return graph, section_map(graph), [(packet, result)], pack()


def test_demo_connects_real_services_without_promoting_unverified_sources():
    graph, mapped, records, rulepack = inputs()
    demo = build_demo(graph, mapped, records, rulepack)
    assert len(demo["claims"]) == 1
    claim = demo["claims"][0]
    assert claim["quote"] == COMPOUND and claim["decision"] is None
    assert claim["retrieval"]["status"] == "blocked_evidence"
    assert claim["retrieval"]["search_coverage"]["not_found_state"] == "unknown"
    assert claim["search_candidates"]
    assert all(c["binding_status"] == "undetermined" for c in claim["search_candidates"])
    assert demo["coverage"]["missing_evidence_pages"] == [5]
    assert demo == build_demo(graph, mapped, records, rulepack)
    bad = deepcopy(records)
    bad[0][0]["source_sha256"] = "f" * 64
    with pytest.raises(ValueError):
        build_demo(graph, mapped, bad, rulepack)


def test_demo_escapes_report_text():
    graph, mapped, records, rulepack = inputs()
    demo = build_demo(graph, mapped, records, rulepack)
    demo["claims"][0]["quote"] = "<script>alert(1)</script>"
    html = render_demo(demo)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert "source.pdf#page=" in html


def test_agent_review_is_source_bound_display_only():
    from evaluation.report_demo import attach_agent_review

    demo = build_demo(*inputs())
    demo["artifact_sha256"] = canonical_hash(demo)
    original = deepcopy(demo)
    claim = demo["claims"][0]
    review = dict(
        reviewer_kind="agent",
        input_sha256=demo["artifact_sha256"],
        claims={
            claim["claim_id"]: dict(
                note="<b>문장 확인; 주장의 사실성 승인은 아님</b>",
                candidates={
                    c["source_id"]: dict(status="context", reason="문맥만 제공")
                    for c in claim["search_candidates"]
                },
            )
        },
        tables={},
    )
    reviewed = attach_agent_review(demo, review)
    assert demo == original
    assert reviewed["claims"] == original["claims"]
    assert reviewed["rule_evaluation_status"] == "not_run"
    assert reviewed["artifact_sha256"] != original["artifact_sha256"]
    html = render_demo(reviewed)
    assert "에이전트 검토" in html and "문맥만 제공" in html
    assert "&lt;b&gt;" in html and "<b>문장 확인" not in html
    for mutate in (
        lambda r: r.update(input_sha256="f" * 64),
        lambda r: r.update(reviewer_kind="human"),
        lambda r: r["claims"][claim["claim_id"]]["candidates"].clear(),
        lambda r: r["claims"][claim["claim_id"]]["candidates"].update(foreign={}),
    ):
        bad = deepcopy(review)
        mutate(bad)
        with pytest.raises(ValueError):
            attach_agent_review(demo, bad)
    changed = deepcopy(demo)
    changed["claims"][0]["quote"] = "다른 문장"
    with pytest.raises(ValueError):
        attach_agent_review(changed, review)


def test_table_notes_reach_value_review_without_cross_year_leak_or_approval():
    from dataclasses import asdict, replace
    from hashlib import sha256

    from proofops.application.ingest.graph_fusion import fuse_candidates

    from evaluation.html_table_cells import parse_table_cells
    from evaluation.report_demo import attach_table_notes
    from evaluation.table_notes import prepare, validate
    from evaluation.table_numeric_candidates import discover_table_candidates
    from tests.acceptance.test_parsing import TENANT, pdf
    from tests.acceptance.test_tables import table

    source = pdf()
    rows = [["지표", "단위", "2024", "2025"], ["배출량", "tCO2e", "1", "2"]]
    batch = table(rows)
    batch = replace(
        batch,
        source_sha256=sha256(source).hexdigest(),
        blocks=tuple(
            replace(
                b,
                table_native_id="table-1" if b.kind == "table_cell" else b.table_native_id,
                source=replace(
                    b.source,
                    source_native_id="table-1" if b.kind == "table" else b.source.source_native_id,
                ),
            )
            for b in batch.blocks
        ),
        edges=tuple(replace(e, target_native_id="table-1") for e in batch.edges),
    )
    graph = fuse_candidates((batch,), tenant_id=TENANT)
    tid = next(b.source_id for b in graph.blocks if b.kind == "table")
    packet = prepare(graph, source, [tid], tenant_id=TENANT)
    target = next(
        t["id"] for t in packet["untrusted_document_data"]["targets"] if t["text"] == "2024"
    )
    payload = dict(
        notes=[
            dict(
                fragment_ids=[packet["untrusted_document_data"]["fragments"][0]["id"]],
                target_ids=[target],
                kind="scope",
            )
        ]
    )
    notes = validate(payload, packet, graph, source, tenant_id=TENANT)
    notes.update(status="source_bound_proposals", error=None)
    html = (
        "<table>"
        + "".join("<tr>" + "".join("<td>" + v + "</td>" for v in row) + "</tr>" for row in rows)
        + "</table>"
    )
    demo = build_demo(*inputs())
    demo.update(
        tenant_id=TENANT,
        document_version_id=graph.document_version_id,
        source_sha256=graph.source_sha256,
    )
    demo["tables"] = [
        dict(id=1, page=1, cells=parse_table_cells(html), numeric=discover_table_candidates(html))
    ]
    demo["artifact_sha256"] = canonical_hash(demo)
    before = deepcopy(demo)
    attached = attach_table_notes(demo, graph, source, packet, notes)
    assert demo == before
    import json

    from proofops.adapters.local.table_notes import replay_note_reviews

    artifact = attached["runtime_note_review_artifacts"][0]
    view = replay_note_reviews((artifact,), graph, source, tenant_id=TENANT)
    review = attached["tables"][0]["note_review"]
    assert review["runtime_artifact_sha256"] == json.loads(artifact)["artifact_sha256"]
    assert review["runtime_graph_sha256"] == canonical_hash(asdict(view))
    assert review["runtime_issue_ids"] == [issue.issue_id for issue in view.issues]
    candidates = attached["tables"][0]["numeric"]["candidates"]
    assert [len(c["note_context"]["notes"]) for c in candidates] == [1, 0]
    assert all(c["note_context"]["numeric_status"] == "not_run" for c in candidates)
    assert all(c["note_context"]["coverage_status"] == "unknown" for c in candidates)
    assert all(c["note_context"]["admission_status"] == "blocked" for c in candidates)
    assert (
        attached["tables"][0]["note_review"]["claim_parse_manifest_id"] == demo["parse_manifest_id"]
    )
    assert attached["tables"][0]["note_review"]["parse_manifest_id"] == graph.parse_manifest_id
    assert attached["artifact_sha256"] != demo["artifact_sha256"]
    rendered = render_demo(attached)
    assert "각주 조건 검토 필요" in rendered and "2024" in rendered
    for bad in (dict(demo, tenant_id="foreign"), dict(demo, source_sha256="f" * 64)):
        with pytest.raises(ValueError):
            attach_table_notes(bad, graph, source, packet, notes)
    altered = deepcopy(demo)
    altered["tables"][0]["cells"][0]["text"] = "forged"
    altered["artifact_sha256"] = canonical_hash(
        {k: v for k, v in altered.items() if k != "artifact_sha256"}
    )
    with pytest.raises(ValueError):
        attach_table_notes(altered, graph, source, packet, notes)

    duplicated = deepcopy(demo)
    duplicated["tables"].append(deepcopy(duplicated["tables"][0]))
    duplicated["artifact_sha256"] = canonical_hash(
        {k: v for k, v in duplicated.items() if k != "artifact_sha256"}
    )
    with pytest.raises(ValueError):
        attach_table_notes(duplicated, graph, source, packet, notes)
    original_packet = deepcopy(packet)
    attached["tables"][0]["note_review"]["notes"][0]["source_fragments"][0]["text"] = "edited"
    assert packet == original_packet
    unassigned = validate(
        dict(notes=[dict(payload["notes"][0], target_ids=[])]),
        packet,
        graph,
        source,
        tenant_id=TENANT,
    )
    unassigned.update(status="invalid_or_failed", error="binding_failed")
    held = attach_table_notes(demo, graph, source, packet, unassigned)
    assert held["tables"][0]["note_review"]["extraction_status"] == "invalid_or_failed"
    assert all(c["note_context"]["notes"] == [] for c in held["tables"][0]["numeric"]["candidates"])
    for candidate in held["tables"][0]["numeric"]["candidates"]:
        context = candidate["note_context"]
        assert context["unassigned_notes"] == unassigned["notes"]
        assert context["reason"] == "footnote_ownership_unresolved"
        assert context["unassigned_notes"][0]["target_ids"] == []
        assert context["admission_status"] == "blocked"
    assert "귀속 미확정 각주 1개 별도 검토" in render_demo(held)
    assert render_demo(held).count("페이지의 귀속 미확정 각주") == 1
