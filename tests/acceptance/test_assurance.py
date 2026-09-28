"""AT-007: synthetic source/tag inputs exercising real extraction and matching."""

import json
from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from proofops.application.ingest.graph_fusion import (
    CandidateBatch,
    CandidateBlock,
    fuse_candidates,
)
from proofops.application.ports.models import ModelBinding
from proofops.domain.documents import NativeSource, PageGeometry

TENANT = "11111111-1111-4111-8111-111111111111"
VERSION = "22222222-2222-4222-8222-222222222222"
MANIFEST = "33333333-3333-4333-8333-333333333333"
STATEMENT = "44444444-4444-4444-8444-444444444444"
RUN = "55555555-5555-4555-8555-555555555555"
CLAIM = "66666666-6666-4666-8666-666666666666"
BINDING = ModelBinding("synthetic-assurance", "assurance", True)


def inputs(**changes):
    """Human-authored synthetic tags; no model or matching stub."""
    values = dict(
        provider="예시 보증기관",
        standard_raw="ISAE 3000",
        level="limited",
        reporting_period="2024",
        entities="예시 법인",
        facilities="서울 사업장",
        covered_metrics="Scope 1",
    )
    values.update(changes)
    blocks = tuple(
        CandidateBlock(
            "paragraph",
            NativeSource(
                VERSION,
                MANIFEST,
                RUN,
                name,
                i + 1,
                None,
                (10, 10, 300, 40),
                "pdf_bottom_left_points",
                value,
                0,
                len(value),
            ),
            PageGeometry(600, 800, 0, (0, 0, 600, 800)),
        )
        for i, (name, value) in enumerate(values.items())
        if value is not None
    )
    batch = CandidateBatch(
        TENANT,
        VERSION,
        MANIFEST,
        "a" * 64,
        RUN,
        "synthetic",
        "fixture-v1",
        "synthetic",
        "b" * 64,
        blocks,
        (),
        synthetic=True,
    )
    graph = fuse_candidates((batch,), tenant_id=TENANT)
    # Explicit synthetic human-verification fixture; never parser auto-approval.
    graph = replace(graph, blocks=tuple(replace(b, quality="verified") for b in graph.blocks))
    fields = {
        b.sources[0].source_native_id: (replace(b.source_ref(), verification_state="verified"),)
        for b in graph.blocks
    }
    refs = tuple(r for items in fields.values() for r in items)
    return graph, refs, fields


def extract(graph=None, refs=None, fields=None, **changes):
    from proofops.application.assurance import extract_assurance

    if graph is None:
        graph, refs, fields = inputs(**changes)
    return extract_assurance(
        graph,
        refs,
        BINDING,
        tagged_fields=fields,
        tenant_id=TENANT,
        statement_id=STATEMENT,
        model_sha256="c" * 64,
        prompt_sha256="d" * 64,
        replicate_id=1,
    )


def claim(**changes):
    from proofops.application.assurance import ClaimContext

    data = dict(
        tenant_id=TENANT,
        document_version_id=VERSION,
        claim_id=CLAIM,
        metric="Scope 1",
        reporting_period="2024",
        entities=("예시 법인",),
        facilities=("서울 사업장",),
    )
    return ClaimContext(**(data | changes))


def match(statement=None, **changes):
    from proofops.application.assurance import match_assurance

    return match_assurance(statement or extract(), claim(**changes))


def test_verified_same_scope_is_covered_with_fixed_api_contract():
    result = match()
    assert (result.status, result.metric_match, result.period_match, result.boundary_match) == (
        "covered",
        "yes",
        "yes",
        "yes",
    )
    assert result.provider == "예시 보증기관" and result.level == "limited"
    schema = json.loads(Path("contracts/jsonschema/api_models.schema.json").read_text())
    Draft202012Validator(
        {"$ref": "#/$defs/AssuranceMatch", "$defs": schema["$defs"]}, format_checker=FormatChecker()
    ).validate(result.to_dict())
    assert result.to_dict()["statement_id"] == STATEMENT
    assert result.to_dict()["evidence_refs"][0]["page_num"] >= 1


