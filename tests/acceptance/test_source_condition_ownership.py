"""Pure ownership checks over synthetic original canonical source evidence."""

from copy import deepcopy
from dataclasses import asdict, replace
from hashlib import sha256

import pytest
from proofops.application.ingest.graph_fusion import CandidateEdge, fuse_candidates
from proofops.domain.errors import DomainValidationError
from proofops.domain.provenance import canonical_hash

from tests.acceptance.test_citations import TENANT, snapshot


def case():
    graph, _ = snapshot("단위: tCO2e")
    candidate = graph.candidates[0].blocks[0]
    blocks = tuple(
        replace(
            candidate,
            kind=kind,
            source=replace(
                candidate.source, source_native_id=name, raw_text=text, char_end=len(text)
            ),
            table_native_id="T" if name == "C" else None,
            row_number=0 if name == "C" else None,
            column_number=1 if name == "C" else None,
            row_span=1 if name == "C" else None,
            column_span=1 if name == "C" else None,
        )
        for name, kind, text in [
            ("N", "footnote", "단위: tCO2e"),
            ("T", "table", "Emissions"),
            ("C", "table_cell", "42"),
        ]
    )
    graph = fuse_candidates(
        (
            replace(
                graph.candidates[0],
                blocks=blocks,
                edges=(
                    CandidateEdge("N", "C", "footnote_of"),
                    CandidateEdge("C", "T", "table_parent"),
                ),
            ),
        ),
        tenant_id=TENANT,
    )
    graph = replace(graph, blocks=tuple(replace(b, quality="verified") for b in graph.blocks))
    by_kind = {b.kind: b for b in graph.blocks}
    note, cell, table = (by_kind[k] for k in ("footnote", "table_cell", "table"))
    source = note.candidates[0].source
    fragment = dict(
        source_id=note.source_id,
        parser_run_id=source.parser_run_id,
        source_native_id=source.source_native_id,
        char_start=0,
        char_end=len(source.raw_text),
        raw_text_sha256=sha256(source.raw_text.encode()).hexdigest(),
    )
    identifier = canonical_hash(fragment)
    inventory = {identifier: fragment}
    state = {
        key: {identifier: dict(id=identifier, fragment=fragment, state=value)}
        for key, value in [("citations", "confirmed"), ("classifications", "note")]
    }
    state["ownership"] = {}
    entry = dict(
        id="a" * 64,
        fragment_id=identifier,
        state="linked",
        targets=[
            dict(
                source_id=cell.source_id,
                table_id=table.source_id,
                row=0,
                column=1,
                row_span=1,
                column_span=1,
            )
        ],
        evidence_refs=[asdict(b.source_ref()) for b in graph.blocks],
    )
    return graph, inventory, state, entry


def validate(graph, inventory, state, entry):
    from proofops.application.evidence.source_condition_ownership import (
        validate_source_condition_ownership,
    )

    return validate_source_condition_ownership(graph, inventory, state, entry, tenant_id=TENANT)


def test_canonical_explicit_ownership_is_separate_and_inputs_immutable():
    args = case()
    before = deepcopy(args)
    assert validate(*args) == dict(id="a" * 64, state="accepted", reasons=[])
    assert args == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("row", 1),
        ("column", 2),
        ("row_span", 2),
        ("column_span", 2),
        ("row", False),
        ("table_id", TENANT),
        ("source_id", TENANT),
    ],
)
def test_forged_exact_targets_are_refused(field, value):
    graph, inventory, state, entry = case()
    entry["targets"][0][field] = value
    with pytest.raises(DomainValidationError):
        validate(graph, inventory, state, entry)


@pytest.mark.parametrize(
    "change",
    ["citation", "classification", "edge", "refs", "quality", "forged_ref", "fragment", "lineage"],
)
def test_missing_or_forged_proof_never_accepts(change):
    graph, inventory, state, entry = case()
    if change in ("citation", "classification"):
        state[change + "s"] = {}
    elif change == "edge":
        graph = replace(graph, edges=tuple(e for e in graph.edges if e.relation != "footnote_of"))
    elif change == "lineage":
        graph = replace(graph, edges=tuple(e for e in graph.edges if e.relation != "table_parent"))
    elif change == "refs":
        entry["evidence_refs"] = []
    elif change == "quality":
        graph = replace(graph, blocks=tuple(replace(b, quality="unverified") for b in graph.blocks))
    elif change == "fragment":
        inventory[entry["fragment_id"]]["parser_run_id"] = TENANT
    else:
        entry["evidence_refs"][0].update(quote="forged", verification_state="verified")
    result = validate(graph, inventory, state, entry)
    assert result["state"] == "unknown"
    assert result["reasons"]


