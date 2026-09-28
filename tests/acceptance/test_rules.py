"""AT-014: real pure-engine checks with explicitly synthetic source records.

No PDF/model/AWS processing is represented by these local test inputs.
"""

from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest
import yaml
from proofops.application.rulepacks import validate_rulepack
from proofops.domain.rulepacks import pack_content_hash, snapshot_from_validated
from proofops.domain.values import SourceRef

ROOT = Path(__file__).resolve().parents[2]
TENANT = "11111111-1111-4111-8111-111111111111"
DOCUMENT = "22222222-2222-4222-8222-222222222222"
CLAIM = "33333333-3333-4333-8333-333333333333"
SOURCE = SourceRef(
    CLAIM,
    DOCUMENT,
    DOCUMENT,
    1,
    None,
    (1, 1, 10, 10),
    "a" * 64,
    "synthetic evidence",
    0,
    18,
    "located",
    "verified",
)


def pack(legacy_gap_003=False):
    data = yaml.safe_load((ROOT / "config/rule_pack_manifest.yaml").read_text())
    files = {p: yaml.safe_load((ROOT / "config" / p).read_text()) for p in data["files"]}
    if legacy_gap_003:
        # impl1 semantics frozen in earlier run snapshots: additional elements block.
        for track in ("goal", "performance", "management"):
            rubric = files[f"rubric/{track}.yaml"]
            del rubric["additional_element_policy"]
            rubric["additional_rubric_gap"] = "GAP-003"
        data["unresolved_gap_ids"] = sorted({*data["unresolved_gap_ids"], "GAP-003"})
    data.update(rule_pack_id=CLAIM, tenant_id=TENANT, approved_by=None, approved_at=None)
    data["sha256"] = pack_content_hash(data, files)
    assert validate_rulepack(data, files, [f"GAP-{i:03}" for i in range(1, 11)]).ok
    return snapshot_from_validated(data, files)


def fact(name, state="present", **changes):
    from proofops.domain.rules.engine import ConfirmedFact

    values = dict(
        name=name,
        state=state,
        evidence_refs=(SOURCE,) if state == "present" else (),
        source_tenant_id=TENANT,
        citation_verified=state == "present",
        binding_accepted=state == "present",
        search_coverage_verified=state == "absent",
        source_scope="local_claim",
        normalized_value=None,
    )
    if name == "assurance_covered":
        values.update(
            source_scope="global_bound",
            normalized_value="covered" if state == "present" else "not_covered",
        )
    if name == "numerical_check":
        values["source_scope"] = "computed_check"
    values.update(changes)
    return ConfirmedFact(**values)


def inputs(track="performance", **changes):
    from proofops.domain.rules.engine import ConfirmedTags, RuleContext

    names = {
        "performance": (
            "quantitative_or_qualified_ordinal",
            "unit_or_qualified_ordinal",
            "comparison_baseline",
            "calculation_boundary",
            "method",
            "assurance_covered",
            "numerical_check",
        ),
        "goal": (
            "target_year",
            "target_metric",
            "baseline_year",
            "baseline_value",
            "scope",
            "org_boundary",
            "current_progress",
            "transition_plan",
        ),
        "management": (
            "named_means_or_concrete_state",
            "org_boundary",
            "external_verification",
            "concrete_implementation_detail",
        ),
    }[track]
    facts = {name: fact(name) for name in names}
    for name in (
        "offset_or_carbon_neutral_claim",
        "science_based_claim",
        "reduction_or_improvement_claim",
        "governance_claim",
        "compensation_link_claim",
        "willingness_only",
    ):
        facts[name] = fact(name, "absent")
    for name, state in changes.items():
        facts[name] = fact(name, state)
    tags = ConfirmedTags(
        tenant_id=TENANT,
        document_version_id=DOCUMENT,
        claim_id=CLAIM,
        track=track,
        facts=tuple(facts.values()),
        tag_revision=1,
        packet_sha256="b" * 64,
        model_sha256="c" * 64,
        prompt_sha256="d" * 64,
        replicate_hashes=("1" * 64, "2" * 64, "3" * 64),
        ontology_version=pack().ontology_version,
    )
    context = RuleContext(
        tenant_id=TENANT,
        document_version_id=DOCUMENT,
        claim_id=CLAIM,
        packet_sha256="b" * 64,
        local_synthetic=True,
    )
    return tags, context