@pytest.mark.parametrize(
    "changes,dimension",
    [
        ({"reporting_period": "2023"}, "period_match"),
        ({"facilities": ("부산 사업장",)}, "boundary_match"),
        ({"entities": ("다른 법인",)}, "boundary_match"),
        ({"metric": "Scope 3"}, "metric_match"),
        ({"facilities": ("서울 사업장", "부산 사업장")}, "boundary_match"),
    ],
)
def test_same_provider_never_covers_other_period_facility_entity_or_metric(changes, dimension):
    result = match(**changes)
    assert result.status == "not_covered"
    assert getattr(result, dimension) == "no"


@pytest.mark.parametrize(
    "field",
    [
        "provider",
        "standard_raw",
        "level",
        "reporting_period",
        "entities",
        "facilities",
        "covered_metrics",
    ],
)
def test_missing_information_is_undetermined(field):
    assert match(extract(**{field: None})).status == "undetermined"


@pytest.mark.parametrize("quality", ["unverified", "conflicted", "unreadable", "unlocated"])
def test_unresolved_source_quality_is_never_coverage_or_absence(quality):
    graph, refs, fields = inputs()
    graph = replace(graph, blocks=tuple(replace(b, quality=quality) for b in graph.blocks))
    statement = extract(graph, refs, fields)
    result = match(statement, reporting_period="2023")
    assert result.status == "undetermined"
    assert result.period_match == "unknown"
    assert statement.unresolved_fields


def test_provider_only_and_no_statement_remain_undetermined():
    from proofops.application.assurance import match_assurance

    assert (
        match(
            extract(
                standard_raw=None,
                level=None,
                reporting_period=None,
                entities=None,
                facilities=None,
                covered_metrics=None,
            )
        ).status
        == "undetermined"
    )
    assert match_assurance(None, claim()).status == "undetermined"


@pytest.mark.parametrize(
    "field,value",
    [
        ("excluded_metrics", "Scope 1"),
        ("excluded_entities", "예시 법인"),
        ("excluded_facilities", "서울 사업장"),
        ("excluded_periods", "2024"),
    ],
)
def test_explicit_exclusion_overrides_positive_scope(field, value):
    statement = extract(**{field: value})
    assert match(statement).status == "not_covered"
    assert statement.explicit_exclusions


def test_ambiguous_exclusion_and_level_do_not_become_covered():
    assert match(extract(explicit_exclusions="일부 자료 제외")).status == "undetermined"
    assert match(extract(level="moderate")).status == "undetermined"
    assert match(extract(level="none")).status == "not_covered"


@pytest.mark.parametrize(
    "mutation",
    ["quote", "hash", "offset", "page", "bbox", "version", "manifest", "source", "outside"],
)
def test_forged_or_out_of_statement_citation_rejected(mutation):
    graph, refs, fields = inputs()
    ref = fields["covered_metrics"][0]
    replacements = dict(
        quote={"quote": "Scope 3"},
        hash={"raw_text_sha256": "f" * 64},
        offset={"char_end": ref.char_end + 1},
        page={"page_num": 50},
        bbox={"bbox": (1, 1, 2, 2)},
        version={"document_version_id": TENANT},
        manifest={"parse_manifest_id": TENANT},
        source={"source_id": TENANT},
    )
    if mutation == "outside":
        refs = tuple(r for r in refs if r != ref)
    else:
        fields["covered_metrics"] = (replace(ref, **replacements[mutation]),)
    with pytest.raises(ValueError):
        extract(graph, refs, fields)


def test_tenant_version_isolation_and_no_source_less_present():
    graph, refs, fields = inputs()
    with pytest.raises(ValueError):
        extract(replace(graph, tenant_id=RUN), refs, fields)
    for changes in ({"tenant_id": RUN}, {"document_version_id": RUN}):
        with pytest.raises(ValueError):
            match(**changes)
    with pytest.raises(ValueError):
        extract(graph, (), fields)
    fields["label"] = fields["provider"]
    with pytest.raises(ValueError):
        extract(graph, refs, fields)


