"""AT-005: explicitly synthetic confirmed sources, never model/customer approval."""

from dataclasses import asdict, dataclass, replace
from decimal import localcontext

import pytest
from proofops.application.claims import ClaimScope, discover_atomic_claims
from proofops.application.evidence.citations import verify_source_ref
from proofops.application.ingest.graph_fusion import fuse_candidates
from proofops.application.ingest.normalize import normalize_tables
from proofops.domain.provenance import canonical_hash
from proofops_agent.extraction import SyntheticClaimExtractor

from .test_parsing import FOREIGN, MANIFEST, TENANT, VERSION, candidate
from .test_tables import table


@dataclass
class Case:
    items: tuple
    binding: object
    claims: tuple
    original: object

    def check(self, **changes):
        from proofops.domain.numeric import check_numeric_consistency

        args = dict(tenant_id=TENANT, claims=self.claims, original=self.original)
        args.update(changes)
        return check_numeric_consistency(self.items, (self.binding,), **args)[0]


def test_saved_sum_binding_hash_before_optional_product_fields_remains_valid():
    c = case(row("1"), row("2"), kind="sum", reported="3", aggregation=True)
    old_payload = asdict(replace(c.binding, aggregation=None))
    for field in (
        "baseline_subject",
        "subject_ref",
        "baseline_subject_ref",
        "product_comparison_accepted",
        "conditions",
    ):
        old_payload.pop(field)
    c.binding = replace(
        c.binding,
        aggregation=replace(c.binding.aggregation, binding_sha256=canonical_hash(old_payload)),
    )
    assert c.check().status == "consistent"


@pytest.mark.parametrize("tamper", [None, "hash", "quote", "members", "actor", "pending", "owner"])
def test_growth_requires_pinned_review_of_nontrivial_shared_note(tamper):
    from proofops.domain.numeric import NumericCondition, condition_binding_hash

    c = case(
        row("598846", "2023"),
        row("644685", "2024"),
        kind="growth",
        reported="8",
        footnote="도매 기준",
    )
    assert c.check().status == "not_computable"
    note = next(b for b in c.original.blocks if b.kind == "footnote")
    source = verify_source_ref(note.source_ref(), c.original, tenant_id=TENANT)
    condition = NumericCondition(
        source,
        c.binding.observation_ids,
        condition_binding_hash(c.binding),
        "same_basis_for_selected_observations",
        "accepted",
        "synthetic-reviewer",
    )
    if tamper == "hash":
        condition = replace(condition, binding_sha256="f" * 64)
    elif tamper == "quote":
        condition = replace(condition, source_ref=replace(source, quote="소매 기준"))
    elif tamper == "members":
        condition = replace(condition, observation_ids=c.binding.observation_ids[:1])
    elif tamper == "actor":
        condition = replace(condition, reviewed_by="")
    elif tamper == "pending":
        condition = replace(condition, acceptance_state="pending")
    elif tamper == "owner":
        c.original = replace(
            c.original, edges=tuple(e for e in c.original.edges if e.relation != "footnote_of")
        )
    c.binding = replace(c.binding, conditions=(condition,))
    assert c.check().status == ("consistent" if tamper is None else "not_computable")


def test_unit_and_denominator_can_be_verified_subspans_of_same_original_cell():
    c = case(row("35.40", unit="tCO2eq/대", denominator="대"), reported="35.40")
    item = c.items[0]
    unit = next(ref for ref in item.source_refs if ref.quote == "tCO2eq/대")
    denominator = next(ref for ref in item.source_refs if ref.quote == "대")
    part = verify_source_ref(
        replace(unit, quote="대", char_start=unit.char_end - 1), c.original, tenant_id=TENANT
    )
    c.items = (
        replace(
            item,
            source_refs=tuple(ref for ref in item.source_refs if ref != denominator) + (part,),
            parent_relations=tuple(
                (a, unit.source_id if field == "denominator" else b, field)
                for a, b, field in item.parent_relations
            ),
        ),
    )
    assert c.check().status == "consistent"


def row(value, period="2025", unit="tCO2e", denominator="", subject="서울"):
    return ["배출량", "Scope 1", subject, period, "시장기반", "연결", unit, denominator, value]