def test_native_same_page_is_unknown_with_explicit_reason():
    graph, inventory, state, entry = case()
    inventory[entry["fragment_id"]] = dict(physical_page=3, native_word_indices=[0])
    result = validate(graph, inventory, state, entry)
    assert result["state"] == "unknown"
    assert "native_ownership_proof_unavailable" in result["reasons"]


def test_competing_relationship_is_not_silently_removed():
    graph, inventory, state, entry = case()
    competing = deepcopy(entry)
    competing["id"] = "other"
    competing["targets"][0]["source_id"] = TENANT
    state["ownership"]["other"] = competing
    assert validate(graph, inventory, state, entry)["state"] == "conflict"


@pytest.mark.parametrize("status", ["unknown", "conflict"])
def test_unresolved_input_does_not_invent_evidence(status):
    graph, inventory, state, entry = case()
    entry.update(state=status, targets=[], evidence_refs=[])
    assert validate(graph, inventory, state, entry)["state"] == status
    assert entry["evidence_refs"] == []


@pytest.mark.parametrize(
    "field,value", [("targets", [{}] * 17), ("evidence_refs", [{}] * 33), ("id", "not-a-digest")]
)
def test_payload_bounds_and_identifier(field, value):
    graph, inventory, state, entry = case()
    entry[field] = [deepcopy(entry[field][0])] * len(value) if isinstance(value, list) else value
    with pytest.raises(DomainValidationError):
        validate(graph, inventory, state, entry)


@pytest.mark.parametrize(
    "change", ["competing_edge", "citation_conflict", "classification_conflict"]
)
def test_original_and_effective_conflicts_remain_conflict(change):
    graph, inventory, state, entry = case()
    if change == "competing_edge":
        edge = next(e for e in graph.edges if e.relation == "footnote_of")
        graph = replace(
            graph, edges=(*graph.edges, replace(edge, target_id=entry["targets"][0]["table_id"]))
        )
    else:
        category = change.removesuffix("_conflict") + "s"
        state[category][entry["fragment_id"]]["state"] = "conflict"
    assert validate(graph, inventory, state, entry)["state"] == "conflict"


def test_table_target_requires_its_exact_identity_and_explicit_edge():
    graph, inventory, state, entry = case()
    table = next(b for b in graph.blocks if b.kind == "table")
    entry["targets"] = [
        dict(
            source_id=table.source_id,
            table_id=table.source_id,
            row=None,
            column=None,
            row_span=None,
            column_span=None,
        )
    ]
    graph = replace(
        graph,
        edges=tuple(
            replace(e, target_id=table.source_id) if e.relation == "footnote_of" else e
            for e in graph.edges
        ),
    )
    assert validate(graph, inventory, state, entry)["state"] == "accepted"


def test_cross_tenant_graph_is_refused():
    graph, inventory, state, entry = case()
    from tests.acceptance.test_citations import OTHER

    with pytest.raises(DomainValidationError):
        validate(replace(graph, tenant_id=OTHER), inventory, state, entry)


@pytest.mark.parametrize("status", ["linked", "unknown", "conflict"])
def test_unknown_fragment_cannot_be_recorded_as_original_evidence(status):
    graph, inventory, state, entry = case()
    entry.update(fragment_id="f" * 64, state=status)
    with pytest.raises(DomainValidationError):
        validate(graph, inventory, state, entry)


def test_classification_cannot_retype_canonical_cell_into_owned_note():
    graph, inventory, state, entry = case()
    note_id = inventory[entry["fragment_id"]]["source_id"]
    graph = replace(
        graph,
        blocks=tuple(
            replace(b, kind="table_cell") if b.source_id == note_id else b for b in graph.blocks
        ),
    )
    result = validate(graph, inventory, state, entry)
    assert result["state"] == "unknown"


