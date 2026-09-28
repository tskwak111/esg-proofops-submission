"""R19: the bullet-line word-split that blocks claim-span source verification.

What is actually wrong
----------------------
``claim_source_verification._read_paragraph`` builds the native reading of a
pinned paragraph box as ``" ".join(word["text"] for word in selected)``, so
EVERY pdfplumber word-token boundary becomes one space in the compared string.
On a bullet line whose first content glyph pdfplumber emits as its own token,
that join inserts a space the document's ink does not contain::

    parser raw_text   '• 시설 영향: 직접적 물리적 리스크가 없는 것으로 평가'
    native join       '• 시 설 영향: 직접적 물리적 리스크가 없는 것으로 평가'
                          ^ inserted by the join, not present as ink

``_normalized`` collapses runs of whitespace but never removes a single space,
so the comparison at ``claim_source_verification.py``'s ``text_mismatch`` check
fails and returns EARLY -- before ``_rendered_text`` is ever called. That is why
every such record stores a reading with only ``{"status", "reason"}`` and why
``render_retries`` stays empty: the independent rendered/OCR reader, which reads
these very paragraphs correctly, never ran.

What this wrapper does
----------------------
For the narrow ``text_mismatch`` subset ONLY, it re-reads the SAME pinned box
with the SAME frozen readers, deletes at most ONE join-inserted space at the
ONE observed boundary, and then runs the EXISTING unchanged gate. Measured on
the real NAVER physical page 90 run (``d5f7908d``), 7 of 8 blocked refs pass;
the 8th stays unresolved because the OCR reader renders the document's curly
quotes as ASCII apostrophes, which this module deliberately does NOT fold.

The admitted boundary is pinned to the observed shape, not searched for:

* the box's first token is literally ``•`` (U+2022, observed in the NAVER
  report) or ``·`` (U+00B7, observed in the KB report) -- those two and no
  others;
* the second token is exactly ONE alphabetic character (never a digit);
* the third token starts with an alphabetic character, or with ``-`` (U+002D)
  immediately followed by an alphabetic character (the observed ``K|-water의``);
* the boundary between the second and third token lies on one ink line and its
  real ink gap is the UNIQUE strict minimum of every same-ink-line boundary in
  that box -- a tie refuses.

Only that boundary is ever considered. No other word boundary, and no boundary
touching a digit, is eligible, so a numeric token can never be joined to its
neighbour. Then the aligned join must equal the parser's own ``raw_text`` under
the UNCHANGED ``_normalized``: a second difference anywhere refuses. This is
strictly narrower than whitespace stripping, which would also accept a genuine
space being added or removed.

What it refuses to do
---------------------
* No threshold constant, no fuzzy match, no edit distance, no number
  correction, no punctuation/quote-character/case folding, no global
  whitespace normalization.
* It never widens the pinned box, the 0.001pt containment quantization, or the
  clipped/rotated refusal, and never reads a glyph outside the box.
* It re-derives the selection itself (the base's early return discards it) and
  SELF-CHECKS that its own unaligned join reproduces the base's
  ``text_mismatch`` verdict exactly. Any disagreement with the base refuses
  rather than proceeding under a divergent reading.
* The ref's ``source_id``, ``quote``, ``char_start`` and ``char_end`` are
  preserved byte-for-byte. No source, quote, coordinate or quality is
  fabricated, and the exact alignment transform is stored in the receipt.
* Source completeness is not semantic accuracy: a promoted record grants no
  tag, grade, label, track or safe-harbour category.

Compatibility
-------------
The baseline is the existing R15 wrapper, reused as a reader rather than
reimplemented, so selecting this policy PRESERVES render resolution instead of
replacing it. Its ``base_records``, ``readings`` and ``render_retries`` travel
through unchanged, and this module adds its own distinct schema and policy. No
stored receipt is reinterpreted: rolling back means not pinning this policy on
future runs.
"""

from dataclasses import asdict, replace
from hashlib import sha256
from pathlib import Path

