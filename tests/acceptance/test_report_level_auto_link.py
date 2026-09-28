"""Run-pinned M3 linking keeps the source and policy gates of reviews."""

import json
from dataclasses import asdict, replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from proofops.application import reviews as review_module
from proofops.application.evidence.span_citations import span_verified_graph
from proofops.application.reviews import ReviewRejected
from proofops.application.tagging.consensus import PARTIAL_FACTS_V1, form_consensus
from proofops.application.tagging.report_level_link import (
    POLICY,
    POLICY_HASH,
    apply_report_level_link,
    replay_report_level_link,
    strict_fallback,
    validate_config,
)
from proofops.domain.provenance import canonical_hash
from proofops.domain.values import SourceRef
from proofops_worker import tag_runner
from proofops_worker.tag_runner import _review_decision

from tests.acceptance.test_report_level_verification import _setup_report_level_case
from tests.acceptance.test_reviews import workspace


def _case(tmp_path):
    original = workspace(tmp_path)
    inputs, refs = _setup_report_level_case(original[2], m2=True)
    consensus = form_consensus(
        inputs.tag_runs,
        packet=inputs.packet,
        rulepack=inputs.rulepack,
        tenant_id=inputs.context.claim.tenant_id,
        tag_revision=1,
        profile=PARTIAL_FACTS_V1,
    )
    config = {
        "policy": POLICY,
        "policy_hash": POLICY_HASH,
        "refs": {"M3": [asdict(ref) for ref in refs[:3]]},
    }
    return inputs, consensus, config


def _attest(inputs, *, unverified=False):
    def verify(refs):
        receipt = {"schema": "test_source_attestation", "records": [asdict(r) for r in refs]}
        receipt["artifact_sha256"] = canonical_hash(receipt)
        if unverified:
            return inputs.original, receipt
        return span_verified_graph(
            inputs.original,
            tuple(replace(ref, verification_state="verified") for ref in refs),
            receipt["artifact_sha256"],
        ), receipt

    return verify


def test_new_policy_accepts_only_m3(tmp_path):
    inputs, _, config = _case(tmp_path)
    with pytest.raises(ValueError, match="M3_ONLY"):
        validate_config(
            config | {"refs": {"M2": [asdict(_setup_report_level_case(inputs, m2=True)[1][3])]}}
        )
    validate_config(config | {"refs": {"M3": config["refs"]["M3"]}})


def test_link_verified_scope_and_assurance_and_replay(tmp_path):
    inputs, base, config = _case(tmp_path)
    linked, receipts = apply_report_level_link(
        base,
        config=config,
        context=inputs.context,
        graph=inputs.original,
        attest=_attest(inputs),
    )
    assert {r["element_id"] for r in receipts} == {"M3"}
    assert {e.element_id for e in linked.candidate_elements if e.state == "present"} >= {"M3"}
    assert linked.review_status == "needs_review"
    assert all(r["rule_hash"] == POLICY_HASH for r in receipts)
    assert (
        replay_report_level_link(
            base,
            config=config,
            context=inputs.context,
            graph=inputs.original,
            receipts=receipts,
        )
        == linked
    )
    assert (
        replay_report_level_link(
            base,
            config=json.loads(json.dumps(config)),
            context=inputs.context,
            graph=inputs.original,
            receipts=receipts,
        )
        == linked
    )
    assert (
        replay_report_level_link(
            base,
            config=json.loads(json.dumps(config)),
            context=inputs.context,
            graph=inputs.original,
            receipts=json.loads(json.dumps(receipts)),
        )
        == linked
    )
    tampered = [dict(receipts[0], rule_hash="0" * 64)]
    with pytest.raises(ValueError, match="REPLAY_MISMATCH"):
        replay_report_level_link(
            base,
            config=config,
            context=inputs.context,
            graph=inputs.original,
            receipts=tampered,
        )


