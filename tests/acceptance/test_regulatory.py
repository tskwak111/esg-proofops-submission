"""AT-018 · FR-018 approved regulatory timeline resolution (TASK-018).

The fixtures are synthetic local-only timeline records.  They do not assert
that any real company is legally covered, exempt, or entitled to immunity.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from proofops.domain.errors import DomainValidationError
from proofops.domain.regulatory import CompanyContext, resolve_deferral
from proofops.domain.rulepacks import pack_content_hash, snapshot_from_validated

TIMELINE_SHA256 = "a" * 64
TENANT_A = "11111111-1111-4111-8111-111111111111"
TENANT_B = "22222222-2222-4222-8222-222222222222"
COMPANY_A = "33333333-3333-4333-8333-333333333333"


def _timeline(
    *entries: dict[str, object],
    timeline_overrides: dict[str, object] | None = None,
    pack_overrides: dict[str, object] | None = None,
):
    timeline: dict[str, object] = {
        "version": "synthetic-local-v1",
        "effective_date": "2030-01-01",
        "source_document_sha256": TIMELINE_SHA256,
        "verification_status": "approved",
        "automatic_legal_applicability_enabled": True,
        "entries": list(entries),
    }
    timeline.update(timeline_overrides or {})
    files = {"regulatory/timeline.yaml": timeline}
    pack: dict[str, object] = {
        "rule_pack_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "tenant_id": TENANT_A,
        "version": "synthetic-local-v1",
        "effective_date": "2030-01-01",
        "mode": "disclosure",
        "status": "validated",
        "ontology_version": "synthetic-elements-v1",
        "source_document_sha256": TIMELINE_SHA256,
        "files": ["regulatory/timeline.yaml"],
        "unresolved_gap_ids": [],
        "approved_by": "domain-owner",
        "approved_at": "2030-01-01T00:00:00Z",
    }
    pack.update(pack_overrides or {})
    pack["sha256"] = pack_content_hash(pack, files)
    return snapshot_from_validated(pack, files)


def _context(
    *conditions: str,
    as_of: date = date(2030, 3, 31),
    tenant_id: str = TENANT_A,
) -> CompanyContext:
    return CompanyContext(
        tenant_id=tenant_id,
        company_id=COMPANY_A,
        as_of=as_of,
        condition_ids=frozenset(conditions),
    )


def _entry(
    entry_id: str,
    outcome: str,
    *,
    conditions: tuple[str, ...] = ("synthetic-cohort-a",),
    starts_on: date = date(2028, 1, 1),
    ends_before: date | None = date(2031, 1, 1),
) -> dict[str, object]:
    return {
        "entry_id": entry_id,
        "outcome": outcome,
        "condition_ids": list(conditions),
        "starts_on": starts_on.isoformat(),
        "ends_before": ends_before.isoformat() if ends_before else None,
    }


def test_unverified_timeline_cannot_create_required_or_immunity_conclusion() -> None:
    timeline = _timeline(
        _entry("synthetic-required", "required"),
        timeline_overrides={"verification_status": "source_provided_unverified"},
        pack_overrides={"approved_by": None, "approved_at": None},
    )

    result = resolve_deferral(_context("synthetic-cohort-a"), timeline)

    assert result == "undetermined"
    assert result not in {"violation", "exempt", "safe_harbor_confirmed"}


def test_resolver_reads_the_hash_verified_rulepack_timeline_snapshot() -> None:
    snapshot = _timeline(
        {
            "entry_id": "synthetic-deferral",
            "outcome": "advisory",
            "condition_ids": ["synthetic-cohort-a"],
            "starts_on": "2028-01-01",
            "ends_before": "2031-01-01",
        }
    )

    assert resolve_deferral(_context("synthetic-cohort-a"), snapshot) == "advisory"


def test_disabled_automatic_legal_applicability_stays_undetermined() -> None:
    timeline = _timeline(
        _entry("synthetic-required", "required"),
        timeline_overrides={"automatic_legal_applicability_enabled": False},
    )

    assert resolve_deferral(_context("synthetic-cohort-a"), timeline) == "undetermined"


def test_cross_tenant_rulepack_is_not_applied() -> None:
    timeline = _timeline(_entry("synthetic-required", "required"))

    assert (
        resolve_deferral(
            _context("synthetic-cohort-a", tenant_id=TENANT_B),
            timeline,
        )
        == "undetermined"
    )


def test_malformed_approval_metadata_stays_undetermined() -> None:
    timeline = _timeline(
        _entry("synthetic-required", "required"),
        pack_overrides={"approved_at": "not-a-timestamp"},
    )

    assert resolve_deferral(_context("synthetic-cohort-a"), timeline) == "undetermined"


def test_approved_matching_entry_returns_advisory() -> None:
    timeline = _timeline(_entry("synthetic-deferral", "advisory"))

    assert resolve_deferral(_context("synthetic-cohort-a"), timeline) == "advisory"


def test_approved_matching_entry_returns_required() -> None:
    timeline = _timeline(
        _entry(
            "synthetic-after-deferral",
            "required",
            starts_on=date(2031, 1, 1),
            ends_before=None,
        )
    )

    assert (
        resolve_deferral(
            _context("synthetic-cohort-a", as_of=date(2031, 1, 1)),
            timeline,
        )
        == "required"
    )


def test_unmatched_company_or_period_is_not_assumed_exempt() -> None:
    timeline = _timeline(_entry("synthetic-deferral", "advisory"))

    assert resolve_deferral(_context("synthetic-cohort-b"), timeline) == "undetermined"
    assert (
        resolve_deferral(
            _context("synthetic-cohort-a", as_of=date(2032, 1, 1)),
            timeline,
        )
        == "undetermined"
    )


def test_overlapping_entries_are_undetermined_even_when_outcomes_agree() -> None:
    timeline = _timeline(
        _entry("synthetic-overlap-a", "advisory"),
        _entry("synthetic-overlap-b", "advisory"),
    )

    assert resolve_deferral(_context("synthetic-cohort-a"), timeline) == "undetermined"


def test_condition_ids_reject_a_bare_string_at_the_domain_boundary() -> None:
    with pytest.raises(DomainValidationError, match="iterable of strings"):
        CompanyContext(
            tenant_id=TENANT_A,
            company_id=COMPANY_A,
            as_of=date(2030, 3, 31),
            condition_ids="synthetic-cohort-a",  # type: ignore[arg-type]
        )


def test_context_rejects_datetime_instead_of_crashing_during_comparison() -> None:
    with pytest.raises(DomainValidationError, match="as_of must be a date"):
        CompanyContext(
            tenant_id=TENANT_A,
            company_id=COMPANY_A,
            as_of=datetime(2030, 3, 31, tzinfo=UTC),  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("field,value", [("outcome", []), ("outcome", {}), ("ends_before", "")])
def test_malformed_timeline_entry_stays_undetermined(field: str, value: object) -> None:
    entry = _entry("synthetic-malformed", "required")
    entry[field] = value
    assert resolve_deferral(_context("synthetic-cohort-a"), _timeline(entry)) == "undetermined"
