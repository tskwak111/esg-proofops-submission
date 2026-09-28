"""Optional, additive render-resolution retry for claim-span rendered OCR.

Real observation (KB run 9e96e8d2-8054-47cd-968b-b5f1038a9e42, source
33896b5d-71ad-5b97-b065-9770181ac94d, physical page 30, quote "9.9억 원"):
the two native PDF word tokens ("9.9억" and "원") are both individually
glyph-ink-resolved and jointly equal the block's own raw text natively -- every
geometry, interactivity, glyph-mapping and clipping guard in
``claim_source_verification._read_paragraph`` already passes -- but the
rendered OCR comparison still leaves the record ``rendered_quote_unresolved``
because Apple Vision's reading of the *whole-block* crop
(``source_verification._rendered_text``, fixed 216dpi / 3x scale, rasterizes
the whole page once and crops pixels) drops the single inter-word space and
returns "9.9억원".

Root cause is crop rendering, not text policy, and it is NOT isolated to
scale alone. ``selected_cell_table_verification._rendered_cell`` already
exists for exactly this class of short-crop problem, and it differs from the
base paragraph reader in three ways at once: it crops the PDF page *before*
rasterizing (``page.crop(box).to_image(...)``, vector-crop-then-render,
instead of the base reader's render-whole-page-then-crop-pixels), it derives
a deterministic scale from the box's own height (``scale = max(3, ceil(96 /
box_height))``, bounded by ``_MAX_CROP_PIXELS``), and it always adds a fixed
24px blank border (``ImageOps.expand(image, border=24, fill="white")``). This
module reuses that reader completely unchanged and does not claim which of
those three factors is responsible: on the real KB run, one promoted source
("9.9억 원", box height ~8.9pt) gets a scale of 11, and the other promoted
source (a full paragraph, box height ~55.8pt) gets a scale of 3 -- the same
minimum scale the base reader effectively already uses -- so for that second
case the crop-before-render and/or fixed border, not a higher scale, is the
more likely explanation. No further experiment isolates this here; it is
recorded as an open, unattributed factor, not a confirmed single cause.

Measured over every real KB p30 claim source in this run, this reader
confirms two sources and leaves every other already-unresolved source
unresolved (their real mismatch is an unrelated, already-known, unapproved
"·"/"•" OCR glyph variant -- see ``evidence/probe-ocr-punctuation.py`` -- not
a regression this module introduces). A generic increased-padding sweep on
the *base* reader was tried and rejected instead (see
``scratch/r13_diag_kb_p30_9_9eok.py``): it corrupts other real sources at some
padding values (e.g. "판매관리비" -> "판매관ㅂ" at padding_px=30) -- exactly
the manufactured-match risk this module must not introduce.

Design (mirrors ``native_paragraph_typography.py``):
- The base receipt, base policy, and ``attest_claim_spans`` semantics are
  completely unchanged; this module never mutates or reinterprets them in
  place, and never returns them relabelled as its own. It calls the existing
  (unmodified) ``attest_claim_spans`` first to get the baseline receipt under
  its own ``claim_source_attestation_v1`` schema, then independently produces
  a *separate* receipt object under this module's own
  ``claim_span_render_resolution_attestation_v1`` schema and policy. A caller
  that reads ``result["schema"]`` can never mistake this for the base
  verifier's receipt.
- Gating is per RECORD, not per source_id. A source_id can appear more than
  once in ``refs`` with different quotes/offsets (e.g. one valid ref and one
  altered/invalid ref pointing at the same block); only a record that (a) the
  base verifier itself left ``status=="unresolved"``,
  ``reason=="rendered_quote_unresolved"`` and (b) whose *own* ``ref.quote`` is
  a unique substring of both the native block text and the higher-resolution
  rendered text (the exact same ``_unique_quote`` two-sided check
  ``attest_claim_spans`` already applies) is promoted. An invalid/altered ref
  sharing a source_id with a promotable one is never promoted by that
  association; each record is independently re-checked against its own quote.
- The retry crop is exactly the same pinned bbox the base reader already
  verified natively (``candidate.bbox``) -- never a smaller ink-derived box
  and never an enlarged one -- so no neighbouring ink can be smuggled in.
  Only the existing cell reader's own crop/scale/border pipeline is invoked;
  no padding search, no per-case tuning, no new box.
- Exact-match discipline is unchanged: uniqueness and equality use the exact
  same ``_unique_quote``/``_normalized`` primitives ``attest_claim_spans``
  already uses -- no new fold, no whitespace stripping, no fuzzy comparison.
  A close-but-not-exact retry stays unresolved with its own recorded reason;
  it is never treated as evidence of anything.
- Never touches ``source_verification.py``, ``native_glyph_geometry.py``,
  ``native_ocr.swift``, ``citations.py``, ``claim_source_verification.py``, or
  any frozen verifier bytes. This module is versioned and hashed
  independently; an old run's stored receipt, checkpoint schema and verdict
  stay byte-identical unless a caller explicitly opts into this comparison on
  a new run, and the proof records exactly which records were retried, with
  which crop, and why.

Wired (R15): a run may opt into this comparison by pinning
``claim_source_policy()`` below in its immutable run snapshot. The reader-protocol
shims at the end of this module (``claim_source_policy`` / ``attest_claim_spans``
/ ``replay_claim_spans``) are the only new surface that wiring needed; the
dispatch itself lives in ``claim_source_policies``. A run that did not opt in is
completely unaffected, and no stored receipt, checkpoint schema or verdict is
reinterpreted -- rolling back means not pinning this policy on future runs.
"""