def test_partial_link_cannot_finalize_a_completed_ladder(tmp_path):
    inputs, base, config = _case(tmp_path)
    only_link_missing = replace(
        base,
        candidate_elements=tuple(
            e
            if e.element_id == "M3"
            else replace(e, state="present", evidence_refs=inputs.context.claim.source_refs)
            for e in base.candidate_elements
        ),
        reasons=("REVIEW:M3",),
    )
    linked, receipts = apply_report_level_link(
        only_link_missing,
        config=config,
        context=inputs.context,
        graph=inputs.original,
        attest=_attest(inputs),
    )
    assert {r["element_id"] for r in receipts} == {"M3"}
    assert linked.review_status == "needs_review"
    assert linked.reasons == ("PARTIAL_FACTS_REVIEW_REQUIRED",)
    assert linked.confirmed_tags is not None
    assert _review_decision(
        SimpleNamespace(decision_status="decided", evidence_grade="E3"),
        PARTIAL_FACTS_V1,
        "needs_review",
    ) == (None, "E3")
    assert (
        replay_report_level_link(
            only_link_missing,
            config=config,
            context=inputs.context,
            graph=inputs.original,
            receipts=receipts,
        )
        == linked
    )
    strict, strict_receipts = apply_report_level_link(
        replace(only_link_missing, confirmed_tags=None),
        config=config,
        context=inputs.context,
        graph=inputs.original,
        attest=_attest(inputs),
        fallback_tags=base.confirmed_tags,
    )
    assert {r["element_id"] for r in strict_receipts} == {"M3"}
    assert strict.confirmed_tags is None
    assert strict.review_status == "needs_review"


def test_partial_decided_receipt_is_rejected_and_carry_stays_in_review(tmp_path, monkeypatch):
    from proofops.domain.rules.engine import evaluate

    inputs, base, _ = _case(tmp_path)
    decision = replace(
        evaluate(base.confirmed_tags, inputs.rule_context, inputs.rulepack),
        decision_status="decided",
        evidence_grade="E3",
        label="SUBSTANTIATED",
    )
    prior = replace(
        inputs, consensus=base, fact_assembly_profile=PARTIAL_FACTS_V1, decision=decision
    )
    monkeypatch.setattr(review_module, "evaluate", lambda *_: decision)
    with pytest.raises(ReviewRejected, match="PARTIAL_FACTS_REVIEW_REQUIRED"):
        prior.validate()
    replace(prior, decision=None).validate()
    record = tag_runner._prior_claim_record(prior.context.claim.claim_id, prior, False)
    assert record["status"] == "needs_review"
    assert record["decision"] is None
    assert record["candidate_grade"] == "E3"


def test_prior_revision_uses_run_profile_when_stored_profile_is_missing(tmp_path):
    from proofops.domain.rules.engine import evaluate

    inputs, base, _ = _case(tmp_path)
    decision = replace(
        evaluate(base.confirmed_tags, inputs.rule_context, inputs.rulepack),
        decision_status="decided",
        evidence_grade="E3",
        label="SUBSTANTIATED",
    )
    prior = replace(
        inputs,
        consensus=replace(base, review_status="auto_confirmed"),
        fact_assembly_profile="strict-v1",
        decision=decision,
    )
    assert "fact_assembly" not in prior.snapshot()

    record = tag_runner._prior_claim_record(
        prior.context.claim.claim_id, prior, False, pinned_profile=PARTIAL_FACTS_V1
    )
    assert record["status"] == "needs_review"
    assert record["decision"] is None
    assert record["candidate_grade"] == "E3"


def test_link_rejects_out_of_range_own_scope_and_unverified(tmp_path):
    inputs, base, config = _case(tmp_path)
    out_of_range = replace(
        inputs.context,
        claim=replace(
            inputs.context.claim,
            source_refs=(replace(inputs.context.claim.source_refs[0], printed_page_label="96"),),
        ),
    )
    result, receipts = apply_report_level_link(
        base,
        config=config,
        context=out_of_range,
        graph=inputs.original,
        attest=_attest(inputs),
    )
    assert not any(r["element_id"] == "M3" for r in receipts)
    result, receipts = apply_report_level_link(
        base,
        config=config,
        context=inputs.context,
        graph=inputs.original,
        attest=_attest(inputs, unverified=True),
    )
    assert receipts == ()
    assert result == base


