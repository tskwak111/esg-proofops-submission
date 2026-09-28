"""Report-level source attestation and receipt replay."""

import json
import sqlite3
from dataclasses import asdict, replace
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from proofops.adapters.local.run_store import LocalSQLiteRunStore
from proofops.application.evidence.report_level import (
    GRI_ASSURED_PAGE_V1,
    REPORT_SCOPE_V1,
)
from proofops.application.evidence.retrieval import freeze_packet
from proofops.application.evidence.span_citations import span_verified_graph
from proofops.application.rescores import RescoreRejected, RescoreService
from proofops.application.reviews import ReviewRejected
from proofops.application.tagging.consensus import form_consensus
from proofops.domain.provenance import canonical_hash
from proofops.domain.rules.engine import MAPPINGS, Decision, evaluate
from proofops.domain.values import LlmElement

from tests.acceptance.test_citations import RUN, TENANT
from tests.acceptance.test_reviews import workspace
from tests.integration.test_ai_delegated_review import _actor


def _setup_report_level_case(inputs, *, m2=False):
    claim = inputs.context.claim
    base_block = next(
        block
        for block in inputs.original.blocks
        if block.source_id == claim.source_refs[0].source_id
    )
    cand = base_block.candidates[0]
    src = cand.source
    claim_quote = "환경 관련 관리체계를 운영합니다" if m2 else src.raw_text
    claim_cand = replace(
        cand,
        source=replace(
            src, printed_page_label="85", raw_text=claim_quote, char_end=len(claim_quote)
        ),
    )
    claim_block = replace(base_block, candidates=(claim_cand,))
    text230 = "GRI Index\n3-3 중대 토픽 관리 83-95"
    src230 = replace(
        src,
        source_native_id="r38-index",
        physical_page=230,
        printed_page_label="230",
        raw_text=text230,
        char_start=0,
        char_end=len(text230),
    )
    cand230 = replace(cand, kind="paragraph", source=src230)
    p230_block = replace(
        base_block,
        source_id=str(uuid5(NAMESPACE_URL, "r38-index")),
        kind="paragraph",
        candidates=(cand230,),
        quality="unverified",
    )
    text242 = "제3자 검증의견서\n중대성 주제 3-1 ~ 3-3\nAA1000AS v3"
    src242 = replace(
        src,
        source_native_id="r38-assurance",
        physical_page=242,
        printed_page_label="242",
        raw_text=text242,
        char_start=0,
        char_end=len(text242),
    )
    cand242 = replace(cand, kind="paragraph", source=src242)
    p242_block = replace(
        base_block,
        source_id=str(uuid5(NAMESPACE_URL, "r38-assurance")),
        kind="paragraph",
        candidates=(cand242,),
        quality="unverified",
    )
    scope_text = "보고 범위\n네이버 주식회사 개별 기업을 기준으로 작성"
    scope_source = replace(
        src,
        source_native_id="r38-scope",
        physical_page=2,
        printed_page_label="2",
        raw_text=scope_text,
        char_start=0,
        char_end=len(scope_text),
    )
    scope_cand = replace(cand, kind="paragraph", source=scope_source)
    scope_block = replace(
        base_block,
        source_id=str(uuid5(NAMESPACE_URL, "r38-scope")),
        kind="paragraph",
        candidates=(scope_cand,),
        quality="unverified",
    )
    updated_original = replace(
        inputs.original,
        blocks=tuple(
            claim_block if block.source_id == base_block.source_id else block
            for block in inputs.original.blocks
        )
        + (p230_block, p242_block, scope_block),
        candidates=(
            *inputs.original.candidates,
            replace(
                inputs.original.candidates[0], blocks=(claim_cand, cand230, cand242, scope_cand)
            ),
        ),
    )

    def span(block, quote):
        start = block.raw_text.index(quote)
        return replace(
            block.source_ref(),
            quote=quote,
            char_start=start,
            char_end=start + len(quote),
            verification_state="candidate",
        )

    index_ref = span(p230_block, "3-3 중대 토픽 관리 83-95")
    coverage_ref = span(p242_block, "중대성 주제 3-1 ~ 3-3")
    standard_ref = span(p242_block, "AA1000AS v3")
    scope_ref = span(scope_block, "네이버 주식회사 개별 기업을 기준으로 작성")
    # Ensure claim refs have printed_page_label in range 83-95
    updated_claim = replace(
        claim,
        quote=claim_quote,
        source_refs=(replace(claim_block.source_ref(), verification_state="candidate"),),
    )

    allowed = [
        e["id"]
        for e in inputs.rulepack.file_content("rubric/elements.yaml")["elements"]
        if e["id"] in MAPPINGS["management"]
    ]
    orig_pkt_dict = inputs.original_packet.to_dict()
    orig_pkt_dict["track"] = "management"
    orig_pkt_dict["allowed_elements"] = allowed
    new_orig_pkt = freeze_packet({k: v for k, v in orig_pkt_dict.items() if k != "packet_sha256"})

    pkt_dict = inputs.packet.to_dict()
    pkt_dict["track"] = "management"
    pkt_dict["allowed_elements"] = allowed
    pkt_dict["graph_sha256"] = canonical_hash(asdict(updated_original))
    pkt_dict["retrieval_packet_sha256"] = new_orig_pkt.packet_sha256
    new_pkt = freeze_packet({k: v for k, v in pkt_dict.items() if k != "packet_sha256"})

    elements = tuple(
        LlmElement(e, "unknown", (), None, None, "synthetic-unresolved")
        for e in ("M1", "M2", "M3", "M4", "M5", "M6")
    )
    tag_runs = tuple(
        replace(
            run,
            packet_sha256=new_pkt.packet_sha256,
            graph_sha256=pkt_dict["graph_sha256"],
            guarded=replace(
                run.guarded,
                track="management",
                elements=elements,
                packet_sha256=new_pkt.packet_sha256,
            ),
        )
        for run in inputs.tag_runs
    )

    cons = form_consensus(
        tag_runs,
        packet=new_pkt,
        rulepack=inputs.rulepack,
        tenant_id=updated_claim.tenant_id,
        tag_revision=inputs.tag_revision,
    )
    new_rule_ctx = replace(inputs.rule_context, packet_sha256=new_pkt.packet_sha256)
    new_decision = (
        evaluate(cons.confirmed_tags, new_rule_ctx, inputs.rulepack)
        if cons.confirmed_tags is not None
        else None
    )

    updated_inputs = replace(
        inputs,
        original=updated_original,
        context=replace(inputs.context, claim=updated_claim),
        packet=new_pkt,
        original_packet=new_orig_pkt,
        tag_runs=tag_runs,
        consensus=cons,
        rule_context=new_rule_ctx,
        decision=new_decision,
    )
    return updated_inputs, (index_ref, coverage_ref, standard_ref, scope_ref)