def evaluate(tags, context):
    from proofops.domain.rules.engine import evaluate as run

    return run(tags, context, pack())


@pytest.mark.parametrize(
    "method,assurance,grade",
    [
        ("absent", "absent", "E2"),
        ("present", "absent", "E2"),
        ("absent", "present", "E2"),
        ("present", "present", "E3"),
    ],
)
def test_performance_method_and_covered_assurance(method, assurance, grade):
    decision = evaluate(*inputs(method=method, assurance_covered=assurance))
    assert decision.evidence_grade == grade
    assert decision.label == ("SUBSTANTIATED" if grade == "E3" else "INCOMPLETE")
    assert decision.decision_status == "decided"


@pytest.mark.parametrize(
    "track,changes,grade",
    [
        ("goal", {}, "E3"),
        ("goal", {"transition_plan": "absent"}, "E2"),
        ("goal", {"scope": "absent"}, "E1"),
        ("goal", {"target_year": "absent"}, "E1"),
        ("goal", {"target_year": "absent", "transition_plan": "absent"}, "E0"),
        ("goal", {"target_metric": "absent"}, None),
        ("performance", {"quantitative_or_qualified_ordinal": "absent"}, "E0"),
        ("performance", {"unit_or_qualified_ordinal": "absent"}, None),
        ("performance", {"calculation_boundary": "absent"}, "E1"),
        ("management", {}, "E3"),
        ("management", {"external_verification": "absent"}, "E2"),
        ("management", {"org_boundary": "absent"}, "E1"),
        (
            "management",
            {"named_means_or_concrete_state": "absent", "willingness_only": "present"},
            "E0",
        ),
    ],
)
def test_explicit_ladders_and_unmapped_edges(track, changes, grade):
    result = evaluate(*inputs(track, **changes))
    assert result.evidence_grade == grade
    assert result.decision_status == ("decided" if grade else "blocked_rule_gap")
    if grade is None:
        assert "GAP-007" in result.gap_ids


@pytest.mark.parametrize("state", ["unknown", "conflict"])
def test_grade_relevant_unresolved_is_not_absent(state):
    result = evaluate(*inputs(method=state))
    assert result.decision_status == "blocked_evidence"
    assert result.evidence_grade is None
    assert "P3" in result.unresolved_elements
    assert "P3" not in result.missing_elements


def test_unknown_that_cannot_change_grade_does_not_block():
    result = evaluate(*inputs(method="unknown", assurance_covered="absent"))
    assert result.evidence_grade == "E2"
    assert "P3" in result.unresolved_elements
    assert result.review_status == "needs_review"


@pytest.mark.parametrize(
    "track,changes,missing",
    [
        ("performance", {"numerical_check": "absent"}, "P6"),
        ("management", {"concrete_implementation_detail": "absent"}, "M4"),
        ("goal", {"offset_or_carbon_neutral_claim": "present", "offset_plan": "absent"}, "G7"),
    ],
)
def test_additional_rubric_gaps_keep_candidate_private(track, changes, missing):
    from proofops.domain.rules.engine import evaluate as run

    result = run(*inputs(track, **changes), pack(legacy_gap_003=True))
    assert result.evidence_grade is None and result.label is None
    assert result.ladder_candidate == "E3"
    assert result.decision_status == "blocked_rule_gap"
    assert "GAP-003" in result.gap_ids and missing in result.missing_elements


@pytest.mark.parametrize(
    "track,changes,missing",
    [
        ("performance", {"numerical_check": "absent"}, "P6"),
        ("management", {"concrete_implementation_detail": "absent"}, "M4"),
        ("goal", {"offset_or_carbon_neutral_claim": "present", "offset_plan": "absent"}, "G7"),
    ],
)
def test_additional_elements_are_reported_without_changing_ladder_grade(track, changes, missing):
    # User decision 2026-09-25 (GAP-003 option 가): grade from the §4.4 ladder only.
    result = evaluate(*inputs(track, **changes))
    assert result.decision_status == "decided"
    assert result.evidence_grade == "E3" and result.label == "SUBSTANTIATED"
    assert missing in result.missing_elements
    assert "GAP-003" not in result.gap_ids