def test_link_never_overrides_existing_or_accepts_unpinned_config(tmp_path):
    inputs, base, config = _case(tmp_path)
    candidates = tuple(
        replace(e, state="conflict") if e.element_id == "M3" else e for e in base.candidate_elements
    )
    facts = tuple(
        replace(f, state="conflict") if f.name == "external_verification" else f
        for f in base.confirmed_tags.facts
    )
    changed = replace(
        base,
        candidate_elements=candidates,
        confirmed_tags=replace(base.confirmed_tags, facts=facts),
    )
    linked, receipts = apply_report_level_link(
        changed,
        config=config,
        context=inputs.context,
        graph=inputs.original,
        attest=_attest(inputs),
    )
    assert not any(r["element_id"] == "M3" for r in receipts)
    assert next(e for e in linked.candidate_elements if e.element_id == "M3").state == "conflict"
    with pytest.raises(ValueError, match="CONFIG_INVALID"):
        validate_config(config | {"policy_hash": "0" * 64})
    with pytest.raises(ValueError, match="CONFIG_INVALID"):
        validate_config(
            config
            | {"refs": {"M3": [{**config["refs"]["M3"][0], "verification_state": "verified"}]}}
        )


def test_strict_profile_can_confirm_only_after_all_unknowns_are_linked(tmp_path):
    inputs, partial, config = _case(tmp_path)
    strict = form_consensus(
        inputs.tag_runs,
        packet=inputs.packet,
        rulepack=inputs.rulepack,
        tenant_id=inputs.context.claim.tenant_id,
        tag_revision=1,
        profile="strict-v1",
    )
    assert strict.confirmed_tags is None
    fallback = strict_fallback(
        inputs.tag_runs,
        inputs.packet,
        inputs.rulepack,
        inputs.context.claim.tenant_id,
        1,
        "strict-v1",
    )
    linked, receipts = apply_report_level_link(
        strict,
        config=config,
        context=inputs.context,
        graph=inputs.original,
        attest=_attest(inputs),
        fallback_tags=fallback,
    )
    assert {r["element_id"] for r in receipts} == {"M3"}
    assert linked.confirmed_tags is None  # M1/M4/M5/M6 remain unknown
    assert (
        replay_report_level_link(
            strict,
            config=config,
            context=inputs.context,
            graph=inputs.original,
            receipts=receipts,
            fallback_tags=fallback,
        )
        == linked
    )


def test_linked_initial_revision_keeps_rule_and_source_receipt(tmp_path):
    found = {}

    def prepare(inputs):
        updated, refs = _setup_report_level_case(inputs, m2=True)
        base = form_consensus(
            updated.tag_runs,
            packet=updated.packet,
            rulepack=updated.rulepack,
            tenant_id=updated.context.claim.tenant_id,
            tag_revision=1,
            profile=PARTIAL_FACTS_V1,
        )
        config = {
            "policy": POLICY,
            "policy_hash": POLICY_HASH,
            "refs": {"M3": [asdict(ref) for ref in refs[:3]]},
        }
        config = json.loads(json.dumps(config))
        linked, receipts = apply_report_level_link(
            base,
            config=config,
            context=updated.context,
            graph=updated.original,
            attest=_attest(updated),
        )
        found["receipts"] = receipts
        return replace(
            updated,
            consensus=linked,
            fact_assembly_profile=PARTIAL_FACTS_V1,
            report_level_link=config,
            report_level_review=receipts,
        )

    ws = workspace(tmp_path, prepare_inputs=prepare)
    tag = ws[1].store.history(
        ws[2].context.claim.tenant_id,
        ws[2].run_id,
        ws[2].context.claim.claim_id,
    )["tags"][0]
    assert canonical_hash(tag["report_level_review"]) == canonical_hash(found["receipts"])
    assert tag["report_level_link"]["policy_hash"] == POLICY_HASH
    ws[2].validate()


def test_run_creator_pins_operator_supplied_refs(tmp_path):
    from tests.acceptance.test_preflight import AUTH
    from tests.integration.test_live_tagging_runtime_config import _live_service

    service, body, _, _ = _live_service(tmp_path)
    ref = SourceRef(
        str(uuid4()),
        body["document_version_id"],
        str(uuid4()),
        2,
        "2",
        (1.0, 1.0, 2.0, 2.0),
        "0" * 64,
        "네이버 주식회사",
        0,
        8,
        "located",
        "candidate",
    )
    service.report_level_link = {
        "policy": POLICY,
        "policy_hash": POLICY_HASH,
        "refs": {"M2": [asdict(ref)]},
    }
    with pytest.raises(ValueError, match="M3_ONLY"):
        service.create(AUTH, body, str(uuid4()))
    service.report_level_link = {
        "policy": POLICY,
        "policy_hash": POLICY_HASH,
        "refs": {"M3": [asdict(ref), asdict(ref), asdict(ref)]},
    }
    run_id = service.create(AUTH, body, str(uuid4()))["run_id"]
    frozen = service.store.snapshot(AUTH.tenant_id, run_id)
    assert frozen["report_level_link"]["policy_hash"] == POLICY_HASH
    assert (
        frozen["report_level_link"]["refs"]["M3"][0]["document_version_id"]
        == body["document_version_id"]
    )