def report_workspace(tmp_path, *, m2=False):
    refs = []

    def prepare(inputs):
        updated, found = _setup_report_level_case(inputs, m2=m2)
        refs.extend(found)
        return updated

    return workspace(tmp_path, prepare_inputs=prepare), tuple(refs)


def _unknown(element_id):
    return dict(
        element_id=element_id,
        state="unknown",
        evidence_refs=[],
        normalized_value=None,
        credited_from=None,
        reason_code="unknown",
    )


def _management_body(m3_element):
    return {
        "base_tag_revision": 1,
        "track": "management",
        "reason": "M3 보고서 전역 외부검증 인정",
        "elements": [
            _unknown("M1"),
            _unknown("M2"),
            m3_element,
            *(_unknown(f"M{i}") for i in range(4, 7)),
        ],
    }


@pytest.mark.parametrize("element_id", ["M2", "M3"])
def test_report_level_review_uses_pinned_snapshot_without_nested_sqlite_writer(
    tmp_path, element_id
):
    ws, (idx, cov, std, scope) = report_workspace(tmp_path, m2=element_id == "M2")
    service, review = ws[1], ws[3]
    run_store = LocalSQLiteRunStore(tmp_path / "state.sqlite")
    pinned = {"tenant_id": TENANT, "run_id": RUN, "claim_source_policy": {"reader": "pinned"}}
    with sqlite3.connect(run_store.path) as db:
        db.execute(
            "INSERT INTO run_snapshots VALUES (?, ?, ?)",
            (TENANT, RUN, json.dumps(pinned)),
        )
    service.load_run_snapshot = run_store.snapshot

    def attest(loaded, refs, *, pinned_run_snapshot=None, published_tag=None, replay_receipt=None):
        # The fallback reproduces the original nested BEGIN IMMEDIATE on a real SQLite DB.
        snapshot = pinned_run_snapshot or run_store.snapshot(TENANT, RUN)
        assert snapshot == pinned
        if replay_receipt is not None:
            if published_tag is None:
                with run_store.jobs._transaction():
                    pass
            assert published_tag["report_level_review"][0]["source_receipt"] == replay_receipt
        return (
            span_verified_graph(
                loaded.original,
                tuple(replace(ref, verification_state="verified") for ref in refs),
                "receipt",
            ),
            {"records": [{"status": "verified"} for _ in refs]},
        )

    service.verify_context_sources = attest
    refs = [scope] if element_id == "M2" else [idx, cov, std]
    body = _management_body(_unknown("M3"))
    body["elements"][1 if element_id == "M2" else 2] = dict(
        element_id=element_id,
        state="present",
        evidence_refs=[asdict(ref) for ref in refs],
        normalized_value=None,
        credited_from=scope.source_id if element_id == "M2" else cov.source_id,
        reason_code=REPORT_SCOPE_V1 if element_id == "M2" else GRI_ASSURED_PAGE_V1,
    )
    answer = service.resolve_ai_delegated_review(
        _actor(),
        review["review_id"],
        body,
        '"1"',
        str(uuid4()),
        delegated_reviewer="sqlite-regression",
        delegation_authority="test approved report-level review",
    )
    assert answer["review"]["status"] == "resolved"
    assert (
        service.store.history(TENANT, RUN, review["claim_id"])["tags"][-1]["report_level_review"][
            0
        ]["policy"]
        == body["elements"][1 if element_id == "M2" else 2]["reason_code"]
    )
    body["base_tag_revision"] = 2
    replayed = service.resolve_ai_delegated_review(
        _actor(),
        review["review_id"],
        body,
        '"2"',
        str(uuid4()),
        delegated_reviewer="sqlite-regression",
        delegation_authority="test approved report-level re-review",
        reopen=True,
    )
    assert replayed["new_tag_revision"] == 3