@pytest.mark.parametrize(
    "track,changes,unresolved",
    [
        ("performance", {"numerical_check": "unknown"}, "P6"),
        ("management", {"concrete_implementation_detail": "unknown"}, "M4"),
    ],
)
def test_unresolved_additional_element_keeps_grade_but_needs_review(track, changes, unresolved):
    result = evaluate(*inputs(track, **changes))
    assert result.decision_status == "decided" and result.evidence_grade == "E3"
    assert unresolved in result.unresolved_elements
    assert result.review_status == "needs_review"


def test_unknown_additional_element_policy_fails_closed():
    from proofops.domain.errors import DomainValidationError
    from proofops.domain.rules.engine import evaluate as run

    data = yaml.safe_load((ROOT / "config/rule_pack_manifest.yaml").read_text())
    files = {p: yaml.safe_load((ROOT / "config" / p).read_text()) for p in data["files"]}
    files["rubric/management.yaml"]["additional_element_policy"] = "lower_one_grade"
    data.update(rule_pack_id=CLAIM, tenant_id=TENANT, approved_by=None, approved_at=None)
    data["sha256"] = pack_content_hash(data, files)
    with pytest.raises(DomainValidationError):
        run(*inputs("management"), snapshot_from_validated(data, files))


def test_current_pack_records_gap_003_resolution():
    data = yaml.safe_load((ROOT / "config/rule_pack_manifest.yaml").read_text())
    assert "GAP-003" not in data["unresolved_gap_ids"]
    for track in ("goal", "performance", "management"):
        rubric = yaml.safe_load((ROOT / f"config/rubric/{track}.yaml").read_text())
        assert rubric["additional_element_policy"] == "report_without_grade_effect"
        assert "additional_rubric_gap" not in rubric


@pytest.mark.parametrize(
    "changes",
    [
        {"evidence_refs": ()},
        {"citation_verified": False},
        {"binding_accepted": False},
        {"source_tenant_id": DOCUMENT},
        {"source_scope": "global_bound"},
        {"evidence_refs": (replace(SOURCE, document_version_id=CLAIM),)},
        {"evidence_refs": (replace(SOURCE, verification_state="candidate"),)},
        {"evidence_refs": (replace(SOURCE, location_quality="unreadable"),)},
    ],
)
def test_unverified_or_wrongly_bound_present_rejected(changes):
    tags, context = inputs("goal")
    with pytest.raises(ValueError):
        bad = fact("target_year", **changes)
        evaluate(
            replace(tags, facts=tuple(bad if f.name == bad.name else f for f in tags.facts)),
            context,
        )


def test_absence_requires_search_coverage_and_na_requires_applicability():
    with pytest.raises(ValueError):
        fact("method", "absent", search_coverage_verified=False)
    result = evaluate(*inputs(method="not_applicable"))
    assert result.evidence_grade is None
    assert "P3" in result.unresolved_elements
    assert "P3" not in result.excluded_elements


def test_coverage_status_cannot_be_replaced_by_provider_name():
    tags, context = inputs()
    bad = fact("assurance_covered", normalized_value="Provider Name")
    with pytest.raises(ValueError):
        evaluate(
            replace(tags, facts=tuple(bad if f.name == bad.name else f for f in tags.facts)),
            context,
        )


def test_hash_reproducibility_and_immutable_revisions():
    tags, context = inputs()
    first = evaluate(tags, context)
    assert first == evaluate(tags, context)
    assert (
        first.semantic_hash
        == evaluate(replace(tags, facts=tags.facts[::-1]), context).semantic_hash
    )
    assert (
        first.semantic_hash
        != evaluate(replace(tags, prompt_sha256="e" * 64), context).semantic_hash
    )
    second = evaluate(replace(tags, tag_revision=2), replace(context, decision_revision=2))
    assert first.tag_revision == first.decision_revision == 1
    assert second.tag_revision == second.decision_revision == 2
    with pytest.raises(FrozenInstanceError):
        first.evidence_grade = "E0"