from proofops.adapters.local import claim_source_verification as base
from proofops.adapters.local import claim_span_render_resolution as baseline
from proofops.adapters.local import native_glyph_geometry, source_verification
from proofops.adapters.local.native_glyph_geometry import native_word_ink_geometry
from proofops.adapters.local.source_verification import _rendered_text
from proofops.application.evidence import span_citations
from proofops.application.evidence.citations import _normalized
from proofops.domain.provenance import canonical_hash
from proofops.domain.values import SourceRef

SCHEMA = "claim_span_bullet_alignment_attestation_v1"
MODE = "bullet_first_token_single_inserted_space_v1"

# Pinned to the two bullet characters actually observed: NAVER's U+2022 and
# KB's U+00B7. Any other bullet glyph, or any other leading punctuation, is
# refused rather than generalised.
_BULLETS = ("\u2022", "\u00b7")
_JOINABLE_LEAD_PUNCTUATION = "\u002d"
# The boundary is fixed at the second/third token, never searched for.
_BOUNDARY_INDEX = 1


def claim_span_bullet_alignment_policy() -> dict:
    """Pins the R15 baseline policy plus this module's own hash and the two
    frozen readers it reuses, so a stored receipt can never be replayed under a
    baseline, glyph reader or rendered reader it was not produced with."""
    baseline_policy = baseline.claim_source_policy()
    return dict(
        schema="claim_span_bullet_alignment_policy_v1",
        mode=MODE,
        base=baseline_policy,
        base_sha256=canonical_hash(baseline_policy),
        wrapper_sha256=sha256(Path(__file__).read_bytes()).hexdigest(),
        glyph_reader_sha256=sha256(Path(native_glyph_geometry.__file__).read_bytes()).hexdigest(),
        rendered_reader_sha256=sha256(Path(source_verification.__file__).read_bytes()).hexdigest(),
    )


def _eligible_records(attestation: dict) -> list[int]:
    """Indices of records the baseline left unresolved ONLY on ``text_mismatch``.

    Record *indices*, not source_ids: two records can share a source_id with
    different quotes/offsets and must be gated independently. Every other
    reason (clipped, geometry, interactive, glyph, rendered) is untouched, and
    reaching ``text_mismatch`` is itself the proof that the base's interactive,
    appearance, geometry and glyph-coverage guards all already passed.
    """
    readings, records = attestation.get("readings"), attestation.get("records")
    if not isinstance(readings, dict) or not isinstance(records, list):
        return []
    return [
        index
        for index, record in enumerate(records)
        if record.get("status") == "unresolved"
        and record.get("reason") == "text_mismatch"
        and isinstance(readings.get(record["ref"]["source_id"]), dict)
    ]


def eligible_bullet_alignment_sources(attestation: dict) -> frozenset[str]:
    """Source ids with at least one ``text_mismatch`` record (survey only)."""
    records = attestation.get("records") or []
    return frozenset(records[index]["ref"]["source_id"] for index in _eligible_records(attestation))


def _pinned_tokens(page, source, block):
    """The base verifier's own selection for this block's pinned box.

    Same readers, same containment test and same 0.001pt quantization as
    ``claim_source_verification._read_paragraph``; nothing is widened. Returns
    ``None`` when the glyph reader cannot cover the page, when any uncovered
    word overlaps the box, or when a token crosses the box -- the same
    conditions under which the base refuses.
    """
    candidate = block.candidates[block.winner]
    box = candidate.bbox
    if box is None:
        return None
    words = page.extract_words()
    try:
        proof = native_word_ink_geometry(source, block.page_num, list(range(len(words))))
    except ValueError:
        return None
    if not isinstance(proof.get("matched_words"), list):
        return None
    boxes = {w["native_word_index"]: w["ink_bbox"] for w in proof["matched_words"]}
    missing = set(proof.get("unresolved_word_indices", ()))
    if set(boxes) | missing != set(range(len(words))):
        return None
    if any(
        words[i]["x1"] > box[0]
        and words[i]["x0"] < box[2]
        and words[i]["bottom"] > box[1]
        and words[i]["top"] < box[3]
        for i in missing
    ):
        return None
    selected, crossing = [], []
    for index, word in enumerate(words):
        if index not in boxes:
            continue
        wb = boxes[index]
        if wb[2] <= box[0] or wb[0] >= box[2] or wb[3] <= box[1] or wb[1] >= box[3]:
            continue
        entry = dict(index=index, text=word["text"], bbox=wb)
        if word["upright"] and (
            box[0] - 0.001 <= wb[0] < wb[2] <= box[2] + 0.001
            and box[1] - 0.001 <= wb[1] < wb[3] <= box[3] + 0.001
        ):
            selected.append(entry)
        else:
            crossing.append(entry)
    if crossing:
        return None  # the base already refuses or tolerates this; never re-decide it
    return selected


