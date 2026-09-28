"""R24: rendered-side-only finite typography equivalence for claim spans.

Wraps -- never modifies -- ``claim_span_bullet_alignment`` and adds ONE
comparison. For a record the wrapped layers already READ successfully but left
unresolved, the quote is additionally accepted only when both hold:

* the UNCHANGED native gate passes on UNFOLDED strings, i.e. exactly
  ``claim_source_verification._unique_quote(native, quote)`` with neither side
  folded; and
* the same two-sided uniqueness check passes on the RENDERED side after a
  finite fold applied to the rendered text and the quote only.

The fold is the shipped ``native_paragraph_typography._quote_folded`` helper
(curly quotes -> ASCII, reused as code) plus exactly one entry,
``U+00B7 -> U+2022``: five entries, pinned literally in ``_ADMITTED_ENTRIES``
and checked at import. Nothing else is folded -- no digit, space, case, dash,
subscript, prime, degree sign, separator, NFKC, edit distance, fuzzy or regex.

The native side is never folded, and the quote is never folded for the native
gate. That unfolded gate is what carries the safety; the evidence for this
shape on original bytes (two reports, both rendered paths, real OCR refusals,
the boundary case, and 6 synthetic probes the rendered gate alone accepts and
this shape refuses) is ``tests/fixtures/pipeline/typography-original-proof/``
and ``.../typography-wrapper/``, not restated here.

No new geometry, box, crop, OCR call or reader: it reads only the rendered
reads the wrapped layers already produced. It does not relax the bullet
wrapper's token-boundary refusal -- such a box has no rendered read here and
stays unresolved. Refs travel through byte-for-byte and no block ``quality``
is ever flipped wholesale. Source completeness is not semantic accuracy: a
promoted record grants no tag, grade, label, track or safe-harbour category.

Distinct schema and policy. The policy pins this wrapper's bytes, the wrapped
bullet-alignment policy (which pins the R15 and base policies), the reused fold
helper module's bytes and the fold map digest, so a stored receipt can never be
replayed under a different baseline, fold table or helper code. Opting in is
only possible when a NEW run pins this policy; rolling back is not pinning it.
"""

from dataclasses import asdict, replace
from hashlib import sha256
from pathlib import Path

from proofops.adapters.local import claim_source_verification as base
from proofops.adapters.local import claim_span_bullet_alignment as baseline
from proofops.adapters.local import native_paragraph_typography as fold_helper
from proofops.adapters.local.native_paragraph_typography import _QUOTE_FOLD, _quote_folded
from proofops.application.evidence import span_citations
from proofops.application.evidence.citations import _normalized
from proofops.domain.provenance import canonical_hash
from proofops.domain.values import SourceRef

SCHEMA = "claim_span_typography_attestation_v1"
POLICY_SCHEMA = "claim_span_typography_policy_v1"
MODE = "claim_span_rendered_only_quote_and_middle_dot_typography_v1"
RECOVERY_REASON = "rendered_only_finite_typography_fold_v1"

MIDDLE_DOT = "\u00b7"
BULLET = "\u2022"

_DOT_FOLD = str.maketrans({MIDDLE_DOT: BULLET})
_RENDERED_FOLD = {**_QUOTE_FOLD, **_DOT_FOLD}
# The whole admissible table, enumerated literally. Not a class, range, regex
# or punctuation predicate: a future helper character is a code change here.
_ADMITTED_ENTRIES = (
    ("U+00B7", "U+2022"),
    ("U+2018", "U+0027"),
    ("U+2019", "U+0027"),
    ("U+201C", "U+0022"),
    ("U+201D", "U+0022"),
)

# Reasons meaning "read successfully, only the compared string differs". Every
# other reason (clipped, geometry, interactive, glyph, unreadable,
# source_invalid, rendered_reader_unavailable) is never considered.
_ELIGIBLE_REASONS = frozenset({"text_mismatch", "rendered_quote_unresolved"})

_BULLET_ALIGNMENT_PATH = "bullet_alignment_rendered_read"
_BASE_PATH = "base_rendered_read"
_NO_READ_PATH = "no_rendered_read"


def _entries(table) -> tuple[tuple[str, str], ...]:
    return tuple(
        sorted((f"U+{code:04X}", f"U+{ord(target):04X}") for code, target in table.items())
    )


if _entries(_RENDERED_FOLD) != _ADMITTED_ENTRIES:
    raise AssertionError("the rendered fold table is not the five admitted entries")


def _rendered_folded(text: str) -> str:
    """The shipped helper's fold (which applies ``_normalized`` first), then the
    single added dot entry."""
    return _quote_folded(text).translate(_DOT_FOLD)


def _applied_fold_entries(*texts: str) -> list[str]:
    """Which admitted entries actually occur in ``texts`` -- the recovery
    reason's detail, so an auditor sees the substitution a promotion used."""
    return [
        f"{source}->{target}"
        for code, (source, target) in zip(sorted(_RENDERED_FOLD), _ADMITTED_ENTRIES, strict=True)
        if any(chr(code) in _normalized(text) for text in texts)
    ]


