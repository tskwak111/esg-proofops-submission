"""Bridge one *real* discovered E claim toward the reviewed-table numeric input.

R06f reached ``application.numeric_analysis`` with ``claims=()`` and
``bindings=()``: no claim of either document was bound to the reviewed grid. This
module supplies the missing half from real inputs only. It reads an already frozen
extraction receipt of the full-context run (read-only), re-reads the same physical
page out of the customer PDF itself, and only then proposes a binding.

Three rules make this honest rather than a relabelling of the reviewed grid:

* No claim is manufactured out of the evidence table. The claim text must already
  exist as a span a frozen extraction receipt retained, and the value, unit, metric
  and year literals must be re-found in the PDF by this module's own read. That
  re-read is *candidate corroboration only*: it is not the native/rendered source
  verification the product requires, which stays not_run here, and a receipt's char
  offsets are not a source verification either.
* The origin refs are preserved, never rewritten. A discovered claim carries the
  full-context run's tenant, document version and parse manifest; the reviewed
  grid carries its own. This module never copies one identity onto the other. It
  mints an explicitly *new* bridge provenance (``_BRIDGE_SCHEMA``) and records
  both origins beside it.
* Acceptance stays with the operator. The proposed ``ClaimBinding`` is emitted
  with ``binding_accepted=False``; the pure domain check is still what decides,
  and it refuses this proposal because the bridge span is not a block of the
  reviewed snapshot. That refusal is the reported result, not a defect to patch.
"""

from __future__ import annotations

import io
import json
import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pdfplumber

from proofops.adapters.local.reviewed_table import (
    ReviewedNumericInputs,
    _external_review_context,
)
from proofops.application.evidence.citations import _normalized
from proofops.application.ingest.graph_fusion import CanonicalDocumentGraph
from proofops.domain.numeric import ClaimBinding
from proofops.domain.provenance import canonical_hash
from proofops.domain.values import SourceRef

# Identity of this bridge view. It is deliberately not either side's manifest.
_BRIDGE_SCHEMA = "reviewed_claim_bridge_v1"
# A column header and its value are the same column when their horizontal spans
# overlap; report layouts right-align numbers under the year label.
_COLUMN_TOL = 1.0
# A row label, its unit and its value are the same row within one line height.
_ROW_TOL = 3.0


def _uuid(name: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"proofops:{_BRIDGE_SCHEMA}:{name}"))


def _squeeze(text: str) -> str:
    return re.sub(r"\s+", "", text)


@dataclass(frozen=True, slots=True)
class FrozenClaimSpan:
    """One claim span exactly as a frozen extraction receipt retained it.

    The receipt's own quote check is what kept this span; that is not a native or
    rendered source verification and is never reported as one.
    """

    receipt_id: str
    tenant_id: str
    document_version_id: str
    parse_manifest_id: str
    source_sha256: str
    block_source_id: str
    page_num: int
    block_kind: str
    block_text: str
    char_start: int
    char_end: int
    quote: str
    topic_ids: tuple[str, ...]
    provider_model: str | None

    @property
    def origin(self) -> dict:
        """The origin provenance, kept verbatim so nothing has to be rewritten."""
        return {
            "receipt_id": self.receipt_id,
            "tenant_id": self.tenant_id,
            "document_version_id": self.document_version_id,
            "parse_manifest_id": self.parse_manifest_id,
            "source_sha256": self.source_sha256,
            "block_source_id": self.block_source_id,
            "page_num": self.page_num,
            "block_kind": self.block_kind,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "quote": self.quote,
            "topic_ids": list(self.topic_ids),
            "provider_model": self.provider_model,
        }


def load_frozen_claim_spans(
    receipts_dir: Path,
    *,
    pages: Collection[int] | None = None,
    contains: str | None = None,
) -> tuple[FrozenClaimSpan, ...]:
    """Read source-verified claim spans out of frozen extraction receipts.

    Read-only: nothing in the run directory is written, and a receipt whose
    verification rejected a quote contributes no span for it.
    """
    spans: list[FrozenClaimSpan] = []
    for result_path in sorted(receipts_dir.glob("*/result.json")):
        directory = result_path.parent
        packet_path = directory / "packet.json"
        raw_path = directory / "raw_response.json"
        if not packet_path.exists():
            continue
        packet = json.loads(packet_path.read_text())
        result = json.loads(result_path.read_text())
        block = packet.get("untrusted_document_data") or {}
        page = block.get("page_num")
        if not isinstance(page, int) or (pages is not None and page not in pages):
            continue
        raw_text = block.get("text")
        if not isinstance(raw_text, str):
            continue
        provider = None
        if raw_path.exists():
            provider = json.loads(raw_path.read_text()).get("provider_model")
        for span in result.get("spans") or ():
            if span.get("kind") != "claim":
                continue
            quote = span.get("quote")
            if not isinstance(quote, str) or (contains is not None and contains not in quote):
                continue
            start, end = span.get("char_start"), span.get("char_end")
            if type(start) is not int or type(end) is not int:
                raise ValueError("frozen claim span offsets required")
            if raw_text[start:end] != quote:
                raise ValueError(f"frozen claim span does not match its block: {directory.name}")
            spans.append(
                FrozenClaimSpan(
                    receipt_id=result.get("request_id") or directory.name,
                    tenant_id=packet["tenant_id"],
                    document_version_id=packet["document_version_id"],
                    parse_manifest_id=packet["parse_manifest_id"],
                    source_sha256=packet["source_sha256"],
                    block_source_id=block["source_id"],
                    page_num=page,
                    block_kind=block.get("kind") or "unknown",
                    block_text=raw_text,
                    char_start=start,
                    char_end=end,
                    quote=quote,
                    topic_ids=tuple(span.get("topic_ids") or ()),
                    provider_model=provider,
                )
            )
    return tuple(spans)