def case(
    *rows,
    kind="comparison",
    reported="1",
    aggregation=False,
    footnote=None,
    cell_note=False,
    note_target=None,
):
    from proofops.domain.numeric import AggregationRelation, ClaimBinding

    statement = f"{reported} 배출량 공시 수치."
    if aggregation:
        statement += (
            f" 표의 데이터 행 1~{len(rows)}은 중복과 소계를 제외한"
            " 이 합계의 모든 상호 배타적 구성요소다."
        )
    graph = fuse_candidates(
        (
            table(
                [
                    [
                        "지표",
                        "Scope",
                        "사업장",
                        "연도",
                        "산정방식",
                        "조직경계",
                        "단위",
                        "분모",
                        "값",
                    ],
                    *rows,
                ],
                footnote=footnote,
            ),
            candidate("claim", [("C", "paragraph", statement, (1, 500, 590, 520), ())]),
        ),
        tenant_id=TENANT,
    )
    if cell_note or note_target:
        root = next(
            b
            for b in graph.blocks
            if b.candidates[0].source.source_native_id == (note_target or "r1c8")
        )
        graph = replace(
            graph,
            edges=tuple(
                replace(e, target_id=root.source_id) if e.relation == "footnote_of" else e
                for e in graph.edges
            ),
        )
    # Explicit synthetic source-quality confirmation, as in AT-012. Real citation
    # verification still checks raw candidates/geometry/hash; no automatic approval.
    graph = replace(graph, blocks=tuple(replace(b, quality="verified") for b in graph.blocks))
    normalized = normalize_tables(graph, tenant_id=TENANT).observations
    items = tuple(
        replace(
            o,
            quality="verified",
            source_refs=tuple(
                verify_source_ref(r, graph, tenant_id=TENANT)
                for r in o.source_refs
                if r.quote.strip()
            ),
        )
        for o in normalized
    )
    discovered = discover_atomic_claims(
        graph, ClaimScope(TENANT, VERSION, MANIFEST), extractor=SyntheticClaimExtractor()
    )
    claim = next(c for c in discovered.claims if c.quote.startswith(reported + " 배출량"))
    claim = replace(
        claim,
        source_refs=tuple(verify_source_ref(r, graph, tenant_id=TENANT) for r in claim.source_refs),
    )
    claim_ref = claim.source_refs[0]
    number_ref = verify_source_ref(
        replace(claim_ref, quote=reported, char_end=claim_ref.char_start + len(reported)),
        graph,
        tenant_id=TENANT,
    )
    first = items[0]
    binding = ClaimBinding(
        claim_id=claim.claim_id,
        tenant_id=TENANT,
        document_version_id=VERSION,
        parse_manifest_id=MANIFEST,
        kind=kind,
        observation_ids=tuple(o.observation_id for o in items),
        reported_value=reported,
        metric_raw=first.metric_raw,
        scope=first.scope,
        subject=first.subject,
        reporting_period=items[-1].reporting_period,
        baseline_period=first.reporting_period if kind in ("reduction", "growth") else None,
        scope2_basis=first.scope2_basis,
        organizational_boundary=first.organizational_boundary,
        unit=first.unit_canonical,
        denominator=first.denominator,
        source_refs=claim.source_refs,
        reported_value_ref=number_ref,
        binding_accepted=True,
        quantity_kind="intensity"
        if first.denominator or "/" in (first.unit_canonical or "")
        else "absolute",
    )
    if aggregation:
        binding = replace(
            binding,
            aggregation=AggregationRelation(
                target_claim_id=binding.claim_id,
                observation_ids=binding.observation_ids,
                binding_sha256=canonical_hash(asdict(binding)),
                source_refs=(
                    verify_source_ref(
                        next(
                            b for b in graph.blocks if b.source_id == claim_ref.source_id
                        ).source_ref(),
                        graph,
                        tenant_id=TENANT,
                    ),
                ),
                relation="disjoint_complete_components",
                acceptance_state="accepted",
            ),
        )
    return Case(items, binding, (claim,), graph)


def test_sum_uses_decimal_without_float_rounding():
    c = case(row("0.1"), row("0.2"), kind="sum", reported="0.3", aggregation=True)
    result = c.check()
    assert result.status == "consistent"
    assert result.computed_value == "0.3"


@pytest.mark.parametrize("baseline", ["0", "-", "0.0"])
def test_reduction_records_zero_or_missing_values_as_not_computable(baseline):
    c = case(row(baseline, "2024"), row("10"), kind="reduction", reported="10")
    assert c.check().status == "not_computable"


def test_comparison_accepts_overlapping_display_rounding_intervals():
    result = case(row("1.24"), reported="1.2").check()
    assert result.status == "consistent"
    assert result.computed_value == "1.24"


@pytest.mark.parametrize(
    "field,value",
    [
        ("reporting_period", "2024"),
        ("scope", "Scope 2"),
        ("subject", "부산"),
        ("scope2_basis", "위치기반"),
        ("organizational_boundary", "별도"),
        ("unit", "%"),
        ("denominator", "매출"),
        ("metric_raw", "에너지"),
    ],
)
def test_dimension_mismatch_is_not_comparable_not_a_numeric_failure(field, value):
    c = case(row("1.24"), reported="1.2")
    c.binding = replace(c.binding, **{field: value})
    assert c.check().status == "not_comparable"


def test_rejects_cross_tenant_and_source_less_claim_bindings():
    c = case(row("1"))
    with pytest.raises(ValueError, match="tenant"):
        c.check(tenant_id=FOREIGN)
    with pytest.raises(ValueError, match="source"):
        replace(c.binding, source_refs=())


@pytest.mark.parametrize(
    "quality", ["unverified", "unknown", "conflicted", "unreadable", "unlocated"]
)
def test_untrusted_observation_never_reports_consistent(quality):
    c = case(row("1"))
    c.items = (replace(c.items[0], quality=quality),)
    assert c.check().status == "not_computable"


@pytest.mark.parametrize("state", ["missing", "unknown", "conflict", "unreadable"])
def test_unknown_values_are_not_absence_or_numeric_findings(state):
    c = case(row("1"))
    c.items = (replace(c.items[0], value_state=state),)
    assert c.check().status == "not_computable"
    assert c.items[0].value_state == state


@pytest.mark.parametrize(
    "changes",
    [
        {"verification_state": "candidate"},
        {"verification_state": "rejected"},
        {"location_quality": "unlocated"},
        {"bbox": None},
        {"source_id": FOREIGN},
        {"parse_manifest_id": FOREIGN},
        {"raw_text_sha256": "f" * 64},
        {"quote": "forged"},
        {"char_end": 999},
    ],
)
def test_forged_observation_refs_fail_even_with_verified_flag(changes):
    c = case(row("1"))
    o = c.items[0]
    c.items = (replace(o, source_refs=(replace(o.source_refs[0], **changes), *o.source_refs[1:])),)
    assert c.check().status == "not_computable"