def claim_span_typography_policy() -> dict:
    baseline_policy = baseline.claim_source_policy()
    return dict(
        schema=POLICY_SCHEMA,
        mode=MODE,
        base=baseline_policy,
        base_sha256=canonical_hash(baseline_policy),
        wrapper_sha256=sha256(Path(__file__).read_bytes()).hexdigest(),
        fold_helper_sha256=sha256(Path(fold_helper.__file__).read_bytes()).hexdigest(),
        rendered_fold_sha256=canonical_hash(sorted(_RENDERED_FOLD.items())),
        rendered_fold_entries=[f"{s}->{t}" for s, t in _ADMITTED_ENTRIES],
    )


def _eligible_records(receipt: dict) -> list[int]:
    """Indices of records the wrapped layers left unresolved WITH a read.

    Record *indices*, not source_ids: two records can share a source_id with
    different quotes/offsets and are gated independently. Reaching one of the
    two eligible reasons is itself the proof every other guard already passed.
    """
    records, readings = receipt.get("records"), receipt.get("readings")
    if not isinstance(records, list) or not isinstance(readings, dict):
        return []
    if not isinstance(receipt.get("bullet_alignments"), dict):
        return []
    return [
        index
        for index, record in enumerate(records)
        if record.get("status") == "unresolved"
        and record.get("reason") in _ELIGIBLE_REASONS
        and isinstance(readings.get(record["ref"]["source_id"]), dict)
    ]


def typography_read(receipt: dict, graph, source_id: str) -> tuple[str | None, dict | None, str]:
    """The native string and rendered read the WRAPPED layers themselves
    compared on, plus which path produced it.

    Exactly the two paths demonstrated on original bytes, in this order: the
    bullet wrapper's own rendered read for a box it accepted, otherwise the base
    verifier's own rendered read. The R15 retry read is deliberately NOT a third
    path -- it was not demonstrated -- so a record whose only successful read is
    a retry stays unresolved.
    """
    alignment = (receipt.get("bullet_alignments") or {}).get(source_id)
    if isinstance(alignment, dict) and alignment.get("status") == "aligned":
        return alignment["native_normalized"], alignment.get("rendered"), _BULLET_ALIGNMENT_PATH
    reading = (receipt.get("readings") or {}).get(source_id) or {}
    block = {b.source_id: b for b in graph.blocks}.get(source_id)
    if (
        reading.get("reason") == "rendered_quote_unresolved"
        and isinstance(reading.get("rendered"), dict)
        and block is not None
    ):
        return _normalized(block.raw_text), reading["rendered"], _BASE_PATH
    return None, None, _NO_READ_PATH


def _typography_gate(native: str, rendered_text: str, quote: str) -> dict:
    """The two gates and their precise inputs. Never a verdict on its own."""
    folded_rendered, folded_quote = _rendered_folded(rendered_text), _rendered_folded(quote)
    native_gate = base._unique_quote(native, quote)  # UNFOLDED, both sides
    rendered_gate = base._unique_quote(folded_rendered, folded_quote)
    return dict(
        native_normalized=native,
        native_gate_unfolded=native_gate,
        native_side_folded=False,
        rendered_normalized=_normalized(rendered_text),
        rendered_folded=folded_rendered,
        quote_normalized=quote,
        quote_folded=folded_quote,
        rendered_gate_folded=rendered_gate,
        applied_fold_entries=_applied_fold_entries(rendered_text, quote),
        accepted=bool(native_gate and rendered_gate),
    )