def native_case():
    graph, _inventory, _state, entry = case()
    fragment = dict(
        note_artifact_sha256="1" * 64,
        packet_sha256="2" * 64,
        physical_page=1,
        fragment_ids=["f0"],
        native_word_indices=[10, 11],
    )
    identifier = canonical_hash(fragment)
    entry["fragment_id"] = identifier
    state = {
        category: {identifier: dict(id=identifier, fragment=fragment, state=value)}
        for category, value in (("citations", "confirmed"), ("classifications", "note"))
    }
    state["ownership"] = {}
    proof = dict(
        schema="native_note_marker_v1",
        tenant_id=TENANT,
        document_version_id=graph.document_version_id,
        parse_manifest_id=graph.parse_manifest_id,
        source_sha256=graph.source_sha256,
        graph_sha256=canonical_hash(asdict(graph)),
        fragment_id=identifier,
        target_source_ids=[entry["targets"][0]["source_id"]],
        marker="2)",
        base_word_index=0,
        marker_word_index=1,
        note_word_indices=[10, 11],
    )
    proof["proof_sha256"] = canonical_hash(proof)
    return graph, {identifier: fragment}, state, entry, proof


def test_native_original_marker_proof_needs_confirmed_note_and_verified_targets():
    from proofops.application.evidence.source_condition_ownership import (
        validate_source_condition_ownership,
    )

    graph, inventory, state, entry, proof = native_case()
    before = deepcopy((graph, inventory, state, entry, proof))
    result = validate_source_condition_ownership(
        graph, inventory, state, entry, tenant_id=TENANT, native_proof=proof
    )
    assert result == dict(
        id=entry["id"], state="accepted", reasons=[], native_proof_sha256=proof["proof_sha256"]
    )
    assert (graph, inventory, state, entry, proof) == before
    for category in ("citations", "classifications"):
        incomplete = deepcopy(state)
        incomplete[category] = {}
        assert (
            validate_source_condition_ownership(
                graph, inventory, incomplete, entry, tenant_id=TENANT, native_proof=proof
            )["state"]
            == "unknown"
        )
    entry["evidence_refs"] = []
    assert (
        validate_source_condition_ownership(
            graph, inventory, state, entry, tenant_id=TENANT, native_proof=proof
        )["state"]
        == "unknown"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("tenant_id", "0" * 36),
        ("source_sha256", "0" * 64),
        ("fragment_id", "0" * 64),
        ("target_source_ids", []),
        ("note_word_indices", [12]),
        ("marker_word_index", 10),
        ("schema", "untrusted"),
    ],
)
def test_native_proof_identity_mismatch_remains_unknown(field, value):
    from proofops.application.evidence.source_condition_ownership import (
        validate_source_condition_ownership,
    )

    graph, inventory, state, entry, proof = native_case()
    proof[field] = value
    proof["proof_sha256"] = canonical_hash({k: v for k, v in proof.items() if k != "proof_sha256"})
    result = validate_source_condition_ownership(
        graph, inventory, state, entry, tenant_id=TENANT, native_proof=proof
    )
    assert result["state"] == "unknown"


def test_native_proof_does_not_override_competing_tags_or_target_quality():
    from proofops.application.evidence.source_condition_ownership import (
        validate_source_condition_ownership,
    )

    graph, inventory, state, entry, proof = native_case()
    state["citations"][entry["fragment_id"]]["state"] = "conflict"
    assert (
        validate_source_condition_ownership(
            graph, inventory, state, entry, tenant_id=TENANT, native_proof=proof
        )["state"]
        == "conflict"
    )
    state["citations"][entry["fragment_id"]]["state"] = "confirmed"
    graph = replace(graph, blocks=tuple(replace(b, quality="unverified") for b in graph.blocks))
    proof["graph_sha256"] = canonical_hash(asdict(graph))
    proof["proof_sha256"] = canonical_hash({k: v for k, v in proof.items() if k != "proof_sha256"})
    assert (
        validate_source_condition_ownership(
            graph, inventory, state, entry, tenant_id=TENANT, native_proof=proof
        )["state"]
        == "unknown"
    )


def test_native_proof_cannot_be_supplied_in_client_ownership_entry():
    graph, inventory, state, entry, proof = native_case()
    entry["native_proof"] = proof
    with pytest.raises(DomainValidationError):
        validate(graph, inventory, state, entry)