@pytest.mark.parametrize("target", ["claim", "binding", "number"])
def test_candidate_claim_or_number_refs_cannot_be_used(target):
    c = case(row("1"))
    if target == "claim":
        c.claims = (
            replace(
                c.claims[0],
                source_refs=tuple(
                    replace(r, verification_state="candidate") for r in c.claims[0].source_refs
                ),
            ),
        )
    elif target == "binding":
        c.binding = replace(c.binding, source_refs=c.items[0].source_refs)
    else:
        c.binding = replace(c.binding, reported_value_ref=c.items[0].source_refs[0])
    assert c.check().status == "not_computable"


@pytest.mark.parametrize(
    "change", ["missing_claim", "manifest", "artifact", "candidate", "quality", "unaccepted"]
)
def test_trusted_claim_and_original_chain_are_required(change):
    c = case(row("1"))
    if change == "missing_claim":
        c.claims = ()
    elif change == "manifest":
        c.original = replace(c.original, parse_manifest_id=FOREIGN)
    elif change == "artifact":
        c.original = replace(c.original, source_sha256="f" * 64)
    elif change == "candidate":
        c.original = replace(c.original, candidates=())
    elif change == "quality":
        c.claims = (replace(c.claims[0], source_quality="unverified"),)
    else:
        c.binding = replace(c.binding, binding_accepted=False)
    assert c.check().status == "not_computable"


def test_missing_trusted_context_fails_closed_for_legacy_callers():
    c = case(row("1"))
    assert c.check(original=None, claims=()).status == "not_computable"


@pytest.mark.parametrize(
    "change", ["missing", "unaccepted", "members", "target", "binding", "candidate", "overlap"]
)
def test_arbitrary_overlapping_or_subtotal_rows_are_not_an_authorized_sum(change):
    c = case(row("1"), row("2"), kind="sum", reported="3", aggregation=True)
    relation = c.binding.aggregation
    if change == "missing":
        relation = None
    elif change == "unaccepted":
        relation = replace(relation, acceptance_state="unknown")
    elif change == "members":
        relation = replace(relation, observation_ids=relation.observation_ids[:1])
    elif change == "target":
        relation = replace(relation, target_claim_id=FOREIGN)
    elif change == "binding":
        relation = replace(relation, binding_sha256="f" * 64)
    elif change == "candidate":
        relation = replace(
            relation,
            source_refs=tuple(
                replace(r, verification_state="candidate") for r in relation.source_refs
            ),
        )
    else:
        relation = replace(relation, relation="overlapping_subtotals")
    c.binding = replace(c.binding, aggregation=relation)
    result = c.check()
    assert (result.status, result.reason) == ("not_comparable", "aggregation_relation_unresolved")
    assert result.computed_value is None


def test_reduction_requires_explicit_distinct_current_and_baseline_periods():
    c = case(row("100.00", "2024"), row("90.00"), kind="reduction", reported="10.00")
    assert c.check().status == "consistent"
    assert c.check().computed_value == "10"
    for changes in (
        {"baseline_period": None},
        {"baseline_period": "2025"},
        {"reporting_period": "2024"},
    ):
        c.binding = replace(c.binding, **changes)
        assert c.check().status == "not_comparable"


def test_cross_year_sum_is_not_comparable_even_with_relation():
    c = case(row("1", "2024"), row("2"), kind="sum", reported="3", aggregation=True)
    assert c.check().status == "not_comparable"


@pytest.mark.parametrize(
    "raw,reported,status", [("1.25", "1254", "consistent"), ("1.25", "1260", "inconsistent")]
)
def test_source_scale_determines_display_precision(raw, reported, status):
    c = case(row(raw, unit="천 tCO2e"), reported=reported)
    assert c.check().status == status
    assert c.check().computed_value == "1250"


def test_sum_combines_input_rounding_intervals():
    c = case(row("1.0"), row("1.0"), kind="sum", reported="2.09", aggregation=True)
    assert c.check().status == "consistent"


def test_reduction_uses_input_intervals_without_fabricated_computed_tolerance():
    c = case(row("100", "2024"), row("90"), kind="reduction", reported="10.8")
    assert c.check().status == "consistent"
    c = case(row("100.000", "2024"), row("90.000"), kind="reduction", reported="10.4")
    assert c.check().status == "inconsistent"


def test_decimal_arithmetic_is_independent_of_ambient_precision_and_traps():
    c = case(
        row("123456789012345678901234567890.1"),
        row("0.2"),
        kind="sum",
        reported="123456789012345678901234567890.3",
        aggregation=True,
    )
    with localcontext() as context:
        context.prec = 4
        for signal in context.traps:
            context.traps[signal] = True
        result = c.check()
    assert result.status == "consistent"
    assert result.computed_value == "123456789012345678901234567890.3"


def test_no_arbitrary_five_percent_tolerance():
    assert case(row("100.00"), reported="104.00").check().status == "inconsistent"


def test_mutated_normalized_number_cannot_disagree_with_verified_raw_source():
    c = case(row("1"))
    c.items = (replace(c.items[0], value_decimal="2"),)
    assert c.check().status == "not_computable"


def test_renamed_duplicate_source_cannot_be_summed_twice():
    c = case(row("1"), row("1"), kind="sum", reported="2", aggregation=True)
    c.items = (c.items[0], replace(c.items[0], observation_id=c.items[1].observation_id))
    assert c.check().status != "consistent"


def test_scale_and_unit_cannot_be_fabricated_away_from_bound_source():
    c = case(row("1.00"), reported="1000")
    c.items = (
        replace(c.items[0], unit_raw="천 tCO2e", scale_multiplier="1000", value_decimal="1000"),
    )
    assert c.check().status == "not_computable"


def test_claim_value_cannot_be_a_substring_of_a_different_number():
    c = case(row("1"), reported="100")
    ref = c.binding.reported_value_ref
    c.binding = replace(
        c.binding,
        reported_value="1",
        reported_value_ref=replace(ref, quote="1", char_end=ref.char_start + 1),
    )
    assert c.check().status == "not_computable"


