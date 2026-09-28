"""R06g: one real discovered claim reaches the reviewed-grid numeric service.

Local integration checks against the git-ignored customer PDFs and the frozen
full-context extraction receipts. They skip when those inputs are absent and they
never write to the run directory.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from proofops.adapters.local.reviewed_claim_bridge import (
    FrozenClaimSpan,
    SourceReverification,
    bridge_case,
    bridge_provenance,
    load_frozen_claim_spans,
    matched_reviewed_candidate,
    propose_comparison_binding,
    reverify_disclosure_cell,
)
from proofops.adapters.local.reviewed_table import (
    ReviewedNumericInputs,
    reviewed_numeric_inputs,
)
from proofops.application.numeric_analysis import analyze_numeric_consistency

APP = Path(__file__).resolve().parents[2]
REVIEW = APP / "tests/fixtures/pipeline/reviewed-table-layout/kia-candidates.json"
PDF = APP / "tests/fixtures/source-documents/kia.pdf"
RECEIPTS = Path(
    os.environ.get(
        "R06G_KIA_RECEIPTS",
        str(
            APP
            / ".local/pipeline-recovery-20260920/kia-full-context-64m/extraction-receipts"
        ),
    )
)
# The E-body disclosure row on physical page 35 and the reviewed appendix grid on
# physical page 106 are different pages of one report, not one table against itself.
CLAIM_PAGE = 35
VALUE = "1,178.5"
METRIC = "총 배출량(Scope1 & 2)1"
PERIOD = "2024"
ROW_LITERALS = ["총", "배출량(Scope", "천tCO₂eq"]

# Only the real-input checks below need the local corpus; the last check is portable.
requires_real_inputs = pytest.mark.skipif(
    not (REVIEW.exists() and PDF.exists() and RECEIPTS.exists()),
    reason="reviewed layout, customer PDF or frozen receipts unavailable",
)


@pytest.fixture(scope="module")
def real_case():
    review = json.loads(REVIEW.read_text())
    source = PDF.read_bytes()
    spans = load_frozen_claim_spans(RECEIPTS, pages={CLAIM_PAGE}, contains=VALUE)
    check = reverify_disclosure_cell(
        source,
        page_num=CLAIM_PAGE,
        value_literal=VALUE,
        year_literal=PERIOD,
        row_literals=ROW_LITERALS,
    )
    inputs = reviewed_numeric_inputs(review, source, verify_context=True)
    return spans, check, inputs, source


@requires_real_inputs
def test_real_claim_reaches_the_service_and_is_not_admitted(real_case):
    spans, check, inputs, _ = real_case
    # The claim is a frozen, source-verified span of the full-context run.
    assert len(spans) == 1
    span = spans[0]
    assert span.page_num == CLAIM_PAGE
    assert VALUE in span.quote
    assert span.block_text[span.char_start : span.char_end] == span.quote

    # This module re-read the same page itself: the value is unique, its year comes
    # from the column above it and its row carries the metric and unit literals.
    # That corroborates the literals; it does not verify the source.
    assert check.value_occurrences == 1
    assert check.column_aligned and check.row_aligned
    assert check.holds == ()
    assert check.corroborated is True
    assert check.value_bbox is not None and check.year_bbox is not None

    # New provenance, and neither origin id is reused or erased.
    provenance = bridge_provenance(span, inputs)
    bridge_manifest = provenance["bridge_parse_manifest_id"]
    assert bridge_manifest not in (span.parse_manifest_id, inputs.graph.parse_manifest_id)
    assert provenance["claim_origin"]["parse_manifest_id"] == span.parse_manifest_id
    assert provenance["reviewed_origin"]["parse_manifest_id"] == inputs.graph.parse_manifest_id

    case = bridge_case(
        span=span,
        inputs=inputs,
        check=check,
        metric_raw=METRIC,
        reporting_period=PERIOD,
        value_raw=VALUE,
        unit_literal_in_claim="천tCO₂eq",
    )
    call = case["numeric_service_call"]
    assert call["bindings_supplied"] == 1
    assert call["claims_supplied"] == 0
    assert call["has_findings"] is False
    assert [item["status"] for item in call["outcomes"]] == ["needs_review"]
    assert [item["reason"] for item in call["outcomes"]] == ["binding_not_accepted"]
    # The same literal on both sides is still not a finding, and the report says why.
    assert case["reviewed_observation"]["value_raw"] == VALUE
    assert case["reviewed_observation"]["numeric_usability"] == "held"
    assert "bridge_span_is_not_a_block_of_the_reviewed_snapshot" in case["holds"]
    assert "claim_span_native_or_rendered_source_verification_not_run" in case["holds"]
    assert case["claim"]["claim_form"] == "structured_disclosure_candidate"
    assert "native_or_rendered_source_verification_of_the_claim_span" in case["not_run"]
    assert "claim_origin_parse_manifest_differs_from_reviewed_grid" in case["holds"]
    assert any(hold.startswith("unit_literal_differs_across_parses:") for hold in case["holds"])


@requires_real_inputs
def test_manual_acceptance_is_refused_by_the_pure_check(real_case):
    spans, check, inputs, _ = real_case
    record = matched_reviewed_candidate(
        inputs, metric_raw=METRIC, reporting_period=PERIOD, value_raw=VALUE
    )
    proposal = propose_comparison_binding(span=spans[0], inputs=inputs, check=check, record=record)
    assert proposal.binding_accepted is False
    assert proposal.parse_manifest_id is None
    # The reported value is the claim-side literal, and the only ref stays a candidate.
    assert proposal.reported_value == check.value_literal
    assert proposal.reported_value in spans[0].quote
    assert [ref.verification_state for ref in proposal.source_refs] == ["candidate"]

    # Marking the proposal accepted must not turn it into a comparison: the claim
    # is not a resolved source of this snapshot, so the domain refuses it.
    report = analyze_numeric_consistency(
        tenant_id=inputs.graph.tenant_id,
        original=inputs.graph,
        observations=inputs.observations,
        bindings=(replace(proposal, binding_accepted=True),),
        claims=(),
    )
    assert report.has_findings is False
    outcome = report.outcomes[0]
    assert (outcome.status, outcome.reason) == ("not_computable", "claim_source_unresolved")


@requires_real_inputs
def test_a_neighbouring_year_or_absent_value_holds_instead_of_matching(real_case):
    _, _, _, source = real_case
    # 2023 is the column left of this value; it must not be written onto it.
    wrong_year = reverify_disclosure_cell(
        source,
        page_num=CLAIM_PAGE,
        value_literal=VALUE,
        year_literal="2023",
        row_literals=ROW_LITERALS,
    )
    assert wrong_year.column_aligned is False
    assert "claim_year_header_not_column_aligned" in wrong_year.holds
    assert wrong_year.corroborated is False

    # A value literal that is not on the page cannot be attested by proximity.
    absent = reverify_disclosure_cell(
        source,
        page_num=CLAIM_PAGE,
        value_literal="9,999.9",
        year_literal=PERIOD,
        row_literals=ROW_LITERALS,
    )
    assert absent.value_occurrences == 0
    assert absent.value_bbox is None
    assert any(hold.startswith("claim_value_literal_not_unique_on_page:") for hold in absent.holds)


def test_a_receipt_span_that_disagrees_with_its_block_is_rejected(tmp_path):
    directory = tmp_path / "0000ffff-0000-5000-8000-000000000000"
    directory.mkdir()
    (directory / "packet.json").write_text(
        json.dumps(
            {
                "tenant_id": "195fb7fa-c3b3-4cba-a2e5-265da642d174",
                "document_version_id": "61041d4f-374a-413e-8f2a-fcbd76f7b6ef",
                "parse_manifest_id": "998bb7f5-c549-5364-9e68-988dfbac36d6",
                "source_sha256": "0" * 64,
                "untrusted_document_data": {
                    "kind": "paragraph",
                    "page_num": CLAIM_PAGE,
                    "source_id": "0bae5ca1-3698-5b7e-9cfc-e1e4a6d563f9",
                    "text": "총 배출량 천tCO₂eq 1,178.5",
                },
            }
        )
    )
    (directory / "result.json").write_text(
        json.dumps(
            {
                "request_id": directory.name,
                "spans": [
                    {"kind": "claim", "quote": "1,178.5", "char_start": 0, "char_end": 7},
                ],
            }
        )
    )
    with pytest.raises(ValueError, match="does not match its block"):
        load_frozen_claim_spans(tmp_path, pages={CLAIM_PAGE}, contains="1,178.5")


def test_a_mismatched_claim_value_stays_distinct_and_candidate():
    """Portable: no PDF, no receipts, no reviewed run -- only the binding boundary.

    The claimed number and the evidence cell must stay independent, because their
    disagreement is the comparison. The proposal keeps both values and stays a
    candidate proposal; nothing is admitted.
    """
    tenant = "195fb7fa-c3b3-4cba-a2e5-265da642d174"
    document = "49720efb-f773-5bd8-98aa-3c80d2fa76e4"
    sha = "d0d814d98c4aeedbbdb2bf8631b8981ae5cde94dec32aa32c57510420274da1f"
    observation = SimpleNamespace(
        observation_id="c43f313d-332f-577a-9ba8-034e2dd71c61",
        metric_raw="총 배출량(Scope1 & 2)1",
        scope=None,
        subject=None,
        scope2_basis=None,
        organizational_boundary=None,
        unit_canonical="천tCOeq\n2",
        denominator=None,
        reporting_period=PERIOD,
    )
    inputs = cast(
        "ReviewedNumericInputs",
        SimpleNamespace(
            graph=SimpleNamespace(
                tenant_id=tenant,
                document_version_id=document,
                parse_manifest_id="ba1c1ad2-0000-5000-8000-000000000001",
                source_sha256=sha,
            ),
            table_id="ba1c1ad2-0000-5000-8000-000000000002",
            observations=(observation,),
            candidates=(),
        ),
    )
    span = FrozenClaimSpan(
        receipt_id="9c1347b3-cd26-502f-aafe-ea0154910cb0",
        tenant_id=tenant,
        document_version_id="61041d4f-374a-413e-8f2a-fcbd76f7b6ef",
        parse_manifest_id="998bb7f5-c549-5364-9e68-988dfbac36d6",
        source_sha256=sha,
        block_source_id="0bae5ca1-3698-5b7e-9cfc-e1e4a6d563f9",
        page_num=CLAIM_PAGE,
        block_kind="paragraph",
        block_text="총 배출량(Scope 1 & 2)1 천tCO₂eq 1,177.4",
        char_start=0,
        char_end=38,
        quote="총 배출량(Scope 1 & 2)1 천tCO₂eq 1,177.4",
        topic_ids=("environment",),
        provider_model="solar-pro3-260323",
    )
    # The claim says 1,177.4 while the evidence cell says 1,178.5.
    check = SourceReverification(
        page_num=CLAIM_PAGE,
        page_height=595.276,
        value_literal="1,177.4",
        value_bbox=(771.0, 410.8, 793.7, 417.8),
        value_occurrences=1,
        year_literal=PERIOD,
        year_bbox=(775.3, 422.9, 793.7, 430.2),
        row_literals=(),
        missing_row_literals=(),
        column_aligned=True,
        row_aligned=True,
        holds=(),
    )
    record = {"observation_id": observation.observation_id, "value_raw": VALUE}

    binding = propose_comparison_binding(span=span, inputs=inputs, check=check, record=record)
    # Both numbers survive: the claim side is the re-read literal, the evidence side
    # keeps its own cell value, and the difference is left for the check to resolve.
    assert binding.reported_value == "1,177.4"
    assert record["value_raw"] == VALUE
    assert binding.reported_value != record["value_raw"]
    # Nothing is admitted and the corroborated re-read is still only a candidate.
    assert binding.binding_accepted is False
    assert binding.parse_manifest_id is None
    assert [ref.verification_state for ref in binding.source_refs] == ["candidate"]
    assert [ref.location_quality for ref in binding.source_refs] == ["located"]

    # A claim from another document, or a page the re-read did not cover, is refused.
    with pytest.raises(ValueError, match="different source documents"):
        propose_comparison_binding(
            span=replace(span, source_sha256="0" * 64),
            inputs=inputs,
            check=check,
            record=record,
        )
    with pytest.raises(ValueError, match="not in the claim quote"):
        propose_comparison_binding(
            span=replace(span, quote="총 배출량(Scope 1 & 2)1 천tCO₂eq", char_end=27),
            inputs=inputs,
            check=check,
            record=record,
        )