@dataclass(frozen=True, slots=True)
class SourceReverification:
    """This module's own text re-read of the claim page, independent of any parser.

    Corroboration of literals and their layout relationship, not an attestation.
    """

    page_num: int
    page_height: float
    value_literal: str
    value_bbox: tuple[float, float, float, float] | None
    value_occurrences: int
    year_literal: str
    year_bbox: tuple[float, float, float, float] | None
    row_literals: tuple[str, ...]
    missing_row_literals: tuple[str, ...]
    column_aligned: bool
    row_aligned: bool
    holds: tuple[str, ...]

    @property
    def corroborated(self) -> bool:
        """Every literal was re-found where the claim implies; still not verified."""
        return not self.holds


def reverify_disclosure_cell(
    source: bytes,
    *,
    page_num: int,
    value_literal: str,
    year_literal: str,
    row_literals: Sequence[str],
) -> SourceReverification:
    """Re-find the value, its year column and its row band in the PDF text itself.

    The year is attested by geometry, not by reading order: the nearest year token
    above the value whose horizontal span overlaps it. A duplicated value literal,
    a missing row literal or a year in another column is a hold, never a guess.
    """
    with pdfplumber.open(io.BytesIO(source)) as pdf:
        page = pdf.pages[page_num - 1]
        height = float(page.height)
        words = page.extract_words()
    target = _squeeze(value_literal)
    value_hits = [word for word in words if _squeeze(word["text"]) == target]
    holds: list[str] = []
    if len(value_hits) != 1:
        holds.append(f"claim_value_literal_not_unique_on_page:{len(value_hits)}")
    value_word = value_hits[0] if len(value_hits) == 1 else None

    year_bbox: tuple[float, float, float, float] | None = None
    column_aligned = False
    if value_word is not None:
        above = [
            word
            for word in words
            if _squeeze(word["text"]) == _squeeze(year_literal)
            and word["bottom"] <= value_word["top"] + _ROW_TOL
            and word["x0"] < value_word["x1"] + _COLUMN_TOL
            and word["x1"] > value_word["x0"] - _COLUMN_TOL
        ]
        if above:
            header = max(above, key=lambda word: word["top"])
            year_bbox = _pdf_bbox(header, height)
            column_aligned = True
        else:
            holds.append("claim_year_header_not_column_aligned")

    missing: list[str] = []
    row_aligned = value_word is not None
    for literal in row_literals:
        squeezed = _squeeze(literal)
        same_row = [
            word
            for word in words
            if squeezed in _squeeze(word["text"])
            and value_word is not None
            and abs(word["top"] - value_word["top"]) <= _ROW_TOL
        ]
        if not same_row:
            missing.append(literal)
            row_aligned = False
    if missing:
        holds.append("claim_row_literals_not_in_value_row:" + ",".join(missing))

    return SourceReverification(
        page_num=page_num,
        page_height=height,
        value_literal=value_literal,
        value_bbox=_pdf_bbox(value_word, height) if value_word else None,
        value_occurrences=len(value_hits),
        year_literal=year_literal,
        year_bbox=year_bbox,
        row_literals=tuple(row_literals),
        missing_row_literals=tuple(missing),
        column_aligned=column_aligned,
        row_aligned=row_aligned,
        holds=tuple(holds),
    )


def _pdf_bbox(word: dict, height: float) -> tuple[float, float, float, float]:
    return (
        float(word["x0"]),
        height - float(word["bottom"]),
        float(word["x1"]),
        height - float(word["top"]),
    )