def test_missing_intensity_denominator_is_not_comparable():
    c = case(row("1", unit="tCO2e/매출"))
    assert c.check().status == "not_comparable"


def test_exact_high_precision_comparison_does_not_collapse_different_values():
    c = case(
        row("123456789012345678901234567890.123"), reported="123456789012345678901234567890.125"
    )
    with localcontext() as context:
        context.prec = 4
        assert c.check().status == "inconsistent"


@pytest.mark.parametrize("reported,status", [("33.33", "consistent"), ("34.00", "inconsistent")])
def test_recurring_reduction_preserves_exact_ratio_without_fabricated_precision(reported, status):
    c = case(row("3.000", "2024"), row("2.000"), kind="reduction", reported=reported)
    result = c.check()
    assert (result.status, result.computed_value, result.reason, result.exact_ratio) == (
        status,
        None,
        "non_terminating_decimal",
        ("100", "3"),
    )


def test_multi_reference_claim_uses_accepted_space_joined_quotes():
    c = case(row("1"))
    ref = c.claims[0].source_refs[0]
    number = c.binding.reported_value_ref
    rest = replace(ref, char_start=ref.char_start + 2, quote=ref.quote[2:])
    rest = verify_source_ref(rest, c.original, tenant_id=TENANT)
    c.claims = (replace(c.claims[0], source_refs=(number, rest)),)
    c.binding = replace(c.binding, source_refs=(number, rest))
    assert c.check().status == "consistent"


@pytest.mark.parametrize("quantity_kind", [None, "intensity"])
def test_unknown_quantity_kind_or_missing_intensity_denominator_blocks_comparison(quantity_kind):
    c = case(row("1"))
    c.binding = replace(c.binding, quantity_kind=quantity_kind)
    assert c.check().status == "not_comparable"


@pytest.mark.parametrize("period", ["tCO2e", "MWh", "기간", "2025-13-01", "2025–2024"])
def test_equal_uninterpretable_periods_do_not_enable_numeric_comparison(period):
    result = case(row("1", period=period)).check()
    assert result.status == "not_comparable"
    assert result.reason == "reporting_period_unresolved"


@pytest.mark.parametrize(
    "period", ["2025년", "FY2025", "2025 Q1", "2024–2025", "2025-01-01", "2025-01-01 ~ 2025-12-31"]
)
def test_supported_explicit_periods_preserve_literal_comparison(period):
    assert case(row("1", period=period)).check().status == "consistent"


@pytest.mark.parametrize(
    "note",
    [
        "1. 데이터 산출 가능한 차종(EV3, EV6, EV9) 한정",
        "해외 사업장 제외",
    ],
)
def test_verified_text_does_not_resolve_footnote_conditions(note):
    sample = case(row("1"), reported="1", footnote=note)
    assert sample.items[0].quality == "verified"
    assert sample.items[0].footnotes == (note,)
    assert sample.check().status == "not_computable"


def test_numeric_unit_note_passes_but_dropping_qualifying_note_metadata_does_not():
    plain = case(row("1"), reported="1", footnote="단위: tCO2e")
    assert plain.check().status == "consistent"
    sample = case(row("1"), reported="1", footnote="해외 사업장 제외")
    item = sample.items[0]
    sample.items = (
        replace(
            item,
            footnotes=(),
            source_refs=tuple(ref for ref in item.source_refs if ref.quote != "해외 사업장 제외"),
        ),
    )
    result = sample.check()
    assert result.status == "not_computable"
    assert result.reason == "footnote_conditions_unresolved"


@pytest.mark.parametrize("scope", ["Scope 1", "Scope 2", "Scope 3"])
@pytest.mark.parametrize("template", ["{}", "  {} \n", "Scope:{}", "Scope: \t{}"])
@pytest.mark.parametrize("row_scope", ["", "explicit"])
def test_complete_verified_scope_note_allows_actual_comparison(scope, template, row_scope):
    from proofops.domain.numeric import scope_note_literal

    note = template.format(scope)
    assert scope_note_literal(note) == scope
    values = row("1")
    values[1] = scope if row_scope else ""
    sample = case(values, footnote=note)
    assert sample.items[0].scope == scope
    result = sample.check()
    assert (result.status, result.computed_value) == ("consistent", "1")
    assert any(ref.quote == note for ref in result.source_refs)


@pytest.mark.parametrize(
    "note",
    [
        "Scope 1 제외",
        "Includes Scope 1",
        "Scope: Scope 1; 해외 사업장 제외",
        "해외 사업장 제외",
        "scope 1",
        "scope: Scope 1",
        "Scope : Scope 1",
        "Scope  1",
        "Scope 4",
        "Scope 1/2/3",
        "Scope: 1",
    ],
)
def test_scope_note_syntax_does_not_broaden_to_prose_or_other_literals(note):
    from proofops.domain.numeric import scope_note_literal

    assert scope_note_literal(note) is None
    result = case(row("1"), footnote=note).check()
    assert (result.status, result.reason) == ("not_computable", "footnote_conditions_unresolved")


def test_scope_note_must_match_observation_and_claim_dimensions():
    sample = case(row("1"), footnote="Scope: Scope 2")
    result = sample.check()
    assert (result.status, result.reason) == ("not_computable", "footnote_conditions_unresolved")
    sample = case(row("1"), footnote="Scope: Scope 1")
    sample.binding = replace(sample.binding, scope="Scope 2")
    result = sample.check()
    assert (result.status, result.reason) == ("not_comparable", "dimension_mismatch")