def test_tenant_packet_and_ontology_mismatch_fail_closed():
    tags, context = inputs()
    for bad in (replace(context, tenant_id=DOCUMENT), replace(context, packet_sha256="f" * 64)):
        with pytest.raises(ValueError):
            evaluate(tags, bad)
    with pytest.raises(ValueError):
        evaluate(replace(tags, ontology_version="different"), context)


def test_draft_is_only_for_explicit_local_synthetic_use():
    tags, context = inputs()
    with pytest.raises(ValueError):
        evaluate(tags, replace(context, local_synthetic=False))


def test_api_projection_matches_fixed_decision_schema():
    import json

    from jsonschema import Draft202012Validator

    schema = json.loads((ROOT / "contracts/jsonschema/api_models.schema.json").read_text())
    for changes in ({}, {"method": "unknown"}, {"numerical_check": "absent"}):
        result = evaluate(*inputs(**changes))
        Draft202012Validator({"$ref": "#/$defs/Decision", "$defs": schema["$defs"]}).validate(
            result.to_api_dict()
        )
        assert "ladder_candidate" not in result.to_api_dict()


def test_sublabel_gap_does_not_hide_known_grade():
    result = evaluate(*inputs(method="absent"))
    assert result.evidence_grade == "E2"
    assert result.sublabel is None and "GAP-005" in result.gap_ids


def test_highest_explicit_branch_wins_independent_of_pack_order():
    from proofops.domain.rules.engine import evaluate as run

    original = pack()
    files = {path: original.file_content(path) for path in original.files}
    files["rubric/performance.yaml"]["branches"].reverse()
    from dataclasses import asdict

    metadata = asdict(original)
    metadata["sha256"] = pack_content_hash(metadata, files)
    reordered = snapshot_from_validated(metadata, files)
    tags, context = inputs()
    result = run(tags, context, reordered)
    assert result.evidence_grade == "E3"
    assert result.rule_ids == ("PERF_E3",)
    assert result.semantic_hash != run(tags, context, original).semantic_hash


def test_missing_facts_are_unknown_including_mandatory_numeric_check():
    from proofops.domain.rules.engine import evaluate as run

    tags, context = inputs()
    for name in ("method", "numerical_check"):
        result = run(
            replace(tags, facts=tuple(f for f in tags.facts if f.name != name)),
            context,
            pack(legacy_gap_003=True),
        )
        assert result.decision_status == "blocked_evidence"
        assert result.evidence_grade is None
        assert ("P3" if name == "method" else "P6") in result.unresolved_elements
    # Current pack: a missing ladder fact still blocks; missing P6 stays unknown
    # (never absent) and forces review without changing the ladder grade.
    result = evaluate(
        replace(tags, facts=tuple(f for f in tags.facts if f.name != "method")), context
    )
    assert result.decision_status == "blocked_evidence" and result.evidence_grade is None
    result = evaluate(
        replace(tags, facts=tuple(f for f in tags.facts if f.name != "numerical_check")), context
    )
    assert "P6" in result.unresolved_elements and "P6" not in result.missing_elements
    assert result.evidence_grade == "E3" and result.review_status == "needs_review"


@pytest.mark.parametrize(
    "changes,gap",
    [
        ({"safe_harbor_category": "emissions_estimate"}, "GAP-001"),
        ({"safe_harbor_category": "forward_looking", "superlative_quote": "best"}, "GAP-006"),
        ({"product_variant": True}, "GAP-007"),
    ],
)
def test_separate_task_paths_cannot_leak_a_general_grade(changes, gap):
    tags, context = inputs()
    result = evaluate(replace(tags, **changes), context)
    assert result.decision_status == "blocked_rule_gap"
    assert result.label is None and result.evidence_grade is None
    assert gap in result.gap_ids


def test_engine_import_is_pure():
    import subprocess
    import sys

    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from proofops.domain.rules.engine import evaluate; "
            'assert not any(n.startswith(("proofops.application", "proofops.adapters", '
            '"boto3", "httpx", "scripts", "legacy")) for n in sys.modules)',
        ],
        check=True,
    )


def test_source_section_seven_goal_example_has_only_explicit_sublabel():
    result = evaluate(
        *inputs(
            "goal",
            baseline_year="absent",
            baseline_value="absent",
            scope="absent",
            org_boundary="absent",
            current_progress="absent",
            transition_plan="absent",
        )
    )
    assert result.evidence_grade == "E1"
    assert result.sublabel == "IMPL"
    assert "GAP-005" not in result.gap_ids


