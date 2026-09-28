"""Finite condition syntax requires exact original evidence and current source facts."""

from copy import deepcopy
from dataclasses import asdict, replace
from hashlib import sha256

import pytest
from proofops.domain.errors import DomainValidationError
from proofops.domain.provenance import canonical_hash

from tests.acceptance.test_citations import OTHER, TENANT
from tests.acceptance.test_source_condition_ownership import case as ownership_case


def case(text="단위: tCO2e", kind="unit_literal", literal="tCO2e"):
    graph, _, _, ownership = ownership_case()
    note = next(b for b in graph.blocks if b.kind == "footnote")
    old = note.candidates[0]
    candidate = replace(old, source=replace(old.source, raw_text=text, char_end=len(text)))
    note = replace(note, candidates=(candidate,))
    graph = replace(
        graph,
        blocks=tuple(note if b.kind == "footnote" else b for b in graph.blocks),
        candidates=tuple(
            replace(batch, blocks=tuple(candidate if b == old else b for b in batch.blocks))
            for batch in graph.candidates
        ),
    )
    fragment = dict(
        source_id=note.source_id,
        parser_run_id=candidate.source.parser_run_id,
        source_native_id=candidate.source.source_native_id,
        char_start=0,
        char_end=len(text),
        raw_text_sha256=sha256(text.encode()).hexdigest(),
    )
    fid = canonical_hash(fragment)
    ownership["fragment_id"] = fid
    ownership["evidence_refs"] = [asdict(b.source_ref()) for b in graph.blocks]
    state = {
        category: {fid: dict(id=fid, fragment=fragment, state=value)}
        for category, value in (("citations", "confirmed"), ("classifications", "note"))
    }
    state["ownership"] = {
        ownership["id"]: dict(
            proposal=ownership,
            assessment=dict(
                id=ownership["id"], state="accepted", reasons=[], policy_sha256="e" * 64
            ),
        )
    }
    state["conditions"] = {}
    start = text.index(literal) if literal else 0
    refs = (
        []
        if literal is None
        else [
            asdict(
                replace(
                    note.source_ref(),
                    quote=literal,
                    char_start=start,
                    char_end=start + len(literal),
                )
            )
        ]
    )
    entry = dict(
        id="b" * 64,
        fragment_id=fid,
        ownership_id=ownership["id"],
        kind=kind,
        state="tagged",
        value_refs=refs,
    )
    return graph, {fid: fragment}, state, entry


def validate(graph, inventory, state, entry):
    from proofops.application.evidence.source_condition_interpretation import (
        validate_source_condition_interpretation,
    )

    return validate_source_condition_interpretation(
        graph, inventory, state, entry, tenant_id=TENANT
    )


@pytest.mark.parametrize(
    "text,kind,literal",
    [
        ("단위: tCO2e", "unit_literal", "tCO2e"),
        (" Unit: 천 tCO₂e ", "unit_literal", "천 tCO₂e"),
        ("Scope: Scope 1", "scope_literal", "Scope 1"),
        (" Scope 3 ", "scope_literal", "Scope 3"),
    ],
)
def test_complete_literal_with_original_proof_is_separate_and_immutable(text, kind, literal):
    args = case(text, kind, literal)
    before = deepcopy(args)
    assert validate(*args) == dict(id="b" * 64, state="accepted", value=literal, reasons=[])
    assert args == before


@pytest.mark.parametrize(
    "text,kind,literal",
    [
        ("단위: tCO2e; 해외 사업장 제외", "unit_literal", "tCO2e"),
        ("Scope 1 해외 사업장 제외", "scope_literal", "Scope 1"),
        ("해외 사업장 제외", "scope_literal", "해외 사업장 제외"),
        ("데이터 커버리지: 국내", "unit_literal", "국내"),
        ("Scope: Scope 2.", "scope_literal", "Scope 2"),
        ("해외 사업장 제외", "unsupported_prose", None),
    ],
)
def test_prose_and_geography_cannot_be_accepted_by_substring(text, kind, literal):
    result = validate(*case(text, kind, literal))
    assert result["state"] == "unsupported"
    assert result["value"] is None