def test_scope_note_field_association_requires_owned_note_not_bag_of_quotes():
    sample = case(row("1"), footnote="Scope: Scope 1")
    item = sample.items[0]
    note = next(ref for ref in item.source_refs if ref.quote == "Scope: Scope 1")
    relations = tuple(
        (source, note.source_id if field == "scope" else target, field)
        for source, target, field in item.parent_relations
    )
    sample.items = (replace(item, parent_relations=relations),)
    assert sample.check().status == "consistent"
    sample.items = (replace(item, parent_relations=tuple(r for r in relations if r[2] != "scope")),)
    assert sample.check().reason == "value_missing_or_unresolved"


@pytest.mark.parametrize("note_text", ["Scope 1", "Scope: Scope 1"])
def test_scope_note_owned_by_other_year_cannot_bind_through_extra_refs(note_text):
    sample = case(
        row("1"), row("1", period="2024"), kind="sum", footnote=note_text, note_target="r2c8"
    )
    item = sample.items[0]
    note = next(b for b in sample.original.blocks if b.kind == "footnote")
    owner = next(
        b for b in sample.original.blocks if b.candidates[0].source.source_native_id == "r2c8"
    )
    sample.items = (
        replace(
            item,
            source_refs=(
                *item.source_refs,
                verify_source_ref(note.source_ref(), sample.original, tenant_id=TENANT),
                verify_source_ref(owner.source_ref(), sample.original, tenant_id=TENANT),
            ),
            parent_relations=tuple(
                (source, note.source_id if field == "scope" else target, field)
                for source, target, field in item.parent_relations
            ),
        ),
    )
    sample.binding = replace(
        sample.binding,
        kind="comparison",
        observation_ids=(item.observation_id,),
        reporting_period="2025",
    )
    result = sample.check()
    assert (result.status, result.reason) == ("not_computable", "value_missing_or_unresolved")


@pytest.mark.parametrize("change", ["missing", "partial", "candidate", "hash"])
def test_scope_note_requires_verified_complete_original_reference(change):
    sample = case(row("1"), footnote="Scope: Scope 1")
    item = sample.items[0]
    note = next(ref for ref in item.source_refs if ref.quote == "Scope: Scope 1")
    refs = tuple(ref for ref in item.source_refs if ref != note)
    if change == "partial":
        note = verify_source_ref(
            replace(note, quote="Scope 1", char_start=note.char_start + 7),
            sample.original,
            tenant_id=TENANT,
        )
    elif change == "candidate":
        note = replace(note, verification_state="candidate")
    elif change == "hash":
        note = replace(note, raw_text_sha256="f" * 64)
    sample.items = (replace(item, source_refs=refs if change == "missing" else (*refs, note)),)
    result = sample.check()
    assert (result.status, result.reason) == ("not_computable", "footnote_conditions_unresolved")


def test_verified_scope_note_does_not_clear_unresolved_sibling():
    sample = case(row("1"), footnote="Scope: Scope 1")
    assert sample.check().status == "consistent"
    sibling = candidate("sibling", [("F2", "footnote", "해외 사업장 제외", (1, 430, 500, 450), ())])
    graph = fuse_candidates((*sample.original.candidates, sibling), tenant_id=TENANT)
    qualities = {b.source_id: b.quality for b in sample.original.blocks}
    sample.original = replace(
        graph,
        blocks=tuple(
            replace(b, quality=qualities.get(b.source_id, b.quality)) for b in graph.blocks
        ),
    )
    result = sample.check()
    assert (result.status, result.reason) == ("not_computable", "footnote_conditions_unresolved")


@pytest.mark.parametrize("extra_relation", [False, True])
def test_table_identifier_cannot_hide_original_footnote_lineage(extra_relation):
    sample = case(row("1"), reported="1", footnote="해외 사업장 제외")
    item = sample.items[0]
    relations = item.parent_relations
    if extra_relation:
        relations = (*relations, (relations[0][0], "unknown-table", "scope"))
    sample.items = (replace(item, table_id="unknown-table", parent_relations=relations),)
    assert sample.check().status == "not_computable"


def test_parent_relation_rewrite_cannot_hide_a_cell_note():
    sample = case(row("1"), reported="1", footnote="해외 사업장 제외", cell_note=True)
    assert sample.check().status == "not_computable"
    item = sample.items[0]
    other = next(b.source_id for b in item.source_blocks if b.raw_text == "2025")
    sample.items = (
        replace(
            item,
            parent_relations=tuple(
                (other, target, role) for _, target, role in item.parent_relations
            ),
        ),
    )
    assert sample.check().status == "not_computable"


@pytest.mark.parametrize("note", ["Unit: tCO2e", "단위 : tCO2e", "unit:   tCO2e"])
def test_supported_unit_note_spacing_and_prefix_case_reach_numeric_check(note):
    assert case(row("1", unit=""), reported="1", footnote=note).check().status == "consistent"


@pytest.mark.parametrize("target", ["r1c0", "r1c3", "r1c5"])
def test_bound_field_note_is_retained_and_cannot_be_ignored(target):
    note = "해외 사업장 제외"
    sample = case(row("1"), footnote=note, note_target=target)
    assert sample.items[0].footnotes == (note,)
    assert sample.check().reason == "footnote_conditions_unresolved"
    sample.items = (replace(sample.items[0], footnotes=()),)
    assert sample.check().status == "not_computable"


@pytest.mark.parametrize("target", ["r0c8", "r0c5", "r0c6"])
def test_value_column_header_note_is_not_lost_before_numeric_check(target):
    sample = case(row("1"), footnote="해외 사업장 제외", note_target=target)
    assert sample.check().status == "not_computable"
    assert sample.items[0].footnotes == ("해외 사업장 제외",)
    sample.items = (replace(sample.items[0], footnotes=()),)
    assert sample.check().reason == "footnote_conditions_unresolved"