from dataclasses import asdict, replace
from hashlib import sha256
from pathlib import Path

from proofops.adapters.local import claim_source_verification as base
from proofops.adapters.local import selected_cell_table_verification as cell_reader
from proofops.application.evidence import span_citations
from proofops.application.evidence.citations import _normalized
from proofops.domain.provenance import canonical_hash
from proofops.domain.values import SourceRef

SCHEMA = "claim_span_render_resolution_attestation_v1"


def claim_span_render_resolution_policy() -> dict:
    """Wrapper policy: pins the base claim-source policy plus this module's own
    hash and the existing deterministic-scale reader's hash, so a stored proof
    can never be replayed under a base verifier or reader it was not produced
    with."""
    base_policy = base.claim_source_policy()
    return dict(
        schema="claim_span_render_resolution_policy_v1",
        mode="claim_span_deterministic_crop_scale_v1",
        base=base_policy,
        base_sha256=canonical_hash(base_policy),
        wrapper_sha256=sha256(Path(__file__).read_bytes()).hexdigest(),
        # The existing, unmodified deterministic-scale reader this module reuses.
        cell_reader_sha256=sha256(Path(cell_reader.__file__).read_bytes()).hexdigest(),
    )


def _eligible_records(claim_attestation: dict) -> list[int]:
    """Indices into ``claim_attestation["records"]`` for records the base
    verifier already read but left unresolved only on the rendered-OCR
    string, with every other guard already passed.

    Never touches unreadable/clipped/text_mismatch/interactive/geometry
    records: only ``rendered_quote_unresolved`` records are even considered.
    Returns record *indices*, not source_ids, because two records can share a
    source_id with different quotes/offsets and must be gated independently.
    """
    readings = claim_attestation.get("readings")
    records = claim_attestation.get("records")
    if not isinstance(readings, dict) or not isinstance(records, list):
        return []
    return [
        index
        for index, record in enumerate(records)
        if record.get("status") == "unresolved"
        and record.get("reason") == "rendered_quote_unresolved"
        and isinstance(readings.get(record["ref"]["source_id"]), dict)
    ]