def test_review_report_scope_attests_unverified_span(tmp_path):
    ws, (*_, scope) = report_workspace(tmp_path, m2=True)
    service = ws[1]
    calls = []

    def attest(loaded, refs):
        calls.append(tuple(refs))
        return (
            span_verified_graph(
                loaded.original,
                tuple(replace(ref, verification_state="verified") for ref in refs),
                "receipt",
            ),
            {"records": [{"status": "verified"}]},
        )

    service.verify_context_sources = attest
    body = _management_body(_unknown("M3"))
    body["elements"][1] = dict(
        element_id="M2",
        state="present",
        evidence_refs=[asdict(scope)],
        normalized_value=None,
        credited_from=scope.source_id,
        reason_code=REPORT_SCOPE_V1,
    )
    result = service.resolve_review(_actor(), ws[3]["review_id"], body, '"1"', str(uuid4()))
    tag = service.store.history(TENANT, RUN, ws[3]["claim_id"])["tags"][-1]
    assert result["new_tag_revision"] == 2
    assert calls == [(scope,)]
    assert tag["report_level_review"][0]["policy"] == REPORT_SCOPE_V1


def test_review_report_level_attestation_success(tmp_path):
    ws, (idx, cov, std, _) = report_workspace(tmp_path)
    inputs = ws[2]
    service = ws[1]

    mock_receipt = {
        "schema": "claim_source_attestation_v1",
        "records": [
            {"ref": asdict(idx), "status": "verified"},
            {"ref": asdict(cov), "status": "verified"},
            {"ref": asdict(std), "status": "verified"},
        ],
        "artifact_sha256": "mock_hash_123",
    }

    def mock_verify_context(inp, refs):
        verified_spans = tuple(replace(r, verification_state="verified") for r in refs)
        scoped = span_verified_graph(inp.original, verified_spans, mock_receipt["artifact_sha256"])
        return scoped, mock_receipt

    service.verify_context_sources = mock_verify_context
    service.load_inputs = lambda tenant, run, claim: inputs

    m3_element = {
        "element_id": "M3",
        "state": "present",
        "evidence_refs": [asdict(idx), asdict(cov), asdict(std)],
        "normalized_value": None,
        "credited_from": cov.source_id,
        "reason_code": GRI_ASSURED_PAGE_V1,
    }
    body = _management_body(m3_element)
    actor = _actor()
    res = service.resolve_review(actor, ws[3]["review_id"], body, '"1"', str(uuid4()))
    assert res["review"]["status"] == "resolved"
    tag = service.store.history(TENANT, RUN, ws[3]["claim_id"])["tags"][-1]
    assert "report_level_review" in tag
    rl = tag["report_level_review"]
    assert len(rl) == 1
    assert rl[0]["element_id"] == "M3"
    assert rl[0]["policy"] == GRI_ASSURED_PAGE_V1
    assert rl[0]["credited_from"] == cov.source_id
    assert canonical_hash(rl[0]["source_receipt"]) == canonical_hash(mock_receipt)