@pytest.mark.parametrize("boundary", ["국내", "해외", "글로벌"])
def test_matching_geographic_note_does_not_approve_organizational_boundary(boundary):
    values = row("1")
    values[5] = boundary
    sample = case(values, footnote=f"데이터 커버리지 : {boundary}")
    assert sample.check().status == "not_computable"
    assert sample.check().reason == "footnote_conditions_unresolved"
    sample.items = (replace(sample.items[0], footnotes=()),)
    assert sample.check().status == "not_computable"


def test_unassigned_same_page_note_cannot_disappear_from_numeric_check():
    sample = case(row("1"), reported="1", footnote="해외 사업장 제외")
    sample.original = replace(
        sample.original,
        edges=tuple(e for e in sample.original.edges if e.relation != "footnote_of"),
    )
    sample.items = tuple(replace(item, footnotes=()) for item in sample.items)
    assert sample.check().status == "not_computable"
    assert sample.check().reason == "footnote_conditions_unresolved"


@pytest.mark.parametrize("page,expected", [(1, "not_computable"), (2, "consistent")])
def test_orphan_note_from_independent_parser_is_scoped_to_its_page(page, expected):
    sample = case(row("1"), reported="1")
    notes = candidate("notes", [("orphan", "footnote", "해외 사업장 제외", (1, 400, 500, 420), ())])
    notes = replace(
        notes,
        blocks=tuple(
            replace(b, source=replace(b.source, physical_page=page)) for b in notes.blocks
        ),
    )
    graph = fuse_candidates((*sample.original.candidates, notes), tenant_id=TENANT)
    qualities = {b.source_id: b.quality for b in sample.original.blocks}
    sample.original = replace(
        graph,
        blocks=tuple(
            replace(b, quality=qualities.get(b.source_id, b.quality)) for b in graph.blocks
        ),
    )
    assert sample.check().status == expected
    if page == 1:
        assert sample.check().reason == "footnote_conditions_unresolved"


@pytest.mark.parametrize("target", ["missing", "self", "paragraph"])
def test_unresolvable_note_owner_cannot_clear_uncertainty(target):
    sample = case(row("1"), reported="1", footnote="해외 사업장 제외")
    note = next(b for b in sample.original.blocks if b.kind == "footnote")
    paragraph = next(b for b in sample.original.blocks if b.kind == "paragraph")
    destination = {"missing": "missing", "self": note.source_id, "paragraph": paragraph.source_id}[
        target
    ]
    sample.original = replace(
        sample.original,
        edges=tuple(
            replace(e, target_id=destination) if e.relation == "footnote_of" else e
            for e in sample.original.edges
        ),
    )
    assert sample.check().reason == "footnote_conditions_unresolved"


def test_paragraph_table_parent_edge_cannot_disguise_orphan_note():
    sample = case(row("1"), reported="1", footnote="해외 사업장 제외")
    paragraph = next(b for b in sample.original.blocks if b.kind == "paragraph")
    parent = next(e for e in sample.original.edges if e.relation == "table_parent")
    sample.original = replace(
        sample.original,
        edges=(
            *(
                replace(e, target_id=paragraph.source_id) if e.relation == "footnote_of" else e
                for e in sample.original.edges
            ),
            replace(parent, source_id=paragraph.source_id),
        ),
    )
    assert sample.check().reason == "footnote_conditions_unresolved"


def test_note_explicitly_owned_by_another_table_does_not_become_global_evidence():
    sample = case(row("1"), reported="1")
    other = candidate(
        "other-table",
        [
            ("T2", "table", "Other table", (500, 500, 590, 790), ()),
            ("F", "footnote", "해외 사업장 제외", (500, 480, 590, 495), ()),
        ],
        edges=(("F", "T2", "footnote_of"),),
    )
    graph = fuse_candidates((*sample.original.candidates, other), tenant_id=TENANT)
    qualities = {b.source_id: b.quality for b in sample.original.blocks}
    sample.original = replace(
        graph,
        blocks=tuple(
            replace(b, quality=qualities.get(b.source_id, "verified")) for b in graph.blocks
        ),
    )
    assert sample.check().status == "consistent"


def test_open_table_note_issue_cannot_be_ignored_by_numeric_check():
    from proofops.application.ingest.graph_fusion import QualityIssue

    sample = case(row("1"), reported="1")
    sample.original = replace(
        sample.original,
        issues=(
            QualityIssue(
                "note-review",
                "table_note_review",
                1,
                tuple(b.source_id for b in sample.original.blocks if b.kind != "paragraph"),
                "open",
                "Conditions unresolved",
            ),
        ),
    )
    assert sample.check().status == "not_computable"
    assert sample.check().reason == "source_issue_unresolved"


def test_observation_hold_details_keep_original_notes_and_parent_issues():
    from proofops.application.ingest.graph_fusion import QualityIssue
    from proofops.domain.numeric import observation_source_holds

    sample = case(row("1"), footnote="해외 사업장 제외")
    item = sample.items[0]
    note = next(b for b in sample.original.blocks if b.kind == "footnote")
    issue = QualityIssue(item.table_id, "table_note_review", 1, (item.table_id,), "open", "test")
    graph = replace(sample.original, issues=(*sample.original.issues, issue))
    stripped = replace(
        item,
        footnotes=(),
        source_refs=tuple(r for r in item.source_refs if r.source_id != note.source_id),
    )
    holds = observation_source_holds(stripped, graph)
    assert holds["issue_ids"] == [issue.issue_id]
    assert holds["note_source_ids"] == [note.source_id]
    assert "footnote_condition_unsupported_or_mismatched" in holds["reasons"]
    assert "footnote_source_unverified" in holds["reasons"]
    plain = case(row("1"), footnote="단위: tCO2e")
    assert observation_source_holds(plain.items[0], plain.original) == dict(
        observation_id=plain.items[0].observation_id, issue_ids=[], note_source_ids=[], reasons=[]
    )
    unassigned = replace(graph, edges=tuple(e for e in graph.edges if e.relation != "footnote_of"))
    holds = observation_source_holds(item, unassigned)
    assert holds["note_source_ids"] == [note.source_id]
    assert "footnote_owner_unresolved" in holds["reasons"]


