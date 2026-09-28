"""AT-017 / FR-017: industry applicability is mapping-bound and fail-closed.

These fixtures are synthetic and local-only.  They exercise the pure domain
function directly; they do not represent an approved GICS-to-SASB mapping.
"""

from __future__ import annotations

import pytest
from proofops.domain.applicability import (
    IndustryIdentity,
    IndustryMapping,
    IndustryMappingEntry,
    resolve_industry_applicability,
)


def _verified_mapping() -> IndustryMapping:
    return IndustryMapping(
        version="synthetic-v1",
        verification_status="verified",
        entries=(
            IndustryMappingEntry(
                industry_system="GICS",
                industry_code="40101010",
                topic_id="water",
                applicability="N_A",
                mandatory=None,
            ),
            IndustryMappingEntry(
                industry_system="GICS",
                industry_code="15104020",
                topic_id="scope3_category_1",
                applicability="applicable",
                mandatory=False,
            ),
        ),
    )


def test_verified_mapping_keeps_applicability_and_mandatory_separate() -> None:
    result = resolve_industry_applicability(
        IndustryIdentity(system="GICS", code="15104020"),
        "scope3_category_1",
        _verified_mapping(),
    )

    assert result.applicability == "applicable"
    assert result.mandatory is False
    assert result.mapping_version == "synthetic-v1"


def test_verified_mapping_can_explicitly_mark_topic_not_applicable() -> None:
    result = resolve_industry_applicability(
        IndustryIdentity(system="GICS", code="40101010"),
        "water",
        _verified_mapping(),
    )

    assert result.applicability == "N_A"
    assert result.mandatory is None


def test_unknown_industry_is_undetermined_not_na() -> None:
    result = resolve_industry_applicability(
        IndustryIdentity(system="GICS", code="99999999"),
        "water",
        _verified_mapping(),
    )

    assert result.applicability == "undetermined"
    assert result.mandatory is None


@pytest.mark.parametrize("system,code", [("gics", None), ("unknown", None)])
def test_nullable_company_industry_stays_undetermined(system, code) -> None:
    result = resolve_industry_applicability(
        IndustryIdentity(system=system, code=code), "water", _verified_mapping()
    )
    assert result.applicability == "undetermined"
    assert result.mandatory is None


def test_unverified_mapping_cannot_remove_topic_from_denominator() -> None:
    mapping = IndustryMapping(
        version="synthetic-unverified-v1",
        verification_status="unverified",
        entries=_verified_mapping().entries,
    )

    result = resolve_industry_applicability(
        IndustryIdentity(system="GICS", code="40101010"),
        "water",
        mapping,
    )

    assert result.applicability == "undetermined"
    assert result.mandatory is None