def test_review_report_level_without_verifier_rejected(tmp_path):
    ws, (idx, cov, std, _) = report_workspace(tmp_path)
    inputs = ws[2]
    service = ws[1]
    service.verify_context_sources = None
    service.load_inputs = lambda tenant, run, claim: inputs

    m3_element = {
        "element_id": "M3",
        "state": "present",
        "evidence_refs": [asdict(idx), asdict(cov), asdict(std)],
        "normalized_value": None,
        "credited_from": cov.source_id,
        "reason_code": GRI_ASSURED_PAGE_V1,
    }
    body = _management_body(m3_element)
    actor = _actor()
    with pytest.raises(ReviewRejected):
        service.resolve_review(actor, ws[3]["review_id"], body, '"1"', str(uuid4()))


def test_review_report_level_tampered_ref_rejected(tmp_path):
    ws, (idx, cov, std, _) = report_workspace(tmp_path)
    inputs = ws[2]
    service = ws[1]

    def mock_verify_context(inp, refs):
        raise ValueError("CONTEXT_SOURCE_REJECTED")

    service.verify_context_sources = mock_verify_context
    service.load_inputs = lambda tenant, run, claim: inputs

    m3_element = {
        "element_id": "M3",
        "state": "present",
        "evidence_refs": [asdict(idx), asdict(cov), asdict(std)],
        "normalized_value": None,
        "credited_from": cov.source_id,
        "reason_code": GRI_ASSURED_PAGE_V1,
    }
    body = _management_body(m3_element)
    actor = _actor()
    with pytest.raises(ReviewRejected):
        service.resolve_review(actor, ws[3]["review_id"], body, '"1"', str(uuid4()))
    assert len(service.store.history(TENANT, RUN, ws[3]["claim_id"])["tags"]) == 1


def test_scoped_graph_does_not_admit_a_forged_quote(tmp_path):
    ws, (idx, cov, std, _) = report_workspace(tmp_path)
    service = ws[1]

    def attest(inputs, refs):
        return (
            span_verified_graph(
                inputs.original,
                tuple(replace(ref, verification_state="verified") for ref in refs),
                "receipt",
            ),
            {"records": [{"status": "verified"}]},
        )

    service.verify_context_sources = attest
    body = _management_body(
        dict(
            element_id="M3",
            state="present",
            evidence_refs=[asdict(replace(idx, quote="forged")), asdict(cov), asdict(std)],
            normalized_value=None,
            credited_from=cov.source_id,
            reason_code=GRI_ASSURED_PAGE_V1,
        )
    )
    with pytest.raises(ReviewRejected, match="SOURCE_REJECTED"):
        service.resolve_review(_actor(), ws[3]["review_id"], body, '"1"', str(uuid4()))
    assert len(service.store.history(TENANT, RUN, ws[3]["claim_id"])["tags"]) == 1


def test_re_review_replay_success_and_tamper_rejected(tmp_path):
    ws, (idx, cov, std, _) = report_workspace(tmp_path)
    inputs = ws[2]
    service = ws[1]

    mock_receipt = {
        "schema": "context_source_attestation_v1",
        "records": [
            {"ref": asdict(idx), "status": "verified"},
            {"ref": asdict(cov), "status": "verified"},
            {"ref": asdict(std), "status": "verified"},
        ],
        "receipts": {"table": {"artifact_sha256": "table_hash_123"}},
        "artifact_sha256": "mock_hash_123",
    }

    def mock_verify_context(inp, refs):
        verified_spans = tuple(replace(r, verification_state="verified") for r in refs)
        scoped = span_verified_graph(inp.original, verified_spans, mock_receipt["artifact_sha256"])
        return scoped, mock_receipt

    service.verify_context_sources = mock_verify_context
    service.load_inputs = lambda tenant, run, claim: inputs

    m3_element = {
        "element_id": "M3",
        "state": "present",
        "evidence_refs": [asdict(idx), asdict(cov), asdict(std)],
        "normalized_value": None,
        "credited_from": cov.source_id,
        "reason_code": GRI_ASSURED_PAGE_V1,
    }
    body = _management_body(m3_element)
    actor = _actor()
    first = service.resolve_review(actor, ws[3]["review_id"], body, '"1"', str(uuid4()))

    # Re-review with carried element unchanged
    re_body = dict(body, base_tag_revision=first["new_tag_revision"])
    second = service.resolve_review(
        actor,
        ws[3]["review_id"],
        re_body,
        f'"{first["review"]["revision"]}"',
        str(uuid4()),
        reopen=True,
    )
    assert second["review"]["revision"] == 3

    before = service.store.history(TENANT, RUN, ws[3]["claim_id"])
    mock_receipt["receipts"]["table"]["artifact_sha256"] = "changed_table_attestation"
    third_body = dict(body, base_tag_revision=second["new_tag_revision"])
    with pytest.raises(ReviewRejected, match="REPORT_LEVEL_SOURCE_REPLAY_MISMATCH"):
        service.resolve_review(
            actor,
            ws[3]["review_id"],
            third_body,
            f'"{second["review"]["revision"]}"',
            str(uuid4()),
            reopen=True,
        )
    assert service.store.history(TENANT, RUN, ws[3]["claim_id"]) == before