def bridge_provenance(span: FrozenClaimSpan, inputs: ReviewedNumericInputs) -> dict:
    """An explicitly new provenance for the re-verified span; no id is reused."""
    graph = inputs.graph
    key = ":".join(
        (
            span.source_sha256,
            span.parse_manifest_id,
            graph.parse_manifest_id,
            str(span.page_num),
            sha256(span.quote.encode("utf-8")).hexdigest(),
        )
    )
    manifest_id = _uuid(f"manifest:{key}")
    if manifest_id in (span.parse_manifest_id, graph.parse_manifest_id):
        raise ValueError("bridge manifest must differ from both origins")
    return {
        "schema": _BRIDGE_SCHEMA,
        "bridge_parse_manifest_id": manifest_id,
        "bridge_source_id": _uuid(f"span:{key}"),
        "document_version_id": graph.document_version_id,
        "source_sha256": graph.source_sha256,
        "coordinate_system": "pdf_bottom_left_points",
        "claim_origin": span.origin,
        "reviewed_origin": {
            "tenant_id": graph.tenant_id,
            "document_version_id": graph.document_version_id,
            "parse_manifest_id": graph.parse_manifest_id,
            "source_sha256": graph.source_sha256,
            "table_id": inputs.table_id,
        },
    }


def bridge_claim_ref(
    span: FrozenClaimSpan, inputs: ReviewedNumericInputs, check: SourceReverification
) -> SourceRef:
    """The re-read span as a ref of the bridge parse, always a candidate.

    A text re-read corroborates the literal; it is not the native/rendered
    attestation the product requires, and an extraction receipt's char offsets are
    not a source verification either. ``verification_state`` therefore stays
    ``candidate`` unconditionally, and rendering/source verification stays not_run.
    """
    provenance = bridge_provenance(span, inputs)
    return SourceRef(
        source_id=provenance["bridge_source_id"],
        document_version_id=provenance["document_version_id"],
        parse_manifest_id=provenance["bridge_parse_manifest_id"],
        page_num=span.page_num,
        printed_page_label=None,
        bbox=check.value_bbox,
        raw_text_sha256=sha256(span.block_text.encode("utf-8")).hexdigest(),
        quote=span.quote,
        char_start=span.char_start,
        char_end=span.char_end,
        location_quality="located" if check.value_bbox else "unlocated",
        verification_state="candidate",
    )


def matched_reviewed_candidate(
    inputs: ReviewedNumericInputs, *, metric_raw: str, reporting_period: str, value_raw: str
) -> dict:
    """The reviewed record for one cell, with the holds it already carries."""
    matches = [
        record
        for record in inputs.candidates
        if record.get("metric_raw") == metric_raw
        and record.get("reporting_period") == reporting_period
        and record.get("value_raw") == value_raw
    ]
    if len(matches) != 1:
        raise ValueError(f"reviewed observation not uniquely matched: {len(matches)}")
    return matches[0]


def propose_comparison_binding(
    *,
    span: FrozenClaimSpan,
    inputs: ReviewedNumericInputs,
    check: SourceReverification,
    record: dict,
) -> ClaimBinding:
    """A proposal only: ``binding_accepted`` stays False and the manifest stays None.

    ``reported_value`` is the claim-side literal this module re-read on the claim
    page, never the evidence record's cell value. The two are deliberately kept
    independent: a disagreement between the claimed number and the evidence cell is
    the numerical comparison itself, so it must stay visible rather than be rejected
    here. What is validated is the claim side -- same source document, same page as
    the re-read, and the literal really inside the claim quote.
    ``parse_manifest_id=None`` is the truth, not a convenience: the bridge span is
    not a block of the reviewed snapshot, so the pure check must refuse it.
    """
    reported_value = check.value_literal
    if span.source_sha256 != inputs.graph.source_sha256:
        raise ValueError("claim and reviewed grid are different source documents")
    if span.page_num != check.page_num:
        raise ValueError("claim page and re-read page differ")
    if reported_value not in span.quote:
        raise ValueError("re-read value literal is not in the claim quote")
    observation = next(
        item for item in inputs.observations if item.observation_id == record["observation_id"]
    )
    return ClaimBinding(
        claim_id=_uuid(f"claim:{span.receipt_id}:{span.char_start}:{span.char_end}"),
        tenant_id=inputs.graph.tenant_id,
        document_version_id=inputs.graph.document_version_id,
        kind="comparison",
        observation_ids=(observation.observation_id,),
        reported_value=reported_value,
        metric_raw=observation.metric_raw,
        scope=observation.scope,
        subject=observation.subject,
        scope2_basis=observation.scope2_basis,
        organizational_boundary=observation.organizational_boundary,
        unit=observation.unit_canonical,
        denominator=observation.denominator,
        source_refs=(bridge_claim_ref(span, inputs, check),),
        parse_manifest_id=None,
        reporting_period=observation.reporting_period,
        quantity_kind="absolute",
        binding_accepted=False,
    )