def test_duplicate_original_note_cannot_disappear_from_hold_diagnostics():
    from proofops.domain.numeric import observation_source_holds

    sample = case(row("1"), footnote="단위: tCO2e")
    note = next(b for b in sample.original.blocks if b.kind == "footnote")
    duplicate = replace(sample.original, blocks=(*sample.original.blocks, note))
    holds = observation_source_holds(sample.items[0], duplicate)
    assert "footnote_source_ambiguous" in holds["reasons"]
    assert holds["note_source_ids"] == [note.source_id]
    assert sample.check(original=duplicate).status == "not_computable"


@pytest.mark.parametrize("winner", [999, -1, True, "0"])
def test_hold_diagnostics_tolerate_invalid_note_selection_after_other_holds(winner):
    from proofops.domain.numeric import observation_source_holds

    sample = case(row("1"), footnote="단위: tCO2e")
    item = replace(sample.items[0], source_refs=())
    graph = replace(
        sample.original,
        blocks=tuple(
            replace(b, winner=winner) if b.kind == "footnote" else b for b in sample.original.blocks
        ),
    )
    holds = observation_source_holds(item, graph)
    assert "footnote_source_unverified" in holds["reasons"]
    assert holds["note_source_ids"]


def test_hold_diagnostics_retain_root_issue_when_value_ref_is_removed():
    from proofops.application.ingest.graph_fusion import QualityIssue
    from proofops.domain.numeric import observation_source_holds

    sample = case(row("1"))
    item = replace(sample.items[0], source_refs=())
    root = item.parent_relations[0][0]
    issue = QualityIssue(root, "table_value_unreadable", 1, (root,), "open", "test")
    graph = replace(sample.original, issues=(issue,))
    assert observation_source_holds(item, graph)["issue_ids"] == [issue.issue_id]


# ---------------------------------------------------------------------------
# R30: bounded temporal growth and same-period different-product relative
# reduction. Explicitly synthetic confirmed sources, never model/customer
# approval. Both new kinds reuse Fraction + the existing display rounding
# intervals and the same verified-source guards as the existing kinds.
# ---------------------------------------------------------------------------


def _carve(claim_ref, literal, original):
    """A verified sub-span of the accepted claim quote, exactly like a number ref."""
    start = claim_ref.char_start + claim_ref.quote.index(literal)
    return verify_source_ref(
        replace(claim_ref, quote=literal, char_start=start, char_end=start + len(literal)),
        original,
        tenant_id=TENANT,
    )


def product_case(
    baseline_value,
    current_value,
    *,
    period="2025",
    baseline_subject="EV4",
    current_subject="SUV",
    reported="27",
    accepted=True,
):
    """Same period, deliberately different product subject; all else identical."""
    from proofops.domain.numeric import ClaimBinding

    statement = f"{reported} {current_subject} 는 동급 {baseline_subject} 대비 저감 배출량 수치."
    graph = fuse_candidates(
        (
            table(
                [
                    [
                        "지표",
                        "Scope",
                        "사업장",
                        "연도",
                        "산정방식",
                        "조직경계",
                        "단위",
                        "분모",
                        "값",
                    ],
                    row(baseline_value, period=period, subject=baseline_subject),
                    row(current_value, period=period, subject=current_subject),
                ]
            ),
            candidate("claim", [("C", "paragraph", statement, (1, 500, 590, 520), ())]),
        ),
        tenant_id=TENANT,
    )
    graph = replace(graph, blocks=tuple(replace(b, quality="verified") for b in graph.blocks))
    normalized = normalize_tables(graph, tenant_id=TENANT).observations
    items = tuple(
        replace(
            o,
            quality="verified",
            source_refs=tuple(
                verify_source_ref(r, graph, tenant_id=TENANT)
                for r in o.source_refs
                if r.quote.strip()
            ),
        )
        for o in normalized
    )
    discovered = discover_atomic_claims(
        graph, ClaimScope(TENANT, VERSION, MANIFEST), extractor=SyntheticClaimExtractor()
    )
    claim = next(c for c in discovered.claims if c.quote.startswith(reported + " "))
    claim = replace(
        claim,
        source_refs=tuple(verify_source_ref(r, graph, tenant_id=TENANT) for r in claim.source_refs),
    )
    claim_ref = claim.source_refs[0]
    number_ref = _carve(claim_ref, reported, graph)
    baseline, current = items
    binding = ClaimBinding(
        claim_id=claim.claim_id,
        tenant_id=TENANT,
        document_version_id=VERSION,
        parse_manifest_id=MANIFEST,
        kind="product_reduction",
        observation_ids=(baseline.observation_id, current.observation_id),
        reported_value=reported,
        metric_raw=current.metric_raw,
        scope=current.scope,
        subject=current_subject,
        reporting_period=period,
        baseline_period=period,
        baseline_subject=baseline_subject,
        scope2_basis=current.scope2_basis,
        organizational_boundary=current.organizational_boundary,
        unit=current.unit_canonical,
        denominator=current.denominator,
        source_refs=claim.source_refs,
        reported_value_ref=number_ref,
        subject_ref=_carve(claim_ref, current_subject, graph),
        baseline_subject_ref=_carve(claim_ref, baseline_subject, graph),
        product_comparison_accepted=accepted,
        binding_accepted=True,
        quantity_kind="absolute",
    )
    return Case(items, binding, (claim,), graph)