def _join(selected, *, drop=None) -> str:
    """The base's own join, optionally omitting ONE inserted space."""
    parts: list[str] = []
    for position, word in enumerate(selected):
        parts.append(word["text"])
        if position < len(selected) - 1:
            parts.append("" if position == drop else " ")
    return "".join(parts)


def _joinable_second_token(text: str) -> bool:
    """Exactly one alphabetic character. Never a digit, never punctuation."""
    return len(text) == 1 and text.isalpha()


def _joinable_third_token(text: str) -> bool:
    """Starts with a letter, or with the observed hyphen followed by a letter."""
    if not text:
        return False
    if text[0].isalpha():
        return True
    return text[0] == _JOINABLE_LEAD_PUNCTUATION and text[1:2].isalpha()


def bullet_alignment(selected) -> dict | None:
    """The single admissible inserted-space deletion, or ``None`` to refuse.

    The boundary is the second/third token of a box that starts with one of the
    two observed literal bullets; it is pinned, not searched. It must
    additionally be on one ink line and carry the UNIQUE strict minimum real ink
    gap of every same-ink-line boundary in the box, which is what distinguishes
    a join artefact from a genuine space typeset by the document (measured on
    the real NAVER page: artefact gaps 0.008-1.240pt against a minimum genuine
    gap of 1.695pt, with the bullet's own boundary a genuine space at
    1.95-2.69pt).
    """
    if not isinstance(selected, list) or len(selected) < 3:
        return None
    if selected[0]["text"] not in _BULLETS:
        return None
    if not _joinable_second_token(selected[_BOUNDARY_INDEX]["text"]):
        return None
    if not _joinable_third_token(selected[_BOUNDARY_INDEX + 1]["text"]):
        return None
    gaps = []
    for position, (left, right) in enumerate(zip(selected, selected[1:], strict=False)):
        if left["bbox"][1] < right["bbox"][3] and left["bbox"][3] > right["bbox"][1]:
            gaps.append((round(right["bbox"][0] - left["bbox"][2], 6), position))
    if len(gaps) < 2:
        return None  # no same-line baseline to compare the candidate against
    gaps.sort()
    smallest, runner_up = gaps[0], gaps[1]
    if smallest[0] >= runner_up[0] or smallest[1] != _BOUNDARY_INDEX:
        return None  # a tie, or the tightest boundary is not the bullet boundary
    return dict(
        mode=MODE,
        boundary_token_index=_BOUNDARY_INDEX,
        left_token=selected[_BOUNDARY_INDEX]["text"],
        right_token=selected[_BOUNDARY_INDEX + 1]["text"],
        ink_gap_pt=smallest[0],
        next_smallest_ink_gap_pt=runner_up[0],
        deleted_inserted_spaces=1,
    )


def _align_source(page, source, block):
    """Alignment evidence for one block, or a refusal reason. Never a verdict."""
    selected = _pinned_tokens(page, source, block)
    if selected is None:
        return dict(status="refused", reason="selection_unavailable")
    unaligned = _join(selected)
    parser_text = _normalized(block.raw_text)
    if not unaligned or _normalized(unaligned) == parser_text:
        # Self-check: the base's own verdict for this record was text_mismatch,
        # so a re-derivation that does NOT reproduce that mismatch disagrees
        # with the base and is refused instead of acted on.
        return dict(status="refused", reason="base_verdict_not_reproduced")
    transform = bullet_alignment(selected)
    if transform is None:
        return dict(status="refused", reason="boundary_not_admissible")
    aligned = _join(selected, drop=transform["boundary_token_index"])
    if _normalized(aligned) != parser_text:
        return dict(status="refused", reason="aligned_text_still_differs")
    rendered = _rendered_text(page, block.candidates[block.winner].bbox)
    return dict(
        status="aligned",
        reason=None,
        alignment=transform,
        tokens=[w["text"] for w in selected],
        unaligned_join=unaligned,
        aligned_join=aligned,
        rendered=rendered,
        render_crop_box=[float(v) for v in block.candidates[block.winner].bbox],
        native_normalized=parser_text,
        rendered_normalized=(
            _normalized(rendered.get("text", "")) if rendered.get("status") == "read" else ""
        ),
    )