def _claim_side_holds(span: FrozenClaimSpan, inputs: ReviewedNumericInputs) -> list[str]:
    holds = []
    if span.parse_manifest_id != inputs.graph.parse_manifest_id:
        holds.append("claim_origin_parse_manifest_differs_from_reviewed_grid")
    if span.tenant_id != inputs.graph.tenant_id:
        holds.append("claim_origin_tenant_differs_from_reviewed_evaluation_tenant")
    if span.document_version_id != inputs.graph.document_version_id:
        holds.append("claim_origin_document_version_differs_from_reviewed_grid")
    holds.append("bridge_span_is_not_a_block_of_the_reviewed_snapshot")
    holds.append("claim_span_native_or_rendered_source_verification_not_run")
    holds.append("binding_acceptance_withheld_from_operator")
    return holds


def bridge_case(
    *,
    span: FrozenClaimSpan,
    inputs: ReviewedNumericInputs,
    check: SourceReverification,
    metric_raw: str,
    reporting_period: str,
    value_raw: str,
    unit_literal_in_claim: str,
) -> dict:
    """Run the real numeric service on the reviewed grid plus this one proposal."""
    from proofops.application.numeric_analysis import analyze_numeric_consistency

    record = matched_reviewed_candidate(
        inputs, metric_raw=metric_raw, reporting_period=reporting_period, value_raw=value_raw
    )
    binding = propose_comparison_binding(span=span, inputs=inputs, check=check, record=record)
    report = analyze_numeric_consistency(
        tenant_id=inputs.graph.tenant_id,
        original=inputs.graph,
        observations=inputs.observations,
        bindings=(binding,),
        claims=(),
    )
    holds = _claim_side_holds(span, inputs)
    holds.extend(check.holds)
    holds.extend(record.get("numeric_holds") or ())
    if record.get("unit_canonical") == record.get("unit_raw"):
        holds.append("reviewed_unit_literal_not_canonicalized")
    if _squeeze(unit_literal_in_claim) != _squeeze(str(record.get("unit_raw"))):
        holds.append(
            "unit_literal_differs_across_parses:"
            f"{unit_literal_in_claim!r}|{record.get('unit_raw')!r}"
        )
    case = {
        "schema": _BRIDGE_SCHEMA,
        "status": "candidate_pair_matched_not_admitted",
        "interpretation": (
            "a real discovered claim span and a reviewed-grid observation of the same "
            "number were matched and independently re-read from the source text; the "
            "numeric service was really called with the proposal and produced no finding. "
            "The re-read is candidate corroboration only: native/rendered source "
            "verification of the claim span is not_run, no binding is admitted, and this "
            "is neither a numeric finding, a grade nor an accuracy result"
        ),
        "not_run": [
            "native_or_rendered_source_verification_of_the_claim_span",
            "semantic_acceptance_of_the_binding",
            "admission_into_the_customer_run",
        ],
        "provenance": bridge_provenance(span, inputs),
        "source_reverification": {
            "method": "pdfplumber_text_and_word_geometry_reread",
            "evidence_strength": "candidate_corroboration_not_attestation",
            "page_num": check.page_num,
            "value_literal": check.value_literal,
            "value_occurrences": check.value_occurrences,
            "value_bbox": list(check.value_bbox) if check.value_bbox else None,
            "year_literal": check.year_literal,
            "year_bbox": list(check.year_bbox) if check.year_bbox else None,
            "row_literals": list(check.row_literals),
            "missing_row_literals": list(check.missing_row_literals),
            "column_aligned": check.column_aligned,
            "row_aligned": check.row_aligned,
            "corroborated": check.corroborated,
            "holds": list(check.holds),
        },
        "claim": {
            "quote": span.quote,
            "page_num": span.page_num,
            "block_kind": span.block_kind,
            "claim_form": "structured_disclosure_candidate",
            "origin": span.origin,
        },
        "reviewed_observation": {
            "observation_id": record.get("observation_id"),
            "metric_raw": record.get("metric_raw"),
            "reporting_period": record.get("reporting_period"),
            "unit_raw": record.get("unit_raw"),
            "unit_canonical": record.get("unit_canonical"),
            "value_raw": record.get("value_raw"),
            "quality": record.get("quality"),
            "numeric_usability": record.get("numeric_usability"),
            "numeric_holds": list(record.get("numeric_holds") or ()),
        },
        "binding_proposal": {
            "claim_id": binding.claim_id,
            "kind": binding.kind,
            "binding_accepted": binding.binding_accepted,
            "parse_manifest_id": binding.parse_manifest_id,
            "reported_value": binding.reported_value,
            "reporting_period": binding.reporting_period,
            "unit": binding.unit,
            "observation_ids": list(binding.observation_ids),
        },
        "numeric_service_call": {
            "invocation": "application.numeric_analysis.analyze_numeric_consistency",
            "claims_supplied": 0,
            "bindings_supplied": 1,
            "observations_supplied": len(inputs.observations),
            "outcome_count": len(report.outcomes),
            "has_findings": report.has_findings,
            "outcomes": [
                {"claim_id": item.claim_id, "status": item.status, "reason": item.reason}
                for item in report.outcomes
            ],
        },
        "holds": sorted(dict.fromkeys(holds)),
    }
    case["case_sha256"] = canonical_hash(case)
    return case