@pytest.mark.parametrize(
    "change",
    [
        {"quote": "kg"},
        {"char_start": 0},
        {"char_end": 200},
        {"raw_text_sha256": "0" * 64},
        {"document_version_id": OTHER},
        {"parse_manifest_id": OTHER},
        {"source_id": OTHER},
        {"page_num": 1},
        {"bbox": None},
        {"location_quality": "unreadable"},
    ],
)
def test_bad_original_refs_never_supply_value(change):
    graph, inventory, state, entry = case()
    entry["value_refs"][0].update(change, verification_state="verified")
    result = validate(graph, inventory, state, entry)
    assert result["state"] == "unknown"
    assert result["value"] is None


@pytest.mark.parametrize("category", ["citations", "classifications", "ownership"])
@pytest.mark.parametrize("status", ["unknown", "conflict"])
def test_current_withdrawal_or_conflict_revokes_interpretation(category, status):
    graph, inventory, state, entry = case()
    if category == "ownership":
        state[category][entry["ownership_id"]]["assessment"]["state"] = status
    else:
        state[category][entry["fragment_id"]]["state"] = status
    result = validate(graph, inventory, state, entry)
    assert result["state"] == status
    assert result["value"] is None


@pytest.mark.parametrize("change", ["missing", "fragment", "proposal", "assessment_id"])
def test_ownership_requires_current_accepted_same_fragment_proposal(change):
    graph, inventory, state, entry = case()
    owner = state["ownership"][entry["ownership_id"]]
    if change == "missing":
        state["ownership"] = {}
    elif change == "fragment":
        owner["proposal"]["fragment_id"] = "f" * 64
    elif change == "proposal":
        owner["proposal"]["state"] = "unknown"
    else:
        owner["assessment"]["id"] = "f" * 64
    result = validate(graph, inventory, state, entry)
    assert result["state"] == "unknown"
    assert result["value"] is None


@pytest.mark.parametrize("change", ["quality", "winner", "candidate", "hash", "offset", "native"])
def test_fragment_must_be_original_selected_and_verified(change):
    graph, inventory, state, entry = case()
    fragment = inventory[entry["fragment_id"]]
    if change in ("quality", "winner"):
        graph = replace(
            graph,
            blocks=tuple(
                replace(
                    b, **({"quality": "unverified"} if change == "quality" else {"winner": None})
                )
                if b.kind == "footnote"
                else b
                for b in graph.blocks
            ),
        )
    else:
        if change == "candidate":
            fragment["parser_run_id"] = OTHER
        elif change == "hash":
            fragment["raw_text_sha256"] = "0" * 64
        elif change == "offset":
            fragment["char_end"] = 200
        else:
            fragment.clear()
            fragment.update(physical_page=3, native_word_indices=[0])
    result = validate(graph, inventory, state, entry)
    assert result["state"] in {"unknown", "unsupported"}
    assert result["value"] is None


def test_exact_same_text_from_unrelated_original_source_is_not_fragment_evidence():
    graph, inventory, state, entry = case()
    note = next(b for b in graph.blocks if b.kind == "footnote")
    other = next(b for b in graph.blocks if b.kind == "table")
    candidate = replace(
        other.candidates[0],
        source=replace(
            other.candidates[0].source,
            raw_text=note.raw_text,
            char_end=len(note.raw_text),
        ),
    )
    updated = replace(other, candidates=(candidate,))
    graph = replace(
        graph,
        blocks=tuple(updated if b == other else b for b in graph.blocks),
        candidates=tuple(
            replace(
                batch,
                blocks=tuple(candidate if b == other.candidates[0] else b for b in batch.blocks),
            )
            for batch in graph.candidates
        ),
    )
    entry["value_refs"][0]["source_id"] = other.source_id
    assert validate(graph, inventory, state, entry)["state"] == "unknown"


def test_other_interval_on_same_source_cannot_supply_fragment_value():
    graph, inventory, state, entry = case("단위: tCO2e\n단위: tCO2e")
    old_id = entry["fragment_id"]
    fragment = dict(inventory[old_id], char_start=10)
    fid = canonical_hash(fragment)
    entry["fragment_id"] = fid
    for category in ("citations", "classifications"):
        state[category][fid] = dict(state[category].pop(old_id), id=fid, fragment=fragment)
    state["ownership"][entry["ownership_id"]]["proposal"]["fragment_id"] = fid
    result = validate(graph, {fid: fragment}, state, entry)
    assert result["state"] == "unknown"
    assert result["value"] is None