def apply_bullet_alignment(graph, source, refs, *, tenant_id):
    """Recompute the R15 baseline against real bytes, then additionally confirm
    only the narrow bullet ``text_mismatch`` subset.

    Returns ``(result, proof)``. ``result`` is a distinct receipt under this
    module's own ``SCHEMA``/policy, never the baseline relabelled: its
    ``records`` are the baseline's records with per-record promotions applied,
    and the baseline's own ``base_records``, ``readings`` and ``render_retries``
    travel through untouched so an auditor sees both layers.
    """
    import io

    import pdfplumber

    receipt = baseline.attest_claim_spans(graph, source, refs, tenant_id=tenant_id)
    eligible_indices = _eligible_records(receipt)
    blocks = {b.source_id: b for b in graph.blocks}
    alignments: dict[str, dict] = {}
    if eligible_indices:
        needed = sorted({receipt["records"][i]["ref"]["source_id"] for i in eligible_indices})
        with pdfplumber.open(io.BytesIO(source)) as document:
            for source_id in needed:
                block = blocks.get(source_id)
                if block is None or block.winner is None:
                    continue
                if not 1 <= block.page_num <= len(document.pages):
                    continue
                alignments[source_id] = _align_source(
                    document.pages[block.page_num - 1], source, block
                )

    promoted: list[int] = []
    for index in eligible_indices:
        record = receipt["records"][index]
        aligned = alignments.get(record["ref"]["source_id"])
        block = blocks.get(record["ref"]["source_id"])
        if block is None or not isinstance(aligned, dict) or aligned["status"] != "aligned":
            continue
        if aligned["rendered"].get("status") != "read":
            continue
        quote = _normalized(record["ref"]["quote"])
        # The base verifier's own two-sided uniqueness gate, unchanged: the
        # record's OWN quote must occur exactly once in the parser paragraph
        # AND exactly once in the independent rendered read of the same box.
        if base._unique_quote(aligned["native_normalized"], quote) and base._unique_quote(
            aligned["rendered_normalized"], quote
        ):
            promoted.append(index)

    promoted_set = set(promoted)
    records = [
        dict(record, status="verified", reason=None) if index in promoted_set else record
        for index, record in enumerate(receipt["records"])
    ]
    result = dict(
        schema=SCHEMA,
        tenant_id=tenant_id,
        document_version_id=graph.document_version_id,
        parse_manifest_id=graph.parse_manifest_id,
        source_sha256=graph.source_sha256,
        graph_sha256=canonical_hash(asdict(graph)),
        policy=claim_span_bullet_alignment_policy(),
        baseline_attestation_sha256=receipt["artifact_sha256"],
        baseline_records=receipt["records"],
        base_attestation_sha256=receipt["base_attestation_sha256"],
        base_records=receipt["base_records"],
        records=records,
        readings=receipt["readings"],
        render_retries=receipt["render_retries"],
        # The confirming evidence is pinned INTO the receipt: two receipts with
        # the same statuses but different underlying alignments or rendered
        # images must never hash identically.
        bullet_alignments=alignments,
    )
    result["artifact_sha256"] = canonical_hash(result)

    proof = dict(
        schema="claim_span_bullet_alignment_proof_v1",
        tenant_id=tenant_id,
        document_version_id=graph.document_version_id,
        parse_manifest_id=graph.parse_manifest_id,
        source_sha256=graph.source_sha256,
        baseline_attestation_sha256=receipt["artifact_sha256"],
        result_attestation_sha256=result["artifact_sha256"],
        policy=claim_span_bullet_alignment_policy(),
        eligible_record_count=len(eligible_indices),
        eligible_source_ids=sorted(
            {receipt["records"][i]["ref"]["source_id"] for i in eligible_indices}
        ),
        alignments=alignments,
        promoted_record_count=len(promoted),
        promoted_source_ids=sorted({records[i]["ref"]["source_id"] for i in promoted}),
    )
    proof["policy_sha256"] = canonical_hash(proof["policy"])
    proof["artifact_sha256"] = canonical_hash(proof)
    return result, proof