def eligible_render_resolution_sources(claim_attestation: dict) -> frozenset[str]:
    """Source ids with at least one record eligible for a retry (see
    ``_eligible_records``). Kept for callers that only need the source-level
    survey; ``apply_render_resolution`` itself gates per record, not per
    source_id, so a mixed valid/invalid-ref source is handled correctly even
    though both its records are "eligible" to be checked here.
    """
    records = claim_attestation.get("records") or []
    return frozenset(
        records[index]["ref"]["source_id"] for index in _eligible_records(claim_attestation)
    )


def apply_render_resolution(graph, source, refs, *, tenant_id):
    """Recompute the base claim-span attestation against real bytes first
    (never trust a caller-injected receipt), then additionally confirm only
    the narrow render-resolution-eligible subset described in the module
    docstring using the existing deterministic-scale cell reader.

    Returns ``(result, proof)``. ``result`` is a *distinct* receipt under this
    module's own ``SCHEMA``/policy, never the base receipt relabelled: its
    ``records`` are the base attestation's records with only per-record
    promotions applied (see ``_eligible_records``), and its ``base_records``
    field carries the untouched base attestation for audit. The base
    attestation returned by plain ``attest_claim_spans`` is never mutated or
    represented as anything other than itself.
    """
    import io

    import pdfplumber

    receipt = base.attest_claim_spans(graph, source, refs, tenant_id=tenant_id)
    eligible_indices = _eligible_records(receipt)
    blocks = {b.source_id: b for b in graph.blocks}
    # One retry render per distinct source_id (the crop is identical for every
    # record of that source_id); the per-record exact-quote gate below is
    # still evaluated independently for every eligible record.
    rendered_by_source: dict[str, dict] = {}
    retries: dict[str, dict] = {}
    if eligible_indices:
        needed_source_ids = sorted(
            {receipt["records"][index]["ref"]["source_id"] for index in eligible_indices}
        )
        with pdfplumber.open(io.BytesIO(source)) as document:
            for source_id in needed_source_ids:
                block = blocks.get(source_id)
                if block is None or block.winner is None:
                    continue
                candidate = block.candidates[block.winner]
                box = candidate.bbox
                if box is None or not 1 <= block.page_num <= len(document.pages):
                    continue
                page = document.pages[block.page_num - 1]
                rendered = cell_reader._rendered_cell(page, box)
                rendered_by_source[source_id] = rendered
                native = _normalized(block.raw_text)
                rendered_text = (
                    _normalized(rendered.get("text", ""))
                    if rendered.get("status") == "read"
                    else ""
                )
                retries[source_id] = dict(
                    rendered=rendered,
                    render_crop_box=[float(v) for v in box],
                    native_normalized=native,
                    rendered_normalized=rendered_text,
                )

    promoted_indices: list[int] = []
    for index in eligible_indices:
        record = receipt["records"][index]
        source_id = record["ref"]["source_id"]
        rendered = rendered_by_source.get(source_id)
        block = blocks.get(source_id)
        if rendered is None or block is None or rendered.get("status") != "read":
            continue
        native = _normalized(block.raw_text)
        rendered_text = _normalized(rendered.get("text", ""))
        quote = _normalized(record["ref"]["quote"])
        # Same two-sided uniqueness check attest_claim_spans applies: the
        # record's OWN quote, not just source_id membership, must uniquely
        # occur in both texts. An invalid/altered ref sharing a source_id
        # with a promotable one is independently re-checked here and refused
        # if its own quote does not itself satisfy this.
        if base._unique_quote(native, quote) and base._unique_quote(rendered_text, quote):
            promoted_indices.append(index)

    promoted_indices_set = set(promoted_indices)
    result_records = [
        dict(record, status="verified", reason=None) if index in promoted_indices_set else record
        for index, record in enumerate(receipt["records"])
    ]
    result = dict(
        schema=SCHEMA,
        tenant_id=tenant_id,
        document_version_id=graph.document_version_id,
        parse_manifest_id=graph.parse_manifest_id,
        source_sha256=graph.source_sha256,
        graph_sha256=canonical_hash(asdict(graph)),
        policy=claim_span_render_resolution_policy(),
        base_attestation_sha256=receipt["artifact_sha256"],
        base_records=receipt["records"],
        records=result_records,
        readings=receipt["readings"],
        # The actual confirming retry evidence (rendered image sha/text/scale
        # per source_id) is pinned INTO this receipt, not only into the
        # separate proof: two admitted receipts with the same final statuses
        # but different underlying retry readings must not hash identically.
        render_retries=retries,
    )
    result["artifact_sha256"] = canonical_hash(result)

    promoted_source_ids = sorted({result_records[i]["ref"]["source_id"] for i in promoted_indices})
    proof = dict(
        schema="claim_span_render_resolution_proof_v1",
        tenant_id=tenant_id,
        document_version_id=graph.document_version_id,
        parse_manifest_id=graph.parse_manifest_id,
        source_sha256=graph.source_sha256,
        base_attestation_sha256=receipt["artifact_sha256"],
        result_attestation_sha256=result["artifact_sha256"],
        policy=claim_span_render_resolution_policy(),
        eligible_record_count=len(eligible_indices),
        eligible_source_ids=sorted(
            {receipt["records"][i]["ref"]["source_id"] for i in eligible_indices}
        ),
        retries=retries,
        promoted_record_count=len(promoted_indices),
        promoted_source_ids=promoted_source_ids,
    )
    proof["policy_sha256"] = canonical_hash(proof["policy"])
    proof["artifact_sha256"] = canonical_hash(proof)
    return result, proof