# ---------------------------------------------------------------------------
# Cross-location comparison: one combined snapshot, two natively proven grids.
#
# Everything above stays as it was: a frozen narrative span re-read as a
# *candidate*, which the pure check correctly refuses because the span is not a
# block of the reviewed snapshot. That refusal named two real blockers -- separate
# parses, and no native verification of the claim side -- and this section removes
# exactly those two without relabelling anything.
#
# The claim side stops being a text re-read and becomes a second reviewed grid of
# the *same original bytes*, carried in one explicitly new combined snapshot
# (``reviewed_table.graph_from_review(..., also=...)``) and put through the same
# unchanged native value verifier and context-literal verifier as the evidence
# side. Only then is a binding accepted, and every dimension of that binding is the
# *claim* location's own verified cell literal -- never copied from the evidence
# row. A literal that differs between the two locations therefore reaches the pure
# domain check as a difference and is refused there.
#
# External qualifiers remain review proposals: no native source/association proof
# exists for them here. Equal values or identical note text cannot clear a hold.
# ---------------------------------------------------------------------------

_CROSS_SCHEMA = "reviewed_cross_location_comparison_v1"
_CLAIM_ROLES = ("metric", "unit", "year", "value")


def _cross_uuid(name: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"proofops:{_CROSS_SCHEMA}:{name}"))


def _grid_index(review: dict) -> dict[tuple[str, str, str], frozenset[str]]:
    """(Literal row label, unit, year) -> the value literals reviewed.

    Read only out of the review artifact's own source-extracted cell text. This is
    corroboration between two reviewed readings, never an attestation.
    """
    index: dict[tuple[str, str, str], set[str]] = {}
    for candidate in review["candidates"]:
        key = (
            _normalized(candidate["metric_raw"]),
            _normalized(candidate.get("unit_raw") or ""),
            _normalized(candidate["year"]),
        )
        index.setdefault(key, set()).add(_normalized(candidate["value_raw"]))
    return {key: frozenset(values) for key, values in index.items()}


def shared_grid_identity(evidence_review: dict, claim_review: dict) -> dict:
    """Report literal overlap only; matching values cannot establish population."""
    evidence, claim = _grid_index(evidence_review), _grid_index(claim_review)
    shared = sorted(set(evidence) & set(claim))
    differing = [key for key in shared if evidence[key] != claim[key]]
    ambiguous = [key for key in shared if len(evidence[key]) != 1 or len(claim[key]) != 1]
    labels = ({key[0] for key in evidence}, {key[0] for key in claim})
    holds = []
    if differing:
        holds.append("shared_grid_values_differ")
    if ambiguous:
        holds.append("shared_grid_cell_not_unique")
    if labels[0] != labels[1]:
        holds.append("shared_grid_row_labels_differ")
    holds.append("population_not_established_by_equal_values")
    return {
        "method": "reviewed_source_text_of_both_grids",
        "evidence_strength": "candidate_corroboration_not_attestation",
        "shared_cells": len(shared),
        "equal_cells": len(shared) - len(differing) - len(ambiguous),
        "differing_keys": [list(key) for key in differing],
        "ambiguous_keys": [list(key) for key in ambiguous],
        "evidence_row_labels": sorted(labels[0]),
        "claim_row_labels": sorted(labels[1]),
        "satisfied": not holds,
        "holds": holds,
    }