@pytest.mark.parametrize("status", ["unknown", "conflict", "unsupported"])
def test_unresolved_states_have_no_value_or_refs(status):
    graph, inventory, state, entry = case()
    entry.update(state=status, value_refs=[])
    result = validate(graph, inventory, state, entry)
    assert result["state"] == status
    assert result["value"] is None
    entry["value_refs"] = [{}]
    with pytest.raises(DomainValidationError):
        validate(graph, inventory, state, entry)


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "BAD"),
        ("fragment_id", "f" * 64),
        ("ownership_id", "F" * 64),
        ("kind", []),
        ("kind", "geography"),
        ("state", []),
        ("state", "accepted"),
        ("value_refs", None),
        ("value_refs", []),
        ("value_refs", [{}] * 33),
        ("value_refs", [{}]),
        ("value", "tCO2e"),
        ("grade", "E3"),
    ],
)
def test_malformed_entries_are_rejected(field, value):
    graph, inventory, state, entry = case()
    entry[field] = value
    with pytest.raises(DomainValidationError):
        validate(graph, inventory, state, entry)


def test_cross_tenant_is_rejected():
    graph, inventory, state, entry = case()
    with pytest.raises(DomainValidationError):
        validate(replace(graph, tenant_id=OTHER), inventory, state, entry)


@pytest.mark.parametrize("rival_state", ["tagged", "conflict"])
def test_competing_conditions_do_not_silently_override(rival_state):
    graph, inventory, state, entry = case()
    proposal = dict(entry, id="c" * 64, state=rival_state, kind="scope_literal")
    state["conditions"][proposal["id"]] = dict(
        proposal=proposal,
        assessment=dict(id=proposal["id"], state="unknown", value=None, reasons=[]),
    )
    result = validate(graph, inventory, state, entry)
    assert result["state"] == "conflict"
    assert result["value"] is None


def test_flat_competing_proposals_on_shared_target_are_order_independent():
    graph, inventory, state, entry = case("단위: tCO2e\n단위: kg")
    old_id = entry["fragment_id"]
    fragment = dict(inventory[old_id], char_end=9)
    fid = canonical_hash(fragment)
    entry["fragment_id"] = fid
    for category in ("citations", "classifications"):
        state[category][fid] = dict(state[category].pop(old_id), id=fid, fragment=fragment)
    owner = state["ownership"][entry["ownership_id"]]
    owner["proposal"]["fragment_id"] = fid
    second_fragment = dict(fragment, char_start=10, char_end=16)
    second_id = canonical_hash(second_fragment)
    second_owner = deepcopy(owner)
    second_owner["proposal"].update(id="d" * 64, fragment_id=second_id)
    second_owner["assessment"]["id"] = "d" * 64
    state["ownership"]["d" * 64] = second_owner
    for category in ("citations", "classifications"):
        state[category][second_id] = dict(
            state[category][fid], id=second_id, fragment=second_fragment
        )
    second = dict(
        entry,
        id="c" * 64,
        fragment_id=second_id,
        ownership_id="d" * 64,
        value_refs=[dict(entry["value_refs"][0], quote="kg", char_start=14, char_end=16)],
    )
    inventory = {fid: fragment, second_id: second_fragment}
    for ordered in ((entry, second), (second, entry)):
        state["conditions"] = {e["id"]: e for e in ordered}
        for proposal in ordered:
            result = validate(graph, inventory, state, proposal)
            assert result["state"] == "conflict"
            assert result["value"] is None
    # Different targets must not become relevant just because a literal differs.
    second_owner["proposal"]["targets"][0]["source_id"] = OTHER
    assert validate(graph, inventory, state, entry)["state"] == "accepted"


def test_stale_condition_assessment_is_not_authority_over_original_proposals():
    graph, inventory, state, entry = case()
    other = dict(entry, id="c" * 64)
    state["conditions"][other["id"]] = dict(
        proposal=other, assessment=dict(id=other["id"], state="accepted", value="kg", reasons=[])
    )
    assert validate(graph, inventory, state, entry)["state"] == "accepted"