def replay_render_resolution(result, graph, source, *, tenant_id):
    """Validate a caller-supplied wrapper receipt by recomputing it in full
    against real bytes (never trust a stored/injected receipt), then build a
    ``SpanVerifiedGraph`` scoped to exactly the promoted per-record spans --
    never a whole-block quality promotion.

    Mirrors ``claim_source_verification.replay_claim_spans``: this module's
    own receipt is recomputed from ``result["base_records"]``'s own refs (the
    same refs the caller originally passed to ``apply_render_resolution``)
    and compared byte-for-byte; a mismatch (tampered receipt, stale graph,
    changed source bytes) raises instead of scoping anything. The returned
    graph's ``verify_source_ref`` (via ``span_citations``) only reports
    "verified" for a ref whose own char_start/char_end range is a SUBSET of
    an actually-promoted record's range on the same source_id -- a ref that
    only partially overlaps a promoted span, or names a different
    source_id, is not silently upgraded. No block's ``quality`` is ever
    flipped wholesale.

    If ``graph`` is already a ``span_citations.SpanVerifiedGraph`` (e.g. an
    earlier, separate receipt already scoped some other spans verified),
    those existing ``verified_spans`` are preserved by union, never dropped:
    ``span_citations.span_verified_graph`` replaces ``verified_spans``
    wholesale, so calling it with only the newly-promoted refs would
    silently lose any spans a prior caller had already established. This
    function's own newly-promoted refs are additive, and an exact duplicate
    (same source_id/char_start/char_end) already present in the incoming
    graph's ``verified_spans`` is kept once, not doubled.
    """
    if not isinstance(result, dict) or result.get("schema") != SCHEMA:
        raise ValueError("CLAIM_SPAN_RENDER_RESOLUTION_RECEIPT_REQUIRED")
    refs = tuple(SourceRef(**r["ref"]) for r in result["base_records"])
    expected, expected_proof = apply_render_resolution(graph, source, refs, tenant_id=tenant_id)
    if canonical_hash(result) != canonical_hash(expected):
        raise ValueError("CLAIM_SPAN_RENDER_RESOLUTION_RECEIPT_MISMATCH")
    newly_promoted = tuple(
        replace(SourceRef(**record["ref"]), verification_state="verified")
        for record in result["records"]
        if record["status"] == "verified"
    )
    existing = tuple(getattr(graph, "verified_spans", ()) or ())
    seen = {(s.source_id, s.char_start, s.char_end) for s in existing}
    merged = existing + tuple(
        s for s in newly_promoted if (s.source_id, s.char_start, s.char_end) not in seen
    )
    scoped = span_citations.span_verified_graph(graph, merged, result["artifact_sha256"])
    return scoped, expected_proof


