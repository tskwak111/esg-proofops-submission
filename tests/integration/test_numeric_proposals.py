"""Synthetic source-bound proposals; no automatic semantic approval or gold claim."""

from dataclasses import asdict, replace

import pytest
from proofops.application.evidence.binding import ClaimContext
from proofops.application.numeric_analysis import (
    analyze_numeric_consistency,
    propose_numeric_bindings,
)

from evaluation.numeric_run import _load_bindings
from tests.acceptance.test_numeric import case, row
from tests.acceptance.test_parsing import TENANT


def sample(statement="1 tCO2e 서울 2025 Scope 1 연결 시장기반", **changes):
    value = case(row("1"), reported=statement)
    claim = value.claims[0]
    source = claim.source_refs[0]
    dimensions = {}
    for role, literal in dict(
        entity="서울", metric="배출량", reporting_period="2025", scope="Scope 1", boundary="연결"
    ).items():
        start = source.quote.index(literal)
        dimensions[role] = replace(
            source, quote=literal, char_start=start, char_end=start + len(literal)
        )
    dimensions.update(changes)
    return value, ClaimContext(claim, dimensions)


def propose(value, context):
    return propose_numeric_bindings(
        tenant_id=TENANT,
        original=value.original,
        observations=value.items,
        claims=value.claims,
        contexts=(context,),
    )


def test_proposal_round_trips_review_import_then_existing_runtime(tmp_path):
    import json

    value, context = sample()
    proposal = propose(value, context)[0]
    binding = proposal.binding
    assert binding and not binding.binding_accepted
    assert binding.reported_value_ref.quote == "1"
    assert binding.unit == "tCO2e" and binding.reporting_period == "2025"
    assert "explicit_review_required" in proposal.reasons
    assert (
        analyze_numeric_consistency(
            tenant_id=TENANT,
            original=value.original,
            observations=value.items,
            claims=value.claims,
            bindings=(binding,),
        )
        .outcomes[0]
        .reason
        == "binding_not_accepted"
    )
    path = tmp_path / "reviewed.json"
    path.write_text(json.dumps({"bindings": [asdict(replace(binding, binding_accepted=True))]}))
    reviewed = _load_bindings(
        path,
        tenant_id=TENANT,
        known_source_ids={b.source_id for b in value.original.blocks},
        known_observation_ids={o.observation_id for o in value.items},
    )
    report = analyze_numeric_consistency(
        tenant_id=TENANT,
        original=value.original,
        observations=value.items,
        claims=value.claims,
        bindings=reviewed,
    )
    assert report.outcomes[0].status == "consistent"


@pytest.mark.parametrize(
    "field,new",
    [
        ("reporting_period", "2024"),
        ("unit_canonical", "kgCO2e"),
        ("subject", "부산"),
        ("scope", "Scope 2"),
    ],
)
def test_wrong_dimensions_never_bind_same_number(field, new):
    value, context = sample()
    value.items = (replace(value.items[0], **{field: new}),)
    proposal = propose(value, context)[0]
    assert proposal.binding is None
    assert proposal.reasons


@pytest.mark.parametrize(
    "text",
    [
        "1 tCO2e 서울 2025 Scope 1 연결 시장기반 목표",
        "1 tCO2e 서울 2025 Scope 1 연결 시장기반 원단위",
        "1 tCO2e 서울 2025 Scope 1 연결 시장기반 1 tCO2e",
    ],
)
def test_targets_intensity_and_ambiguous_numbers_stay_pending_without_binding(text):
    value, context = sample(text)
    assert propose(value, context)[0].binding is None


@pytest.mark.parametrize("state", ["unknown", "conflict", "unreadable", "missing"])
def test_unresolved_observation_state_is_retained(state):
    value, context = sample()
    value.items = (replace(value.items[0], value_state=state, value_decimal=None),)
    proposal = propose(value, context)[0]
    assert proposal.binding is None
    assert f"value_state:{state}" in proposal.reasons


def test_global_year_and_missing_context_are_not_filled():
    value, context = sample()
    foreign = next(r for r in value.items[0].source_refs if r.quote == "2025")
    context = ClaimContext(context.claim, dict(context.dimensions, reporting_period=foreign))
    proposal = propose(value, context)[0]
    assert proposal.binding is None
    assert "dimension_unresolved:reporting_period" in proposal.reasons


def test_number_mismatch_is_still_a_candidate_for_existing_consistency_check():
    value, context = sample("3 tCO2e 서울 2025 Scope 1 연결 시장기반")
    binding = propose(value, context)[0].binding
    assert binding.reported_value == "3"
    result = analyze_numeric_consistency(
        tenant_id=TENANT,
        original=value.original,
        observations=value.items,
        claims=value.claims,
        bindings=(replace(binding, binding_accepted=True),),
    )
    assert result.outcomes[0].status == "inconsistent"


def test_claim_scope_cue_without_source_role_never_defaults():
    value, context = sample()
    context = ClaimContext(
        context.claim, {k: v for k, v in context.dimensions.items() if k != "scope"}
    )
    value.items = (replace(value.items[0], scope=None),)
    assert "dimension_unresolved:scope" in propose(value, context)[0].reasons
    assert propose(value, context)[0].binding is None


def test_review_import_rejects_string_boolean(tmp_path):
    import json

    from evaluation.numeric_run import InputRejected

    value, context = sample()
    data = asdict(propose(value, context)[0].binding)
    data["binding_accepted"] = "false"
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps({"bindings": [data]}))
    with pytest.raises(InputRejected):
        _load_bindings(
            path,
            tenant_id=TENANT,
            known_source_ids={b.source_id for b in value.original.blocks},
            known_observation_ids={o.observation_id for o in value.items},
        )


def test_cli_exports_pending_review_file_without_overwriting(monkeypatch, tmp_path, capsys):
    from evaluation import numeric_run

    value, context = sample()
    proposal = asdict(propose(value, context)[0])

    def frozen_run(state, bindings, *, propose=False):
        assert propose is True
        return {"proposals": [proposal]}

    monkeypatch.setattr(numeric_run, "run", frozen_run)
    path = tmp_path / "pending.json"
    argv = ["--state", str(tmp_path), "--propose-bindings", str(path)]
    assert numeric_run.main(argv) == 0
    assert '"binding_accepted": false' in path.read_text()
    original = path.read_bytes()
    assert numeric_run.main(argv) == 2
    assert path.read_bytes() == original
    capsys.readouterr()


def test_no_normalized_observations_is_an_explicit_hold():
    value, context = sample()
    value.items = ()
    proposal = propose(value, context)[0]
    assert proposal.binding is None
    assert "normalized_observations_absent" in proposal.reasons