def replay_bullet_alignment(result, graph, source, *, tenant_id):
    """Recompute a caller-supplied receipt in full against real bytes, then
    scope exactly the promoted per-record spans verified.

    Mirrors ``claim_span_render_resolution.replay_render_resolution``: the
    receipt is rebuilt from its own ``base_records`` refs and compared
    canonically, so a tampered receipt, stale graph or changed source bytes
    raises instead of scoping anything. Existing ``verified_spans`` on an
    already-scoped graph are preserved by union, never dropped, and no block's
    ``quality`` is ever flipped wholesale.
    """
    if not isinstance(result, dict) or result.get("schema") != SCHEMA:
        raise ValueError("CLAIM_SPAN_BULLET_ALIGNMENT_RECEIPT_REQUIRED")
    refs = tuple(SourceRef(**r["ref"]) for r in result["base_records"])
    expected, expected_proof = apply_bullet_alignment(graph, source, refs, tenant_id=tenant_id)
    if canonical_hash(result) != canonical_hash(expected):
        raise ValueError("CLAIM_SPAN_BULLET_ALIGNMENT_RECEIPT_MISMATCH")
    newly = tuple(
        replace(SourceRef(**record["ref"]), verification_state="verified")
        for record in result["records"]
        if record["status"] == "verified"
    )
    existing = tuple(getattr(graph, "verified_spans", ()) or ())
    seen = {(s.source_id, s.char_start, s.char_end) for s in existing}
    merged = existing + tuple(
        s for s in newly if (s.source_id, s.char_start, s.char_end) not in seen
    )
    scoped = span_citations.span_verified_graph(graph, merged, result["artifact_sha256"])
    return scoped, expected_proof


def apply_to_discovery(result, discovery, graph, source, *, tenant_id):
    """Promote a claim only when EVERY one of its own refs verifies against the
    resulting scoped graph; a claim with one still-unresolved ref is unchanged.
    """
    scoped, proof = replay_bullet_alignment(result, graph, source, tenant_id=tenant_id)
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


# --- Reader-protocol shims --------------------------------------------------
# Thin adapters, not a second implementation: the run snapshot, worker
# publication and claim-store replay already dispatch through a module exposing
# exactly these three names.


def claim_source_policy():
    """Reader-protocol alias for ``claim_span_bullet_alignment_policy``."""
    return claim_span_bullet_alignment_policy()


def attest_claim_spans(graph, source, refs, *, tenant_id):
    """Reader-protocol publication entry point: the wrapper receipt only."""
    result, _ = apply_bullet_alignment(graph, source, refs, tenant_id=tenant_id)
    return result


def replay_claim_spans(receipt, graph, source, discovery, *, tenant_id):
    """Reader-protocol replay returning ``(discovery, scoped_graph)``."""
    if not isinstance(receipt, dict) or receipt.get("schema") != SCHEMA:
        raise ValueError("CLAIM_SPAN_BULLET_ALIGNMENT_RECEIPT_REQUIRED")
    records = receipt.get("base_records")
    if not isinstance(records, list) or canonical_hash(
        [r.get("ref") for r in records]
    ) != canonical_hash([asdict(ref) for ref in base.discovery_refs(discovery)]):
        raise ValueError("CLAIM_SPAN_BULLET_ALIGNMENT_RECEIPT_MISMATCH")
    replayed, scoped, _ = apply_to_discovery(
        receipt, discovery, graph, source, tenant_id=tenant_id
    )
    return replayed, scoped