def test_replay_rejects_tampered_linked_element_even_if_facts_match(tmp_path):
    inputs, base, config = _case(tmp_path)
    linked, receipts = apply_report_level_link(
        base,
        config=config,
        context=inputs.context,
        graph=inputs.original,
        attest=_attest(inputs),
    )
    assert next(e for e in linked.candidate_elements if e.element_id == "M3").state == "present"
    tampered = replace(
        linked,
        candidate_elements=tuple(
            replace(e, state="unknown", evidence_refs=(), credited_from=None)
            if e.element_id == "M3"
            else e
            for e in linked.candidate_elements
        ),
    )
    with pytest.raises(ReviewRejected, match="REPORT_LEVEL_LINK_REPLAY_MISMATCH"):
        replace(
            inputs,
            consensus=tampered,
            fact_assembly_profile=PARTIAL_FACTS_V1,
            report_level_link=config,
            report_level_review=receipts,
        ).validate()


def test_live_worker_replays_pinned_link_config_without_minting_missing_source(
    tmp_path, monkeypatch
):
    from tests.integration import test_live_tagging_pipeline as pipeline

    original = pipeline._live_service

    def configured(path):
        service, body, preliminary, tagging = original(path)
        service.report_level_link = {
            "policy": POLICY,
            "policy_hash": POLICY_HASH,
            "refs": {"M3": [{"page_num": 2, "kind": "paragraph", "quote": "missing source"}] * 3},
        }
        return service, body, preliminary, tagging

    monkeypatch.setattr(pipeline, "_live_service", configured)
    ctx = pipeline._pipeline_setup(tmp_path, monkeypatch, fact_assembly_profile=PARTIAL_FACTS_V1)
    tenant, run_id, runner = ctx["tenant"], ctx["run_id"], ctx["tag_runner"]
    assert runner.run_once(tenant_id=tenant, run_id=run_id) == "needs_review"
    claim_id = runner.claims.list(tenant, run_id)[0].claim_id
    inputs = runner.tags.load_inputs(tenant, run_id, claim_id)
    assert inputs.report_level_link["policy_hash"] == POLICY_HASH
    assert inputs.report_level_review == ()
    assert (
        next(e for e in inputs.consensus.candidate_elements if e.element_id == "M3").state
        == "unknown"
    )


def test_run_pinned_locators_resolve_after_parse_and_replay(tmp_path):
    inputs, base, _ = _case(tmp_path)
    config = {
        "policy": POLICY,
        "policy_hash": POLICY_HASH,
        "refs": {
            "M3": [
                {"page_num": 230, "kind": "paragraph", "quote": "3-3 중대 토픽 관리 83-95"},
                {"page_num": 242, "kind": "paragraph", "quote": "중대성 주제 3-1 ~ 3-3"},
                {"page_num": 242, "kind": "paragraph", "quote": "AA1000AS v3"},
            ],
        },
    }
    linked, receipts = apply_report_level_link(
        base,
        config=config,
        context=inputs.context,
        graph=inputs.original,
        attest=_attest(inputs),
    )
    assert {r["element_id"] for r in receipts} == {"M3"}
    assert all(
        receipt["config_ref_hash"] == canonical_hash(config["refs"][receipt["element_id"]])
        for receipt in receipts
    )
    assert (
        replay_report_level_link(
            base,
            config=config,
            context=inputs.context,
            graph=inputs.original,
            receipts=receipts,
        )
        == linked
    )
    ambiguous = json.loads(json.dumps(config))
    ambiguous["refs"]["M3"][2]["quote"] = "3"
    _, rejected = apply_report_level_link(
        base,
        config=ambiguous,
        context=inputs.context,
        graph=inputs.original,
        attest=_attest(inputs),
    )
    assert not any(receipt["element_id"] == "M3" for receipt in rejected)
