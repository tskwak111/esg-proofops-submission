"""Optional, additive quote-typography tolerance for native paragraph replay.

Real observation (kia-assurance run d10b45cb-6192-4914-ad3c-5dd140dab414,
source a6eaff68-bbc0-5e6e-8404-7d43cc2115be, physical page 130): the native
PDF text and Apple Vision's rendered OCR reading match exactly on every word
and digit; the sole difference is that Vision renders curly double/single
quotation marks (as they truly appear in the source) as straight ASCII quotes.
`attest_native_sources` correctly leaves such a record `rendered_text_unresolved`
because its raw comparison (`citations._normalized`) never folds quotation
marks — by design, since folding globally would risk masking real content
errors. This module adds a narrowly scoped, explicitly opt-in comparison that
tolerates *only* that one substitution, and only for records the base verifier
already read successfully; it never re-runs OCR and never touches
`source_verification.py`, `native_glyph_geometry.py`, `native_ocr.swift`,
`citations.py`, or any frozen verifier bytes.

Design:
- The base receipt, base policy, and base `replay_native_sources` semantics
  are completely unchanged; this module never mutates or reinterprets them
  in place. It calls the existing (unmodified) `replay_native_sources` via
  the existing process-local cache to get the baseline verified graph, and
  independently recomputes a *second* graph with a strictly larger verified
  set (base ∪ quote-tolerant promotions).
- A record is only eligible for promotion if the base verifier's own read
  already produced a `status == "unresolved"`, `reason ==
  "rendered_text_unresolved"` record with a successfully read `rendered.text`
  (i.e. every other guard — geometry, interactivity, glyph mapping, clipping,
  exact word/digit match against the raw text — already passed). This module
  adds no new geometry, OCR, or word-matching logic of its own.
  Unreadable/clipped/`text_mismatch`/unresolved-native records are never
  touched; nothing here can promote a record the base verifier rejected for
  any other reason, and no record can regress from `verified` to anything
  else.
- The tolerance is one explicit, finite `str.maketrans` map (curly double
  quotes U+201C/U+201D -> U+0022, curly single quotes/apostrophe U+2018/
  U+2019 -> U+0027), applied identically to both sides before the existing
  `_normalized` comparison. No NFKC, no punctuation stripping, no digit or
  decimal-separator rewriting, and primes/double-primes (U+2032/U+2033) and
  the degree sign are deliberately excluded from the map so unit/measurement
  text is never folded.
- This module is versioned and hashed independently of the base policy
  (`native_paragraph_policy_sha256` in the checkpoint is untouched). Nothing
  here is written back into the parse-stage checkpoint; callers that opt in
  receive an additional, separately hashed proof artifact describing exactly
  which records were promoted and why, so an old run's stored receipt,
  checkpoint schema, and verdict stay byte-identical unless a caller
  explicitly requests this comparison on a new run.
"""

from dataclasses import asdict, replace
from hashlib import sha256
from pathlib import Path

from proofops.adapters.local.native_replay_cache import replay_cached
from proofops.adapters.local.run_artifacts import native_paragraph_policy
from proofops.application.evidence.citations import _normalized
from proofops.domain.provenance import canonical_hash

# Explicit, finite quotation-mark fold. Anything not listed here (primes,
# double primes, degree sign, guillemets, digits, decimal points, percent
# signs, etc.) is left exactly as `_normalized` already treats it.
_QUOTE_FOLD = str.maketrans(
    {
        "\u201c": '"',  # LEFT DOUBLE QUOTATION MARK
        "\u201d": '"',  # RIGHT DOUBLE QUOTATION MARK
        "\u2018": "'",  # LEFT SINGLE QUOTATION MARK
        "\u2019": "'",  # RIGHT SINGLE QUOTATION MARK
    }
)


def _quote_folded(text: str) -> str:
    return _normalized(text).translate(_QUOTE_FOLD)


def native_paragraph_typography_policy() -> dict:
    """Wrapper policy: pins the base verifier policy plus this module's own
    hash and an explicit finite map digest, so a stored proof can never be
    replayed under a base verifier or fold table it was not produced with."""
    base = native_paragraph_policy()
    return dict(
        schema="native_paragraph_typography_policy_v1",
        mode="paragraph_native_quote_typography_v1",
        base=base,
        base_sha256=canonical_hash(base),
        wrapper_sha256=sha256(Path(__file__).read_bytes()).hexdigest(),
        quote_fold_sha256=canonical_hash(sorted(_QUOTE_FOLD.items())),
    )


def is_typography_policy(policy: object) -> bool:
    """True only for this module's own wrapper policy shape (never the base
    verifier's plain dict), so a generic dispatcher can route to it without
    guessing at unrelated policy shapes."""
    return (
        isinstance(policy, dict)
        and policy.get("schema") == "native_paragraph_typography_policy_v1"
        and isinstance(policy.get("base"), dict)
    )