def apply_to_discovery(result, discovery, graph, source, *, tenant_id):
    """Apply a validated wrapper receipt to a ``ClaimDiscovery``, exactly the
    way ``claim_source_verification.replay_claim_spans`` applies its own base
    receipt: a claim is only promoted to ``source_quality="verified"`` if
    EVERY one of its own ``source_refs`` independently verifies against the
    resulting scoped graph. A claim with even one still-unresolved ref (e.g.
    a multi-source claim where only one axis was render-resolution-eligible)
    is returned unchanged.
    """
    scoped, proof = replay_render_resolution(result, graph, source, tenant_id=tenant_id)
    claims = []
    for claim in discovery.claims:
        if claim.source_quality != "unverified":
            claims.append(claim)
            continue
        checked = tuple(
            span_citations.verify_source_ref(ref, scoped, tenant_id=tenant_id)
            for ref in claim.source_refs
        )
        claims.append(
            replace(claim, source_quality="verified", source_refs=checked)
            if checked and all(ref.verification_state == "verified" for ref in checked)
            else claim
        )
    return replace(discovery, claims=tuple(claims)), scoped, proof


# --- Reader-protocol shims (R15 wiring) -------------------------------------
# The run snapshot, worker publication and claim-store replay already dispatch
# through a module exposing exactly these three names (see
# ``claim_source_verification.claim_source_reader``). Implementing them here is
# what lets a run pin THIS comparison instead of the base verifier's, without
# any call site special-casing the wrapper. The semantics above are unchanged:
# these are thin adapters, not a second implementation.


def claim_source_policy():
    """Reader-protocol alias for ``claim_span_render_resolution_policy``.

    Same dict, so a snapshot pins this module's own hashes plus the base policy
    it wraps; the distinct ``schema`` keeps the two policies unambiguous.
    """
    return claim_span_render_resolution_policy()


def attest_claim_spans(graph, source, refs, *, tenant_id):
    """Reader-protocol publication entry point: the wrapper receipt only.

    The separate proof is recomputed deterministically by
    ``replay_render_resolution`` from the receipt and the real bytes, so it is
    not duplicated into the published checkpoint.
    """
    result, _ = apply_render_resolution(graph, source, refs, tenant_id=tenant_id)
    return result


def replay_claim_spans(receipt, graph, source, discovery, *, tenant_id):
    """Reader-protocol replay returning ``(discovery, scoped_graph)``, matching
    ``claim_source_verification.replay_claim_spans``.

    The receipt's ``base_records`` refs must be exactly the refs this discovery
    would attest now, in the same order, compared canonically so a JSON
    round-tripped stored receipt is not rejected for list/tuple shape alone.
    ``apply_to_discovery`` already refuses a
    receipt that does not recompute byte-for-byte against the real bytes; this
    additional gate refuses a receipt that recomputes consistently but attests a
    DIFFERENT ref set than the discovery being loaded, instead of applying it
    partially. A malformed receipt raises rather than scoping anything.
    """
    if not isinstance(receipt, dict) or receipt.get("schema") != SCHEMA:
        raise ValueError("CLAIM_SPAN_RENDER_RESOLUTION_RECEIPT_REQUIRED")
    records = receipt.get("base_records")
    if not isinstance(records, list) or canonical_hash(
        [r.get("ref") for r in records]
    ) != canonical_hash([asdict(ref) for ref in base.discovery_refs(discovery)]):
        raise ValueError("CLAIM_SPAN_RENDER_RESOLUTION_RECEIPT_MISMATCH")
    replayed, scoped, _ = apply_to_discovery(
        receipt, discovery, graph, source, tenant_id=tenant_id
    )
    return replayed, scoped