def test_growth_uses_temporal_rate_and_matches_reported_percent():
    # (644685/598846 - 1) * 100 ~= 7.65, consistent with the reported ~8 band 7.5-8.5.
    c = case(row("598846", "2023"), row("644685", "2024"), kind="growth", reported="8")
    result = c.check()
    assert result.status == "consistent"
    # Recurring quotient -> exact ratio preserved, never fabricated precision.
    assert result.computed_value is None
    assert result.reason == "non_terminating_decimal"
    assert result.exact_ratio is not None


def test_growth_terminating_rate_is_exact_and_can_be_inconsistent():
    c = case(row("100.00", "2023"), row("110.00", "2024"), kind="growth", reported="10.00")
    assert c.check().status == "consistent"
    assert c.check().computed_value == "10"
    c = case(row("100.000", "2023"), row("110.000"), kind="growth", reported="9.0")
    assert c.check().status == "inconsistent"


def test_growth_requires_distinct_periods_like_reduction():
    c = case(row("100.00", "2023"), row("110.00", "2024"), kind="growth", reported="10.00")
    for changes in ({"baseline_period": None}, {"reporting_period": "2023"}):
        c.binding = replace(c.binding, **changes)
        assert c.check().status == "not_comparable"


@pytest.mark.parametrize("baseline", ["0", "-", "0.0"])
def test_growth_zero_or_missing_baseline_is_not_computable(baseline):
    c = case(row(baseline, "2023"), row("10"), kind="growth", reported="10")
    assert c.check().status == "not_computable"


def test_growth_dimension_mismatch_including_subject_is_not_comparable():
    c = case(row("100.00", "2023"), row("110.00", "2024"), kind="growth", reported="10.00")
    c.items = (replace(c.items[0], subject="부산"), c.items[1])
    # A mismatched dimension is never a numeric finding (not consistent/inconsistent).
    assert c.check().status not in ("consistent", "inconsistent")


def test_product_reduction_same_period_matches_reported_percent():
    # (1 - 35.40/48.73) * 100 ~= 27.35, consistent with the reported ~27 band 26.5-27.5.
    c = product_case("48.73", "35.40", reported="27")
    result = c.check()
    assert result.status == "consistent"
    assert result.computed_value is None
    assert result.reason == "non_terminating_decimal"
    assert result.exact_ratio is not None


def test_product_reduction_terminating_rate_is_exact():
    c = product_case("100.00", "75.00", reported="25.00")
    assert c.check().status == "consistent"
    assert c.check().computed_value == "25"
    c = product_case("100.000", "75.000", reported="20.0")
    assert c.check().status == "inconsistent"


def test_product_reduction_requires_same_period():
    c = product_case("48.73", "35.40", reported="27")
    c.binding = replace(c.binding, baseline_period="2024")
    assert c.check().status == "not_comparable"


def test_product_reduction_requires_deliberately_different_subject():
    c = product_case("48.73", "35.40", baseline_subject="SUV", current_subject="SUV", reported="0")
    # Same subject on both sides is not a different-product comparison.
    assert c.check().status == "not_comparable"


def test_product_reduction_requires_explicit_product_acceptance():
    c = product_case("48.73", "35.40", reported="27", accepted=False)
    assert c.check().status == "not_comparable"
    assert c.check().reason == "product_comparison_unaccepted"


def test_product_reduction_requires_verified_subject_source_spans():
    c = product_case("48.73", "35.40", reported="27")
    # A forged/absent baseline subject ref cannot be synthesized from the number.
    c.binding = replace(c.binding, baseline_subject_ref=None)
    assert c.check().status == "not_comparable"
    c = product_case("48.73", "35.40", reported="27")
    ref = c.binding.baseline_subject_ref
    c.binding = replace(
        c.binding, baseline_subject_ref=replace(ref, quote="FORGED", raw_text_sha256="f" * 64)
    )
    assert c.check().status == "not_comparable"


def test_product_reduction_subject_literal_must_match_observation_subjects():
    c = product_case("48.73", "35.40", reported="27")
    # Observation subject that does not equal the accepted product literal is refused.
    c.items = (replace(c.items[0], subject="세단"), c.items[1])
    assert c.check().status not in ("consistent", "inconsistent")


def test_product_reduction_zero_baseline_is_not_computable():
    c = product_case("0", "35.40", reported="100")
    assert c.check().status == "not_computable"


def test_product_reduction_dimension_mismatch_other_than_subject_is_not_comparable():
    c = product_case("48.73", "35.40", reported="27")
    c.items = (replace(c.items[0], scope="Scope 2"), c.items[1])
    assert c.check().status not in ("consistent", "inconsistent")


def test_new_kinds_still_require_trusted_observations_and_claims():
    c = case(row("100.00", "2023"), row("110.00", "2024"), kind="growth", reported="10.00")
    c.items = (replace(c.items[0], quality="unverified"), c.items[1])
    assert c.check().status == "not_computable"
    c = product_case("48.73", "35.40", reported="27")
    assert c.check(original=None, claims=()).status == "not_computable"


def test_existing_reduction_behavior_is_unchanged_by_new_kinds():
    # Same-period reduction stays not_comparable; distinct-period reduction unchanged.
    c = case(row("48.73", "2024"), row("35.40", "2024"), kind="reduction", reported="27")
    assert c.check().status == "not_comparable"
    ok = case(row("100.00", "2024"), row("90.00"), kind="reduction", reported="10.00")
    assert ok.check().status == "consistent"
    assert ok.check().computed_value == "10"