def apply_claim_span_typography(graph, source, refs, *, tenant_id):
    """Recompute the wrapped receipt against real bytes first (never trust a
    caller-injected receipt), then accept only the rendered-typography subset.

    Returns ``(result, proof)``. ``result`` is a distinct receipt under this
    module's own ``SCHEMA``/policy, never the baseline relabelled: all inner
    layers travel through so an auditor sees each of them.
    """
    receipt = baseline.attest_claim_spans(graph, source, refs, tenant_id=tenant_id)
    reads: dict[str, dict] = {}
    promoted: list[int] = []
    for index in _eligible_records(receipt):
        record = receipt["records"][index]
        native, rendered, path = typography_read(receipt, graph, record["ref"]["source_id"])
        entry = dict(
            source_id=record["ref"]["source_id"],
            baseline_reason=record["reason"],
            path=path,
            rendered_read_status=(rendered or {}).get("status"),
            # Pins the ACTUAL read (text, crop, scale, image sha) into this
            # receipt: two receipts with the same statuses but different
            # underlying reads must never hash identically.
            rendered_read_sha256=canonical_hash(rendered) if rendered is not None else None,
        )
        if native is None or not isinstance(rendered, dict) or rendered.get("status") != "read":
            reads[str(index)] = dict(entry, accepted=False, reason="no_rendered_read")
            continue
        gate = _typography_gate(
            native, rendered.get("text", ""), _normalized(record["ref"]["quote"])
        )
        if gate["accepted"]:
            reason = RECOVERY_REASON
            promoted.append(index)
        elif not gate["native_gate_unfolded"]:
            reason = "native_gate_unfolded_refused"
        else:
            reason = "rendered_fold_still_differs"
        reads[str(index)] = dict(entry, **gate, reason=reason)

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
        policy=claim_span_typography_policy(),
        # The wrapped layer plus every layer it itself carries, untouched.
        bullet_alignment_attestation_sha256=receipt["artifact_sha256"],
        bullet_alignment_records=receipt["records"],
        baseline_attestation_sha256=receipt["baseline_attestation_sha256"],
        baseline_records=receipt["baseline_records"],
        base_attestation_sha256=receipt["base_attestation_sha256"],
        base_records=receipt["base_records"],
        records=records,
        readings=receipt["readings"],
        render_retries=receipt["render_retries"],
        bullet_alignments=receipt["bullet_alignments"],
        typography_reads=reads,
    )
    result["artifact_sha256"] = canonical_hash(result)

    proof = dict(
        schema="claim_span_typography_proof_v1",
        tenant_id=tenant_id,
        document_version_id=graph.document_version_id,
        parse_manifest_id=graph.parse_manifest_id,
        source_sha256=graph.source_sha256,
        bullet_alignment_attestation_sha256=receipt["artifact_sha256"],
        result_attestation_sha256=result["artifact_sha256"],
        policy=claim_span_typography_policy(),
        eligible_record_count=len(reads),
        typography_reads=reads,
        promoted_record_count=len(promoted),
        promoted_source_ids=sorted({records[i]["ref"]["source_id"] for i in promoted}),
    )
    proof["policy_sha256"] = canonical_hash(proof["policy"])
    proof["artifact_sha256"] = canonical_hash(proof)
    return result, proof


def replay_claim_span_typography(result, graph, source, *, tenant_id):
    """Recompute a caller-supplied receipt IN FULL against real bytes under this
    module's own exact policy, then scope exactly the promoted per-record spans.

    Mirrors ``claim_span_bullet_alignment.replay_bullet_alignment``: the whole
    receipt is rebuilt from its own ``base_records`` refs and compared
    canonically, so a tampered receipt, a tampered inner layer, a stale graph or
    changed source bytes raises instead of scoping anything. Existing
    ``verified_spans`` are preserved by union.
    """
    if not isinstance(result, dict) or result.get("schema") != SCHEMA:
        raise ValueError("CLAIM_SPAN_TYPOGRAPHY_RECEIPT_REQUIRED")
    if result.get("policy") != claim_span_typography_policy():
        raise ValueError("CLAIM_SPAN_TYPOGRAPHY_POLICY_MISMATCH")
    records = result.get("base_records")
    if not isinstance(records, list):
        raise ValueError("CLAIM_SPAN_TYPOGRAPHY_RECEIPT_REQUIRED")
    refs = tuple(SourceRef(**record["ref"]) for record in records)
    expected, expected_proof = apply_claim_span_typography(graph, source, refs, tenant_id=tenant_id)
    if canonical_hash(result) != canonical_hash(expected):
        raise ValueError("CLAIM_SPAN_TYPOGRAPHY_RECEIPT_MISMATCH")
    newly = tuple(
        replace(SourceRef(**record["ref"]), verification_state="verified")
        for record in result["records"]
        if record["status"] == "verified"
    )
    existing = tuple(getattr(graph, "verified_spans", ()) or ())
    seen = {(span.source_id, span.char_start, span.char_end) for span in existing}
    merged = existing + tuple(
        span for span in newly if (span.source_id, span.char_start, span.char_end) not in seen
    )
    scoped = span_citations.span_verified_graph(graph, merged, result["artifact_sha256"])
    return scoped, expected_proof


def apply_to_discovery(result, discovery, graph, source, *, tenant_id):
    """Promote a claim only when EVERY one of its own refs verifies against the
    resulting scoped graph; a claim with one still-unresolved ref is unchanged.
    """
    scoped, proof = replay_claim_span_typography(result, graph, source, tenant_id=tenant_id)
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
    return claim_span_typography_policy()


def attest_claim_spans(graph, source, refs, *, tenant_id):
    result, _ = apply_claim_span_typography(graph, source, refs, tenant_id=tenant_id)
    return result


def replay_claim_spans(receipt, graph, source, discovery, *, tenant_id):
    """Reader-protocol replay returning ``(discovery, scoped_graph)``."""
    if not isinstance(receipt, dict) or receipt.get("schema") != SCHEMA:
        raise ValueError("CLAIM_SPAN_TYPOGRAPHY_RECEIPT_REQUIRED")
    records = receipt.get("base_records")
    if not isinstance(records, list) or canonical_hash(
        [record.get("ref") for record in records]
    ) != canonical_hash([asdict(ref) for ref in base.discovery_refs(discovery)]):
        raise ValueError("CLAIM_SPAN_TYPOGRAPHY_RECEIPT_MISMATCH")
    replayed, scoped, _ = apply_to_discovery(receipt, discovery, graph, source, tenant_id=tenant_id)
    return replayed, scoped