def test_rescore_replays_report_level_receipt_and_rejects_tamper(tmp_path, monkeypatch):
    ws, (idx, cov, std, _) = report_workspace(tmp_path)
    inputs = ws[2]
    service = ws[1]

    mock_receipt = {
        "schema": "context_source_attestation_v1",
        "records": [
            {"ref": asdict(idx), "status": "verified"},
            {"ref": asdict(cov), "status": "verified"},
            {"ref": asdict(std), "status": "verified"},
        ],
        "receipts": {"table": {"artifact_sha256": "table_hash_123"}},
        "artifact_sha256": "mock_hash_123",
    }

    def mock_verify_context(inp, refs):
        verified_spans = tuple(replace(r, verification_state="verified") for r in refs)
        scoped = span_verified_graph(inp.original, verified_spans, mock_receipt["artifact_sha256"])
        return scoped, mock_receipt

    service.verify_context_sources = mock_verify_context
    service.load_inputs = lambda tenant, run, claim: inputs

    m3_element = {
        "element_id": "M3",
        "state": "present",
        "evidence_refs": [asdict(idx), asdict(cov), asdict(std)],
        "normalized_value": None,
        "credited_from": cov.source_id,
        "reason_code": GRI_ASSURED_PAGE_V1,
    }
    body = _management_body(m3_element)
    actor = _actor()
    service.resolve_review(actor, ws[3]["review_id"], body, '"1"', str(uuid4()))
    history = service.store.history(TENANT, RUN, ws[3]["claim_id"])
    tag = history["tags"][-1]

    class CapturedStore:
        def capture(self, actor, run, body, key, expected):
            return dict(
                target_pack=asdict(inputs.rulepack),
                run_snapshot={"rulepack": {"sha256": inputs.rulepack.sha256}},
                run={"document_version_id": inputs.original.document_version_id},
                claims={
                    ws[3]["claim_id"]: dict(
                        tag=tag,
                        head={"tag_revision": 2, "decision_revision": 1},
                        decision=history["decisions"][-1],
                    )
                },
            )

        def commit(self, actor, run, body, captured, prepared):
            return prepared

    # Rule-engine compatibility is covered by the existing rescore tests; this
    # captured store isolates the additional source replay before commit.
    import proofops.application.rescores as rescores

    prior_decision = Decision(**history["decisions"][-1]["decision"])
    monkeypatch.setattr(
        rescores,
        "create_rescore",
        lambda *args, **kwargs: replace(prior_decision, decision_revision=2),
    )
    rescore_service = RescoreService(
        CapturedStore(),
        load_inputs=lambda tenant, run, claim: inputs,
        verify_context_sources=mock_verify_context,
    )

    # Call rescore
    rescore_body = {"rule_pack_id": inputs.rulepack.rule_pack_id, "reason": "정상 재채점"}
    rescore_result = rescore_service.create_rescore(actor, RUN, rescore_body, str(uuid4()))
    assert ws[3]["claim_id"] in rescore_result

    tag["report_level_review"][0]["source_receipt"]["receipts"]["table"]["artifact_sha256"] = (
        "tampered_rescore"
    )

    with pytest.raises(RescoreRejected, match="RESCORE_SOURCE_REJECTED"):
        rescore_service.create_rescore(actor, RUN, rescore_body, str(uuid4()))