def reconcile_reviewed_context(
    *,
    evidence_review: dict,
    claim_review: dict,
    bound_period: str,
    dispositions: Sequence[dict],
    identity: dict,
) -> tuple[tuple[dict, ...], tuple[str, ...]]:
    """Pair every reviewed out-of-grid passage with a re-checked caller disposition.

    Dispositions preserve reviewer proposals only. These external passages have
    no native source/association proof in this adapter, so even matching text or
    values cannot clear a hold. Cell verification does not verify outside notes.
    """
    sides = (
        ("evidence", evidence_review, claim_review),
        ("claim", claim_review, evidence_review),
    )
    records: list[dict] = []
    holds: list[str] = []
    used: set[int] = set()
    for location, review, _other in sides:
        for entry in _external_review_context(review):
            matches = [
                (index, item)
                for index, item in enumerate(dispositions)
                if item.get("location") == location
                and item.get("kind") == entry["kind"]
                and _normalized(str(item.get("raw_text", ""))) == _normalized(entry["raw_text"])
            ]
            if len(matches) != 1:
                holds.append(f"reviewed_context_disposition_missing:{location}:{entry['kind']}")
                records.append(
                    {
                        "location": location,
                        "kind": entry["kind"],
                        "raw_text": entry["raw_text"],
                        "state": "unreconciled",
                        "holds": [f"disposition_count:{len(matches)}"],
                    }
                )
                continue
            index, disposition = matches[0]
            used.add(index)
            entry_holds = ["reviewed_context_source_and_association_unverified"]
            records.append(
                {
                    "location": location,
                    "kind": entry["kind"],
                    "raw_text": entry["raw_text"],
                    "bbox": list(entry["bbox"]),
                    "word_indices": list(entry["word_indices"]),
                    "ground": disposition.get("ground"),
                    "reviewer": disposition.get("reviewer"),
                    "rationale": disposition.get("rationale"),
                    "review_kind": "ai_delegated_domain_review_not_independent_gold",
                    "state": "unreconciled" if entry_holds else "reconciled",
                    "holds": entry_holds,
                }
            )
            if entry_holds:
                holds.append(f"reviewed_context_unreconciled:{location}:{entry['kind']}")
    for index in range(len(dispositions)):
        if index not in used:
            holds.append(f"reviewed_context_disposition_unmatched:{index}")
    return tuple(records), tuple(dict.fromkeys(holds))


@dataclass(frozen=True, slots=True)
class CrossLocationSnapshot:
    """One combined snapshot whose two reviewed grids were natively re-attested.

    No verified status is inherited from an earlier run: the combined snapshot is
    its own parse manifest and every id in ``promoted_source_ids`` was produced by
    the unchanged value/context verifiers reading these bytes again.
    """

    graph: CanonicalDocumentGraph
    table_receipt: dict
    context_receipts: tuple
    cell_source_ids: dict
    promoted_source_ids: frozenset
    evidence_table_id: str
    claim_table_id: str
    evidence_review: dict
    claim_review: dict

    @property
    def tenant_id(self) -> str:
        return self.graph.tenant_id


def prepare_cross_location_snapshot(
    evidence_review: dict, claim_review: dict, source: bytes, *, tenant_id: str | None = None
) -> CrossLocationSnapshot:
    """Build the combined snapshot and run the existing native attestations on it."""
    from proofops.adapters.local.reviewed_table import _TENANT, native_attested_layout

    graph, receipt, cell_ids, evidence_table_id, promoted, context_receipts = (
        native_attested_layout(
            evidence_review,
            source,
            tenant_id=tenant_id or _TENANT,
            verify_context=True,
            also=(claim_review,),
        )
    )
    return CrossLocationSnapshot(
        graph=graph,
        table_receipt=receipt,
        context_receipts=context_receipts,
        cell_source_ids=cell_ids,
        promoted_source_ids=promoted,
        evidence_table_id=evidence_table_id,
        claim_table_id=cell_ids["1:reviewed-table"],
        evidence_review=evidence_review,
        claim_review=claim_review,
    )


def _block(snapshot: CrossLocationSnapshot, source_id: str):
    return next(b for b in snapshot.graph.blocks if b.source_id == source_id)


def _cell_ref(snapshot: CrossLocationSnapshot, source_id: str):
    """One whole-cell ref of the combined snapshot, checked by the real verifier."""
    from proofops.application.evidence.citations import verify_source_ref
    from proofops.application.ingest.geometry import canonicalize_source_ref

    block = _block(snapshot, source_id)
    candidate = block.candidates[block.winner]
    ref = canonicalize_source_ref(candidate.source, candidate.geometry, source_id=source_id)
    return verify_source_ref(ref, snapshot.graph, tenant_id=snapshot.tenant_id), block


def _role_source_ids(snapshot: CrossLocationSnapshot, cells: dict, *, prefix: str) -> dict:
    missing = [role for role in _CLAIM_ROLES if role not in cells]
    if missing:
        raise ValueError(f"explicit metric/unit/year/value cells required: {missing}")
    resolved = {}
    for role in _CLAIM_ROLES:
        key = f"{prefix}{cells[role]}"
        if key not in snapshot.cell_source_ids:
            raise ValueError(f"reviewed cell not in the combined snapshot: {key}")
        resolved[role] = snapshot.cell_source_ids[key]
    return resolved


@dataclass(frozen=True, slots=True)
class DisclosureRowClaim:
    """A claim that *is* a disclosure row, read out of that row's verified cells.

    Not a narrative sentence and never reported as one. ``quote`` is the ordered
    join of four whole-cell literals, each of which the verifier re-checked against
    the original bytes at its own pinned box, so every part of it is source-exact.
    ``source_quality`` is ``verified`` only when all four cells are natively
    promoted in this snapshot and all four refs verified.
    """

    claim_id: str
    tenant_id: str
    document_version_id: str
    parse_manifest_id: str
    source_sha256: str
    quote: str
    source_quality: str
    source_refs: tuple[SourceRef, ...]
    page_num: int
    role_source_ids: tuple[tuple[str, str], ...]
    claim_form: str = "source_verified_disclosure_row_reading"


