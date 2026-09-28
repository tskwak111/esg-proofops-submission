"""R22 manual preliminary classification validator: literal source guards, no grade."""

from dataclasses import asdict

import pytest
from proofops.application.tagging.manual_classification import (
    AI_DELEGATED_ORIGIN,
    HUMAN_ORIGIN,
    SCHEMA,
    ClassificationRejected,
    classification_override,
    classification_snapshot,
    validate_manual_classification,
)
from proofops.application.tagging.tracks import TrackCandidate
from proofops.domain.provenance import canonical_hash

from tests.acceptance.test_binding import DIMENSIONS, corpus
from tests.acceptance.test_citations import OTHER, TENANT


def body(
    claim, *, track="performance", category=None, axes=("entity", "metric", "reporting_period")
):
    dimensions = {}
    for name in axes:
        text = DIMENSIONS[name]
        start = claim.quote.index(text)
        dimensions[name] = dict(source_index=0, start=start, end=start + len(text), quote=text)
    for required in ("entity", "metric", "reporting_period"):
        dimensions.setdefault(required, None)
    return dict(
        track=track,
        safe_harbor_category=category,
        dimensions=dimensions,
        reason="reviewer classified this claim as performance from the reported result",
    )


def test_manual_classification_verifies_literal_sources_and_forces_null_confidence():
    graph, claim, _ = corpus()
    before = asdict(graph), asdict(claim)
    result = validate_manual_classification(claim, graph, body(claim), tenant_id=TENANT)
    assert isinstance(result.track, TrackCandidate)
    assert result.track.track == "performance"
    # A manual judgement is never a model confidence or a 3-vote agreement.
    assert result.track_confidence is None
    assert result.context.claim == claim
    for name in ("entity", "metric", "reporting_period"):
        ref = result.context.dimensions[name]
        assert ref.quote == DIMENSIONS[name] and ref.verification_state == "verified"
    # Pure: the source graph and claim are untouched.
    assert (asdict(graph), asdict(claim)) == before


def test_null_axis_stays_unknown_not_a_fact():
    graph, claim, _ = corpus()
    raw = body(claim)
    raw["dimensions"] = dict(entity=None, metric=None, reporting_period=None)
    result = validate_manual_classification(claim, graph, raw, tenant_id=TENANT)
    assert result.context.dimensions == {"entity": None, "metric": None, "reporting_period": None}


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b.update(track="unknown"),
        lambda b: b.update(track="grade"),
        lambda b: b.update(safe_harbor_category="not_a_category"),
        lambda b: b.update(reason="x"),
        lambda b: b.__setitem__("confidence", 1.0),
        lambda b: b.__setitem__("origin", "human"),
        lambda b: b.__setitem__("grade", "E3"),
        lambda b: b["dimensions"].__setitem__("unknown_axis", None),
        lambda b: b["dimensions"].pop("entity"),
    ],
)
def test_rejects_bad_track_category_extra_keys_and_axes(mutate):
    graph, claim, _ = corpus()
    raw = body(claim)
    mutate(raw)
    with pytest.raises(ClassificationRejected) as exc:
        validate_manual_classification(claim, graph, raw, tenant_id=TENANT)
    assert exc.value.status in (409, 422)


def test_rejects_source_unverified_quote():
    graph, claim, _ = corpus()
    raw = body(claim)
    # A quote not present verbatim in the numbered source is ambiguous/absent.
    raw["dimensions"]["entity"] = dict(source_index=0, quote="이 회사는 존재하지 않는 문구")
    with pytest.raises(ClassificationRejected) as exc:
        validate_manual_classification(claim, graph, raw, tenant_id=TENANT)
    assert exc.value.code == "CLASSIFICATION_SOURCE_REJECTED"


def test_foreign_tenant_source_is_rejected():
    graph, claim, _ = corpus()
    with pytest.raises(ClassificationRejected):
        validate_manual_classification(claim, graph, body(claim), tenant_id=OTHER)


def test_snapshot_pins_lineage_and_records_provenance_without_grade():
    graph, claim, _ = corpus()
    result = validate_manual_classification(claim, graph, body(claim), tenant_id=TENANT)
    record = classification_snapshot(
        result,
        tenant_id=TENANT,
        run_id=claim.claim_id,  # any uuid; run id shape only
        claim=claim,
        graph=graph,
        lineage_checkpoint_sha256="a" * 64,
        origin=HUMAN_ORIGIN,
        classified_by="reviewer-1",
        reason="reviewer classified this claim as performance",
    )
    assert record["schema"] == SCHEMA
    assert record["track"] == "performance" and record["track_confidence"] is None
    assert record["origin"] == HUMAN_ORIGIN and record["classified_by"] == "reviewer-1"
    assert record["review_origin"] is None and record["delegation_authority"] is None
    assert record["lineage_checkpoint_sha256"] == "a" * 64
    assert "grade" not in record and "label" not in record and "decision" not in record
    assert record["record_sha256"] == canonical_hash(
        {k: v for k, v in record.items() if k != "record_sha256"}
    )
    # The stored record round-trips back through the same literal guards.
    replayed = classification_override(record, claim, graph, tenant_id=TENANT)
    assert replayed.track.track == "performance" and replayed.track_confidence is None


def test_ai_delegated_snapshot_requires_authority_and_distinct_provenance():
    graph, claim, _ = corpus()
    result = validate_manual_classification(claim, graph, body(claim), tenant_id=TENANT)
    with pytest.raises(ClassificationRejected):
        classification_snapshot(
            result,
            tenant_id=TENANT,
            run_id=claim.claim_id,
            claim=claim,
            graph=graph,
            lineage_checkpoint_sha256="a" * 64,
            origin=AI_DELEGATED_ORIGIN,
            classified_by="ai-delegated-classification:op",
            reason="delegated classification",
            delegation_authority="   ",
        )
    record = classification_snapshot(
        result,
        tenant_id=TENANT,
        run_id=claim.claim_id,
        claim=claim,
        graph=graph,
        lineage_checkpoint_sha256="a" * 64,
        origin=AI_DELEGATED_ORIGIN,
        classified_by="ai-delegated-classification:op",
        reason="delegated classification",
        delegation_authority="user delegation 2026-09-22",
    )
    assert record["origin"] == AI_DELEGATED_ORIGIN
    assert record["review_origin"] == "ai_project_interpretation"
    assert record["delegation_authority"] == "user delegation 2026-09-22"