def test_confirmed_input_rejects_mutable_values_and_non_boolean_flags():
    with pytest.raises(ValueError):
        fact("method", normalized_value=[])
    tags, _ = inputs()
    with pytest.raises(ValueError):
        replace(tags, product_variant=0)
    with pytest.raises(ValueError):
        replace(tags, safe_harbor_category=[])


def test_retired_pack_cannot_be_used_as_a_draft_demo():
    from proofops.domain.rules.engine import evaluate as run

    tags, context = inputs()
    with pytest.raises(ValueError):
        run(tags, context, replace(pack(), status="retired"))


# Plan B (user decision 2026-09-25): expose the ladder's reachable grade range
# for evidence-blocked claims without turning it into a grade or label.
@pytest.mark.parametrize(
    "changes,floor,ceiling,open_elements",
    [
        ({"org_boundary": "unknown", "external_verification": "unknown"}, "E1", "E3", ["M2", "M3"]),
        ({"external_verification": "unknown"}, "E2", "E3", ["M3"]),
    ],
)
def test_blocked_evidence_exposes_reachable_grade_range(changes, floor, ceiling, open_elements):
    import json

    from jsonschema import Draft202012Validator

    result = evaluate(*inputs("management", **changes))
    assert result.decision_status == "blocked_evidence"
    assert result.evidence_grade is None and result.label is None
    api = result.to_api_dict()
    assert api["grade_range"] == dict(floor=floor, ceiling=ceiling, open_elements=open_elements)
    schema = json.loads((ROOT / "contracts/jsonschema/api_models.schema.json").read_text())
    Draft202012Validator({"$ref": "#/$defs/Decision", "$defs": schema["$defs"]}).validate(api)


def test_decided_claim_has_no_grade_range():
    result = evaluate(*inputs("management"))
    assert result.evidence_grade == "E3" and result.to_api_dict()["grade_range"] is None


@pytest.mark.parametrize(
    "changes",
    [
        {"safe_harbor_category": "emissions_estimate"},
        {"product_variant": True},
    ],
)
def test_separate_task_paths_do_not_leak_a_grade_range(changes):
    tags, context = inputs("management", external_verification="unknown")
    result = evaluate(replace(tags, **changes), context)
    assert result.evidence_grade is None and result.to_api_dict()["grade_range"] is None


def test_legacy_pack_additional_block_keeps_range_private():
    from proofops.domain.rules.engine import evaluate as run

    tags, context = inputs(
        "performance", method="unknown", numerical_check="unknown", assurance_covered="absent"
    )
    result = run(tags, context, pack(legacy_gap_003=True))
    assert result.evidence_grade is None and result.to_api_dict()["grade_range"] is None


def test_stored_decision_without_range_fields_still_loads():
    from dataclasses import asdict

    from proofops.domain.rules.engine import Decision

    stored = asdict(evaluate(*inputs("management", external_verification="unknown")))
    for key in ("grade_floor", "grade_ceiling", "grade_open_elements"):
        stored.pop(key)
    assert Decision(**stored).to_api_dict()["grade_range"] is None


def test_api_dto_accepts_engine_range_and_rejects_invalid_ranges():
    from proofops_api.dto import Decision as DecisionDTO
    from pydantic import ValidationError

    api = evaluate(*inputs("management", external_verification="unknown")).to_api_dict()
    assert DecisionDTO.model_validate(api).model_dump(mode="json")["grade_range"] == dict(
        floor="E2", ceiling="E3", open_elements=["M3"]
    )
    old = {key: value for key, value in api.items() if key != "grade_range"}
    assert DecisionDTO.model_validate(old).grade_range is None
    for bad in (
        dict(floor="E3", ceiling="E1", open_elements=["M3"]),
        dict(floor="E1", ceiling="E3", open_elements=[]),
    ):
        with pytest.raises(ValidationError):
            DecisionDTO.model_validate(api | {"grade_range": bad})
    decided = evaluate(*inputs("management")).to_api_dict()
    with pytest.raises(ValidationError):
        DecisionDTO.model_validate(decided | {"grade_range": api["grade_range"]})
