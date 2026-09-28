"""Executable integration of the pure numeric check via application composition.

These exercise ``proofops.application.numeric_analysis.analyze_numeric_consistency``
over the real fuse -> normalize -> discover -> typed-binding pipeline built by the
acceptance ``case`` helper. They assert a positive real domain check integration
plus mismatch/unknown/binding-absent handling, and that the layer never invents a
success for an unbound claim. The domain module is used, not modified.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from proofops.application.numeric_analysis import (
    BINDING_ABSENT,
    BINDING_NOT_ACCEPTED,
    analyze_numeric_consistency,
)

from tests.acceptance.test_numeric import case
from tests.acceptance.test_parsing import FOREIGN, TENANT


def _run(sample, *, bindings=None, claims=None):
    return analyze_numeric_consistency(
        tenant_id=TENANT,
        original=sample.original,
        observations=sample.items,
        bindings=sample.binding if bindings is None else bindings,
        claims=sample.claims if claims is None else claims,
    )


def test_positive_real_domain_check_integration_reports_consistent_with_provenance():
    sample = case(row_reported())
    report = _run(sample, bindings=(sample.binding,))
    assert report.has_findings is True
    assert len(report.outcomes) == 1
    outcome = report.outcomes[0]
    assert outcome.claim_id == sample.binding.claim_id
    assert outcome.status == "consistent"
    assert outcome.result is not None
    assert outcome.result.computed_value == "1"
    # Provenance is retained: the domain result keeps source refs, and per
    # observation holds are attached for the bound observations.
    assert outcome.result.source_refs
    assert len(outcome.holds) == len(sample.binding.observation_ids)
    assert report.document_version_id == sample.binding.document_version_id


def test_dimension_mismatch_is_not_comparable_not_a_success():
    sample = case(row_reported())
    mismatched = replace(sample.binding, scope="Scope 2")
    report = _run(sample, bindings=(mismatched,))
    outcome = report.outcomes[0]
    assert outcome.status == "not_comparable"
    assert outcome.reason == "dimension_mismatch"
    assert outcome.result is not None


@pytest.mark.parametrize("state", ["unknown", "conflict", "unreadable"])
def test_unknown_or_conflicting_value_stays_not_computable(state):
    sample = case(row_reported())
    sample.items = (replace(sample.items[0], value_state=state),)
    report = _run(sample, bindings=(sample.binding,))
    outcome = report.outcomes[0]
    assert outcome.status == "not_computable"
    assert outcome.result is not None
    # Uncertainty is preserved, never rewritten to absence or a decided grade.
    assert sample.items[0].value_state == state


def test_claim_without_binding_is_explicit_needs_review_not_success():
    sample = case(row_reported())
    report = _run(sample, bindings=())
    assert report.has_findings is False
    assert len(report.outcomes) == 1
    outcome = report.outcomes[0]
    assert outcome.status == "needs_review"
    assert outcome.reason == BINDING_ABSENT
    assert outcome.result is None


def test_unaccepted_binding_is_surfaced_not_silently_dropped():
    sample = case(row_reported())
    unaccepted = replace(sample.binding, binding_accepted=False)
    report = _run(sample, bindings=(unaccepted,), claims=())
    outcome = report.outcomes[0]
    assert outcome.status == "needs_review"
    assert outcome.reason == BINDING_NOT_ACCEPTED
    assert outcome.result is None
    assert report.has_findings is False


def test_tenant_mismatch_against_snapshot_is_rejected():
    sample = case(row_reported())
    with pytest.raises(ValueError, match="tenant"):
        analyze_numeric_consistency(
            tenant_id=FOREIGN,
            original=sample.original,
            observations=sample.items,
            bindings=(sample.binding,),
            claims=sample.claims,
        )


def test_duplicate_claim_binding_is_rejected_before_partition():
    sample = case(row_reported())
    duplicate = replace(sample.binding)
    with pytest.raises(ValueError, match="duplicate claim binding"):
        _run(sample, bindings=(sample.binding, duplicate))


def test_foreign_tenant_binding_rejected_even_when_not_accepted():
    # A rejected binding is surfaced, not dropped, so it is still validated for
    # tenant/version identity before the accepted partition is computed.
    sample = case(row_reported())
    foreign = replace(sample.binding, tenant_id=FOREIGN, binding_accepted=False)
    with pytest.raises(ValueError, match="binding tenant mismatch"):
        _run(sample, bindings=(foreign,), claims=())


def test_foreign_document_version_binding_rejected():
    sample = case(row_reported())
    # A mismatched document version fails at binding construction (source version
    # guard); the identity contract rejects a foreign-version binding either way.
    with pytest.raises(ValueError):
        foreign = replace(sample.binding, document_version_id=FOREIGN)
        _run(sample, bindings=(foreign,))


def test_claim_from_foreign_snapshot_is_rejected():
    sample = case(row_reported())
    foreign_claim = replace(sample.claims[0], tenant_id=FOREIGN)
    with pytest.raises(ValueError, match="numeric analysis tenant mismatch|claim snapshot"):
        analyze_numeric_consistency(
            tenant_id=TENANT,
            original=sample.original,
            observations=sample.items,
            bindings=(),
            claims=(foreign_claim,),
        )


def test_not_computable_binding_is_not_a_finding():
    # A supplied binding whose observation is unknown yields not_computable; that
    # is unresolved uncertainty, so has_findings must stay False.
    sample = case(row_reported())
    sample.items = (replace(sample.items[0], value_state="unknown"),)
    report = _run(sample, bindings=(sample.binding,))
    assert report.outcomes[0].status == "not_computable"
    assert report.has_findings is False


def test_default_no_bindings_reports_binding_absent_per_claim():
    sample = case(row_reported())
    report = analyze_numeric_consistency(
        tenant_id=TENANT,
        original=sample.original,
        observations=sample.items,
        bindings=(),
        claims=sample.claims,
    )
    assert report.has_findings is False
    assert [o.reason for o in report.outcomes] == [BINDING_ABSENT] * len(sample.claims)


def row_reported():
    """A single verified row whose reported value matches the source ("1")."""
    from tests.acceptance.test_numeric import row

    return row("1")