def test_conflicting_scalar_tags_preserved_and_revisions_immutable():
    graph, refs, fields = inputs(excluded_periods="2023")
    fields["reporting_period"] += fields["excluded_periods"]
    statement = extract(graph, refs, fields)
    assert match(statement).period_match == "unknown"
    assert "reporting_period" in statement.unresolved_fields
    fields.clear()
    assert statement.source_refs and statement.reporting_period is None
    with pytest.raises(FrozenInstanceError):
        statement.provider = "changed"
    first, second = extract(), extract(level="reasonable")
    assert first.semantic_hash != second.semantic_hash
    assert match(first).level == "limited" and match(second).level == "reasonable"
    assert first.synthetic is True
    assert first.model_sha256 == "c" * 64 and first.prompt_sha256 == "d" * 64
    assert first.source_sha256 == "a" * 64 and first.replicate_id == 1
    assert len(match(first).rule_sha256) == 64


def test_unknown_claim_dimensions_and_uninterpreted_period_remain_unknown():
    assert match(metric=None).metric_match == "unknown"
    assert match(facilities=()).boundary_match == "unknown"
    assert match(reporting_period="FY24").period_match == "unknown"


def test_span_extraction_checks_substring_against_actual_graph():
    graph, refs, fields = inputs(provider="보증기관: 예시기관")
    full = fields["provider"][0]
    fields["provider"] = (replace(full, quote="예시기관", char_start=6),)
    statement = extract(graph, refs, fields)
    assert statement.provider == "예시기관"
    saved = dict(statement.tagged_fields)["provider"][0]
    assert (saved.quote, saved.char_start, saved.char_end) == ("예시기관", 6, 10)
    assert sha256("보증기관: 예시기관".encode()).hexdigest() == full.raw_text_sha256


def test_statement_cannot_drop_source_or_alias_mutable_input():
    statement = extract()
    with pytest.raises(ValueError):
        replace(statement, source_refs=())
    refs = list(statement.source_refs)
    snapshot = replace(statement, source_refs=refs)
    refs.clear()
    assert snapshot.source_refs == statement.source_refs


def test_unverified_exclusion_is_not_definite_absence():
    graph, refs, fields = inputs(excluded_metrics="Scope 1")
    graph = replace(
        graph,
        blocks=tuple(
            replace(b, quality="unverified")
            if b.sources[0].source_native_id == "excluded_metrics"
            else b
            for b in graph.blocks
        ),
    )
    assert match(extract(graph, refs, fields)).status == "undetermined"


def test_distinct_opinions_keep_levels_and_scope_separate():
    from proofops.application.assurance import match_assurance

    limited = extract()
    reasonable_elsewhere = extract(level="reasonable", facilities="부산 사업장")
    results = [match_assurance(s, claim()) for s in (limited, reasonable_elsewhere)]
    assert [(r.status, r.level) for r in results] == [
        ("covered", "limited"),
        ("not_covered", "reasonable"),
    ]


@pytest.mark.parametrize(
    "changes",
    [
        {"replicate_id": True},
        {"replicate_id": 0},
        {"model_sha256": "invalid"},
        {"prompt_sha256": "invalid"},
    ],
)
def test_invalid_extraction_provenance_is_rejected(changes):
    from proofops.application.assurance import extract_assurance

    graph, refs, fields = inputs()
    options = dict(
        tagged_fields=fields,
        tenant_id=TENANT,
        statement_id=STATEMENT,
        model_sha256="c" * 64,
        prompt_sha256="d" * 64,
        replicate_id=1,
    )
    with pytest.raises(ValueError):
        extract_assurance(graph, refs, BINDING, **(options | changes))


def test_multiple_metric_site_groups_cannot_form_invented_cross_product():
    graph, refs, fields = inputs(extra_metric="Scope 2", extra_facility="부산 사업장")
    fields["covered_metrics"] += fields.pop("extra_metric")
    fields["facilities"] += fields.pop("extra_facility")
    statement = extract(graph, refs, fields)
    # Lists alone cannot establish that Scope 1 belongs to the Busan scope group.
    assert match(statement, facilities=("부산 사업장",)).status == "undetermined"
