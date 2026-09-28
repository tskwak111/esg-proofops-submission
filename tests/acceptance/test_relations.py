"""AT: bounded literal cross-source role extraction; no semantic attribution."""

from dataclasses import asdict, replace

import pytest
from proofops.domain.errors import DomainValidationError

from tests.acceptance.test_binding import DIMENSIONS, bind, corpus, span
from tests.acceptance.test_citations import OTHER, TENANT


def response(*dimensions):
    return {
        "relations": [
            {
                "source_index": index,
                "dimensions": {
                    "entity": {"source_index": index, "quote": values["entity"]},
                    "metric": {"source_index": index, "quote": values["metric"]},
                    "reporting_period": {
                        "source_index": index,
                        "quote": values["reporting_period"],
                    },
                    "product": None,
                },
            }
            for index, values in enumerate(dimensions)
        ]
    }


def validate(sources, graph, raw, **kwargs):
    from proofops.application.tagging.relations import validate_relations

    return validate_relations(sources, graph, raw, tenant_id=kwargs.get("tenant_id", TENANT))


def test_relation_boundary_exists():
    import importlib.util

    assert importlib.util.find_spec("proofops.application.tagging.relations") is not None


@pytest.mark.parametrize(
    ("selection", "code"),
    [
        ("not a span", "RELATION_SPAN_SHAPE"),
        (
            {"source_index": 0, "quote": "source text that is absent"},
            "RELATION_QUOTE_ABSENT",
        ),
        ({"source_index": 0, "quote": "A"}, "RELATION_QUOTE_AMBIGUOUS"),
    ],
)
def test_invalid_relation_span_reports_safe_code_and_field(selection, code):
    graph, _, refs = corpus()
    raw = response(DIMENSIONS, DIMENSIONS)
    raw["relations"][0]["dimensions"]["entity"] = selection
    with pytest.raises(DomainValidationError) as error:
        validate(refs, graph, raw)
    assert error.value.code == code
    assert error.value.field == "relations[0].dimensions.entity"
    assert "source text that is absent" not in str(error.value)


def test_overlapping_relation_quote_is_reported_as_ambiguous():
    dimensions = DIMENSIONS | {"entity": "AAA"}
    graph, _, refs = corpus(claim_dimensions=dimensions)
    raw = response(dimensions, dimensions)
    raw["relations"][0]["dimensions"]["entity"]["quote"] = "AA"
    with pytest.raises(DomainValidationError) as error:
        validate(refs, graph, raw)
    assert error.value.code == "RELATION_QUOTE_AMBIGUOUS"
    assert error.value.field == "relations[0].dimensions.entity"
    assert "AA" not in repr((error.value.args, vars(error.value)))


def test_two_verified_catalog_sources_restore_literal_roles_and_nulls():
    graph, _, refs = corpus()
    result = validate(refs, graph, response(DIMENSIONS, DIMENSIONS))
    assert set(result) == {ref.source_id for ref in refs}
    assert result[refs[0].source_id]["product"] is None
    assert result[refs[1].source_id]["metric"].quote == DIMENSIONS["metric"]
    assert all(
        value is None or value.verification_state == "verified"
        for roles in result.values()
        for value in roles.values()
    )


def test_request_pins_hashes_and_only_indexed_untrusted_sources():
    from proofops.application.evidence.citations import verify_source_ref
    from proofops.application.tagging.relations import relation_request
    from proofops.domain.provenance import canonical_hash

    graph, _, refs = corpus()
    request = relation_request(refs, graph, tenant_id=TENANT)
    assert request["sources_sha256"] == canonical_hash(
        [asdict(verify_source_ref(ref, graph, tenant_id=TENANT)) for ref in refs]
    )
    assert {"schema", "graph_sha256", "sources_sha256", "prompt_sha256"} <= set(request)
    assert request["untrusted_document_data"]["sources"] == [
        {"source_index": index, "text": ref.quote} for index, ref in enumerate(refs)
    ]


@pytest.mark.parametrize(
    "sources, change",
    [
        ("foreign", None),
        ("foreign_tenant", None),
        ("partial", None),
        ("duplicate", None),
        ("unverified", None),
        ("malformed", None),
        ("normal", {"source_index": True}),
        ("normal", {"quote": "forged"}),
        ("normal", {"quote": "A"}),
        ("normal", {"start": 0, "end": 3}),
    ],
)
def test_source_identity_and_literal_quote_guards(sources, change):
    graph, _, refs = corpus()
    supplied = refs
    if sources == "foreign":
        supplied = (replace(refs[0], document_version_id=OTHER), refs[1])
    elif sources == "partial":
        supplied = (span(refs[0], DIMENSIONS["entity"]), refs[1])
    elif sources == "duplicate":
        supplied = (refs[0], refs[0])
    elif sources == "unverified":
        graph = replace(
            graph,
            blocks=tuple(replace(block, quality="unverified") for block in graph.blocks),
        )
    elif sources == "malformed":
        supplied = (None, refs[1])
    raw = response(DIMENSIONS, DIMENSIONS)
    if change:
        raw["relations"][0]["dimensions"]["entity"].update(change)
    with pytest.raises(DomainValidationError):
        validate(supplied, graph, raw, tenant_id=OTHER if sources == "foreign_tenant" else TENANT)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda raw: raw["relations"].append(raw["relations"][0]),
        lambda raw: raw["relations"].pop(),
        lambda raw: raw["relations"][1].update(source_index=0),
        lambda raw: raw["relations"][0].update(source_index=True),
        lambda raw: raw["relations"][0]["dimensions"].pop("reporting_period"),
        lambda raw: raw["relations"][0]["dimensions"].update(unknown=None),
        lambda raw: raw["relations"][0].update(grade="E3"),
        lambda raw: raw.update(grade="E3"),
    ],
)
def test_exact_relation_rows_and_no_grades(mutate):
    graph, _, refs = corpus()
    raw = response(DIMENSIONS, DIMENSIONS)
    mutate(raw)
    with pytest.raises(DomainValidationError):
        validate(refs, graph, raw)


def test_relation_roles_do_not_grant_cross_source_binding():
    graph, claim, refs = corpus(product="제품B")
    raw = response(DIMENSIONS, DIMENSIONS)
    raw["relations"][1]["dimensions"]["product"] = {"source_index": 1, "quote": "제품B"}
    roles = validate(refs, graph, raw)
    assert bind(graph, claim, span(refs[1], "40%"), roles[refs[1].source_id]) == "rejected"


def test_literal_catalog_roles_still_require_and_can_pass_existing_binding():
    graph, claim, refs = corpus()
    raw = response(DIMENSIONS, DIMENSIONS)
    raw["relations"][1]["dimensions"].update(
        {role: {"source_index": 1, "quote": quote} for role, quote in DIMENSIONS.items()}
    )
    roles = validate(refs, graph, raw)
    assert refs[1].source_id not in {ref.source_id for ref in claim.source_refs}
    assert bind(graph, claim, span(refs[1], "40%"), roles[refs[1].source_id]) == "accepted"
    # Catalog existence alone cannot move roles from a different table row.
    raw["relations"][1]["dimensions"]["entity"]["source_index"] = 0
    roles = validate(refs, graph, raw)
    assert bind(graph, claim, span(refs[1], "40%"), roles[refs[1].source_id]) == "rejected"
