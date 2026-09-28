from copy import deepcopy
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest
from proofops.application.claims import Claim, ExtractionProfile, ExtractionReceipt
from proofops.application.ingest.graph_fusion import CandidateBatch, CandidateBlock, fuse_candidates
from proofops.domain.documents import NativeSource, PageGeometry
from proofops.domain.provenance import canonical_hash

from tests.acceptance.test_binding import span
from tests.acceptance.test_citations import MANIFEST, OTHER, RUN, TENANT, VERSION


def case():
    texts = [
        "그린팩토리",
        "태양광: 2025년 전력 발전량 21.3 MWh",
        "커넥트원",
        "태양광: 2025년 전력 발전량 111.6 MWh",
    ]
    candidates = tuple(
        CandidateBlock(
            "paragraph",
            NativeSource(
                VERSION,
                MANIFEST,
                RUN,
                str(i),
                1,
                None,
                (10, 770 - i * 30, 300, 790 - i * 30),
                "pdf_bottom_left_points",
                text,
                0,
                len(text),
            ),
            PageGeometry(600, 800, 0, (0, 0, 600, 800)),
        )
        for i, text in enumerate(texts)
    )
    graph = fuse_candidates(
        (
            CandidateBatch(
                TENANT,
                VERSION,
                MANIFEST,
                "a" * 64,
                RUN,
                "synthetic",
                "1",
                "synthetic",
                "b" * 64,
                candidates,
                synthetic=True,
            ),
        ),
        tenant_id=TENANT,
    )
    graph = replace(graph, blocks=tuple(replace(b, quality="verified") for b in graph.blocks))
    refs = [next(b for b in graph.blocks if b.raw_text == t).source_ref() for t in texts]
    claim = Claim(
        OTHER,
        TENANT,
        VERSION,
        MANIFEST,
        graph.source_sha256,
        texts[1],
        (refs[1],),
        "verified",
        (),
        ExtractionReceipt(
            refs[1].source_id,
            "f" * 64,
            None,
            None,
            ExtractionProfile("c" * 64, "d" * 64, "e" * 64, True),
            "ok",
        ),
    )
    p6 = SimpleNamespace(element_id="P6", evidence_refs=(refs[1], refs[3]))
    inputs = SimpleNamespace(
        run_id=RUN,
        context=SimpleNamespace(claim=claim),
        original=graph,
        tag_runs=(SimpleNamespace(guarded=SimpleNamespace(elements=(p6,))),),
        snapshot=lambda: {"claim": OTHER},
    )
    request = dict(
        policy="facility_section_context_v1",
        input_snapshot_sha256=canonical_hash(inputs.snapshot()),
        dimensions={
            k: asdict(span(refs[1], v))
            for k, v in dict(
                metric="태양광", reporting_period="2025년", value="21.3", unit="MWh"
            ).items()
        },
        section_end=asdict(refs[2]),
    )
    request["dimensions"]["facility"] = asdict(refs[0])
    return inputs, request


def test_scoped_recheck_excludes_neighbor_but_never_self_proves_numeric_consistency():
    from proofops.application.claim_context_review import review_facility_context

    inputs, request = case()
    receipt = review_facility_context(inputs, request, lambda *_: (inputs.original, {}))
    p = receipt["projection"]
    assert p["dimensions"]["facility"]["quote"] == "그린팩토리"
    assert p["numeric_check"]["status"] == "needs_review"
    assert p["numeric_check"]["reason"] == "no_comparable_table_observation"
    assert [x["status"] for x in p["numeric_check"]["considered"]] == [
        "claim_source",
        "outside_reviewed_section",
    ]
    assert receipt["numeric_result"]["outcomes"][0]["reason"] == "binding_absent"


@pytest.mark.parametrize("bad", ["period", "facility", "hash", "quote", "grade"])
def test_context_rejects_cross_scope_or_forged_inputs(bad):
    from proofops.application.claim_context_review import review_facility_context

    inputs, request = case()
    request = deepcopy(request)
    if bad == "period":
        request["dimensions"]["reporting_period"]["quote"] = "2024년"
    if bad == "facility":
        request["dimensions"]["facility"] = request["section_end"]
    if bad == "hash":
        request["input_snapshot_sha256"] = "0" * 64
    if bad == "quote":
        request["dimensions"]["value"]["quote"] = "111.6"
    if bad == "grade":
        request["grade"] = "E3"
    with pytest.raises(ValueError):
        review_facility_context(inputs, request, lambda *_: (inputs.original, {}))