def cross_location_case(
    snapshot: CrossLocationSnapshot,
    *,
    evidence_cells: dict,
    claim_cells: dict,
    dispositions: Sequence[dict],
    reviewer: str,
    label: str | None = None,
) -> dict:
    """Compare one reviewed claim row against one reviewed evidence cell, for real.

    The evidence cell becomes an observation through the existing normalizer; the
    claim row becomes a claim whose four cells are all natively promoted; the pure
    domain check is then called with a binding whose every dimension is the *claim*
    side's own literal. Nothing is decided here: an unverified cell, an unreconciled
    reviewed passage or a differing literal all reach the domain as what they are.
    """
    from proofops.application.evidence.citations import verify_source_ref
    from proofops.application.ingest.normalize import normalize_table_bindings
    from proofops.application.numeric_analysis import analyze_numeric_consistency

    graph = snapshot.graph
    evidence_ids = _role_source_ids(snapshot, evidence_cells, prefix="")
    claim_ids = _role_source_ids(snapshot, claim_cells, prefix="1:")
    normalized = normalize_table_bindings(
        graph,
        table_id=snapshot.evidence_table_id,
        bindings=(
            {
                "metric_raw": evidence_ids["metric"],
                "unit_raw": evidence_ids["unit"],
                "reporting_period": evidence_ids["year"],
                "value_raw": evidence_ids["value"],
            },
        ),
        tenant_id=snapshot.tenant_id,
    )
    observation = replace(
        normalized.observations[0],
        source_refs=tuple(
            verify_source_ref(ref, graph, tenant_id=snapshot.tenant_id)
            for ref in normalized.observations[0].source_refs
        ),
    )
    evidence_holds: list[str] = []
    for role, source_id in sorted(evidence_ids.items()):
        ref = next((r for r in observation.source_refs if r.source_id == source_id), None)
        if ref is None or ref.verification_state != "verified":
            evidence_holds.append(f"evidence_cell_unverified:{role}")
        elif source_id not in snapshot.promoted_source_ids:
            evidence_holds.append(f"evidence_cell_not_natively_promoted:{role}")
    if observation.value_state != "value":
        evidence_holds.append(f"evidence_value_state:{observation.value_state}")

    claim_refs: list[SourceRef] = []
    claim_literals: dict[str, str] = {}
    claim_holds: list[str] = []
    claim_pages: set[int] = set()
    for role in _CLAIM_ROLES:
        ref, block = _cell_ref(snapshot, claim_ids[role])
        claim_refs.append(ref)
        claim_literals[role] = block.raw_text
        claim_pages.add(block.page_num)
        if ref.verification_state != "verified":
            claim_holds.append(f"claim_cell_unverified:{role}")
        elif claim_ids[role] not in snapshot.promoted_source_ids:
            claim_holds.append(f"claim_cell_not_natively_promoted:{role}")
    evidence_pages = {_block(snapshot, source_id).page_num for source_id in evidence_ids.values()}
    if len(claim_pages) != 1:
        claim_holds.append("claim_row_spans_several_pages")
    if evidence_pages & claim_pages:
        # A value compared against itself is not a cross-location comparison.
        claim_holds.append("claim_and_evidence_are_the_same_physical_page")

    identity = shared_grid_identity(snapshot.evidence_review, snapshot.claim_review)
    context_records, context_holds = reconcile_reviewed_context(
        evidence_review=snapshot.evidence_review,
        claim_review=snapshot.claim_review,
        bound_period=claim_literals["year"],
        dispositions=dispositions,
        identity=identity,
    )
    observation_holds = sorted(dict.fromkeys([*evidence_holds, *context_holds]))
    if not observation_holds:
        observation = replace(observation, quality="verified")

    claim = DisclosureRowClaim(
        claim_id=_cross_uuid(
            "claim:"
            + ":".join((graph.parse_manifest_id, *(claim_ids[role] for role in _CLAIM_ROLES)))
        ),
        tenant_id=snapshot.tenant_id,
        document_version_id=graph.document_version_id,
        parse_manifest_id=graph.parse_manifest_id,
        source_sha256=graph.source_sha256,
        quote=" ".join(ref.quote for ref in claim_refs),
        source_quality="unverified" if claim_holds else "verified",
        source_refs=tuple(claim_refs),
        page_num=sorted(claim_pages)[0],
        role_source_ids=tuple((role, claim_ids[role]) for role in _CLAIM_ROLES),
    )
    binding = ClaimBinding(
        claim_id=claim.claim_id,
        tenant_id=snapshot.tenant_id,
        document_version_id=graph.document_version_id,
        kind="comparison",
        observation_ids=(observation.observation_id,),
        # Every dimension below is the claim location's own verified literal. None
        # is read off the evidence row, so a difference stays a difference.
        reported_value=claim_literals["value"],
        metric_raw=claim_literals["metric"],
        scope=None,
        subject=None,
        scope2_basis=None,
        organizational_boundary=None,
        unit=claim_literals["unit"],
        denominator=None,
        source_refs=tuple(claim_refs),
        parse_manifest_id=graph.parse_manifest_id,
        reporting_period=claim_literals["year"],
        quantity_kind="absolute",
        reported_value_ref=claim_refs[_CLAIM_ROLES.index("value")],
        binding_accepted=not claim_holds and not observation_holds,
    )
    report = analyze_numeric_consistency(
        tenant_id=snapshot.tenant_id,
        original=graph,
        observations=(observation,),
        bindings=(binding,),
        claims=(claim,),
    )
    outcome = report.outcomes[0]
    result = outcome.result
    case = {
        "schema": _CROSS_SCHEMA,
        "label": label,
        "reviewer": reviewer,
        "review_kind": "ai_delegated_layout_and_context_review_not_independent_gold",
        "snapshot": {
            "tenant_id": snapshot.tenant_id,
            "document_version_id": graph.document_version_id,
            "parse_manifest_id": graph.parse_manifest_id,
            "source_sha256": graph.source_sha256,
            "combined_of": [
                {
                    "role": "evidence",
                    "physical_page": snapshot.evidence_review["physical_page"],
                    "layout_sha256": snapshot.evidence_review["layout_sha256"],
                    "table_id": snapshot.evidence_table_id,
                },
                {
                    "role": "claim",
                    "physical_page": snapshot.claim_review["physical_page"],
                    "layout_sha256": snapshot.claim_review["layout_sha256"],
                    "table_id": snapshot.claim_table_id,
                },
            ],
            "native_value_receipt_sha256": snapshot.table_receipt["artifact_sha256"],
            "native_value_policy_sha256": snapshot.table_receipt["policy_sha256"],
            "context_receipt_sha256": [
                proof["artifact_sha256"] for proof in snapshot.context_receipts
            ],
            "natively_promoted_cells": len(snapshot.promoted_source_ids),
        },
        "claim": {
            "claim_id": claim.claim_id,
            "claim_form": claim.claim_form,
            "page_num": claim.page_num,
            "quote": claim.quote,
            "source_quality": claim.source_quality,
            "cell_literals": dict(sorted(claim_literals.items())),
            "role_source_ids": dict(claim.role_source_ids),
            "holds": sorted(dict.fromkeys(claim_holds)),
        },
        "evidence_observation": {
            "observation_id": observation.observation_id,
            "page_num": sorted(evidence_pages)[0],
            "metric_raw": observation.metric_raw,
            "unit_raw": observation.unit_raw,
            "unit_canonical": observation.unit_canonical,
            "scale_multiplier": observation.scale_multiplier,
            "reporting_period": observation.reporting_period,
            "value_raw": observation.value_raw,
            "value_decimal": observation.value_decimal,
            "quality": observation.quality,
            "role_source_ids": dict(sorted(evidence_ids.items())),
            "holds": observation_holds,
        },
        "shared_grid_identity": identity,
        "reviewed_context_reconciliation": list(context_records),
        "binding": {
            "kind": binding.kind,
            "binding_accepted": binding.binding_accepted,
            "reported_value": binding.reported_value,
            "metric_raw": binding.metric_raw,
            "unit": binding.unit,
            "reporting_period": binding.reporting_period,
            "quantity_kind": binding.quantity_kind,
            "dimensions_taken_from": "claim_location_cells_only",
        },
        "numeric_service_call": {
            "invocation": "application.numeric_analysis.analyze_numeric_consistency",
            "claims_supplied": 1,
            "bindings_supplied": 1,
            "observations_supplied": 1,
            "has_findings": report.has_findings,
            "outcome": {
                "claim_id": outcome.claim_id,
                "status": outcome.status,
                "reason": outcome.reason,
                "result_status": result.status if result else None,
                "result_reason": result.reason if result else None,
                "reported_value": result.reported_value if result else None,
                "computed_value": result.computed_value if result else None,
                "source_ref_count": len(result.source_refs) if result else 0,
            },
        },
        "not_run": [
            "rulepack_grade_or_label_for_this_comparison",
            "independent_human_or_expert_confirmation",
            "admission_into_a_customer_run",
        ],
    }
    case["case_sha256"] = canonical_hash(case)
    return case