def replay_native_with_typography_policy(stored_policy, receipt, graph, source, *, tenant_id):
    """Drop-in replacement for `replay_native_with_policy` that also accepts
    this module's wrapper policy shape.

    For any policy that is not this module's shape, behavior is byte-identical
    to calling `frozen_native_replay.replay_native_with_policy` directly (old
    runs, old defaults, and the live/pinned-historical base verifiers are
    completely untouched). This module's wrapper policy is only ever bound
    for a *new* run against the then-current live base verifier -- never a
    pinned historical one, since a new run's own live policy is always the
    live verifier -- so this rejects a wrapper policy whose pinned `base`
    does not equal the actual current live policy, rather than silently
    replaying it under the wrong (live) verifier.
    """
    from proofops.adapters.local.frozen_native_replay import replay_native_with_policy

    if not is_typography_policy(stored_policy):
        return replay_native_with_policy(stored_policy, receipt, graph, source, tenant_id=tenant_id)
    if stored_policy != native_paragraph_typography_policy():
        raise ValueError("native typography policy does not match the live base verifier")
    result, _proof = apply_typography_tolerance(receipt, graph, source, tenant_id=tenant_id)
    return result


def eligible_typography_sources(native: dict) -> dict[str, str]:
    """Records the base verifier already read but rejected only on quote
    typography. Maps source_id -> the record's already-stored rendered text.

    Never touches unreadable/clipped/text_mismatch/unresolved-native records:
    only `rendered_text_unresolved` records with a successfully read
    `rendered.text` are even considered, and the actual fold-tolerant
    equality check happens in `apply_typography_tolerance` against the real
    raw words, not here.
    """
    return {
        row["source_id"]: row["rendered"]["text"]
        for row in native.get("records", ())
        if row.get("status") == "unresolved"
        and row.get("reason") == "rendered_text_unresolved"
        and isinstance(row.get("rendered"), dict)
        and row["rendered"].get("status") == "read"
        and isinstance(row["rendered"].get("text"), str)
        and row["rendered"]["text"].strip()
    }


def apply_typography_tolerance(native, graph, source, *, tenant_id):
    """Recompute the base verification against real bytes first (never trust
    a caller-injected receipt), then additionally promote only the narrow
    quote-typography subset described in the module docstring.

    `graph` must be the attested-input (pre-replay) graph the receipt was
    produced from: replay receipts pin `input_graph_sha256`, so an already
    base-replayed graph never reproduces its own input receipt. (Both the
    producer and the checkpoint reader thread the pre-native graph here.)

    Returns (result_graph, proof). `result_graph.blocks` quality is a
    superset of the base replay: every block the base verifier already
    verified stays verified, and only rendered_text_unresolved records whose
    fold-tolerant text equals the raw words are added. Raises ValueError
    (matching `replay_native_sources`) if the receipt does not reproduce
    against the actual source bytes and graph.
    """
    if not isinstance(native, dict) or native.get("schema") != "native_paragraph_attestation_v2":
        raise ValueError("NATIVE_PARAGRAPH_ATTESTATION_REQUIRED")
    baseline = replay_cached(native, graph, source, tenant_id=tenant_id)
    already_verified = {b.source_id for b in baseline.blocks if b.quality == "verified"}
    eligible = eligible_typography_sources(native)
    raw_by_id = {block.source_id: block.raw_text for block in baseline.blocks}
    promoted = {
        source_id
        for source_id, rendered_text in eligible.items()
        if source_id not in already_verified
        and source_id in raw_by_id
        and _quote_folded(rendered_text) == _quote_folded(raw_by_id[source_id])
    }
    result = replace(
        baseline,
        blocks=tuple(
            replace(block, quality="verified") if block.source_id in promoted else block
            for block in baseline.blocks
        ),
    )
    proof = dict(
        schema="native_paragraph_typography_proof_v1",
        tenant_id=tenant_id,
        document_version_id=graph.document_version_id,
        parse_manifest_id=graph.parse_manifest_id,
        source_sha256=graph.source_sha256,
        input_graph_sha256=canonical_hash(asdict(baseline)),
        output_graph_sha256=canonical_hash(asdict(result)),
        policy=native_paragraph_typography_policy(),
        native_attestation_sha256=canonical_hash(native),
        base_verified_source_ids=sorted(already_verified),
        promoted_source_ids=sorted(promoted),
    )
    proof["policy_sha256"] = canonical_hash(proof["policy"])
    proof["artifact_sha256"] = canonical_hash(proof)
    return result, proof
