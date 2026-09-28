"""Versioned native-attestation replay against pinned historical verifier source.

Covers the frozen local-KB blocker where a stored checkpoint carries a native
paragraph policy produced by an older verifier. Replay must reproduce that
receipt with the pinned historical source, and must reject any policy whose
verifier bytes are unknown or tampered.
"""

from copy import deepcopy
from dataclasses import replace
from hashlib import sha256

import pytest
from proofops.adapters.local import frozen_native_replay as fr
from proofops.adapters.local import source_verification
from proofops.adapters.local.run_artifacts import (
    accepted_native_policy_digests,
    native_paragraph_policy,
)
from proofops.application.ingest.graph_fusion import fuse_candidates
from proofops.domain.provenance import canonical_hash

from tests.acceptance.test_parsing import TENANT, candidate, pdf

PINNED_HASH = "2f68a213aa7c32ce25110bb57cb9dc5b03036d808956ffb417a243b85c07556e"

# Registered when this worktree's native_ocr.swift carried an uncommitted,
# intentional local edit (usesCPUOnly) that diverged the live
# rendered_reader_sha256 from the HEAD-committed value. Checkpoints stamped
# against the HEAD-committed verifier (same source_verification.py /
# native_glyph_geometry.py as live, HEAD native_ocr.swift) must still replay.
CPU_SWIFT_PINNED_HASH = "cf201df19cfa9770a7f66ad67d1906388de7417863d7421ae84093938df0f8f6"


def test_pinned_verifier_reproduces_stored_policy_hash():
    """The vendored historical source must reproduce the exact stored policy hash."""
    bundle = fr.find_pinned_verifier(PINNED_HASH)
    assert bundle is not None
    assert bundle["policy_hash"] == PINNED_HASH
    assert canonical_hash(fr.frozen_policy(bundle)) == PINNED_HASH
    # The pinned digest differs from the live verifier: this is a real historical
    # policy, not a re-hash of current code.
    assert PINNED_HASH != canonical_hash(native_paragraph_policy())


def test_cpu_swift_pinned_verifier_reproduces_stored_policy_hash():
    """The HEAD-committed-bytes bundle must reproduce the exact stored policy
    hash carried by checkpoints made before native_ocr.swift went dirty."""
    bundle = fr.find_pinned_verifier(CPU_SWIFT_PINNED_HASH)
    assert bundle is not None
    assert bundle["policy_hash"] == CPU_SWIFT_PINNED_HASH
    assert canonical_hash(fr.frozen_policy(bundle)) == CPU_SWIFT_PINNED_HASH


def test_checkpoint_gate_accepts_pinned_but_not_live_only():
    """checkpoint_native_attestation must admit the pinned historical policy."""
    digests = accepted_native_policy_digests()
    assert canonical_hash(native_paragraph_policy()) in digests
    assert PINNED_HASH in digests
    assert CPU_SWIFT_PINNED_HASH in digests


def test_pinned_verifier_uses_historical_glyph_not_live():
    """Loading the frozen verifier must bind the pinned glyph implementation."""
    bundle = fr.find_pinned_verifier(PINNED_HASH)
    frozen_sv = fr._load_frozen_verifier(bundle)
    from proofops.adapters.local import native_glyph_geometry as live_glyph

    # Frozen glyph source differs from the live one at the byte level.
    live_hash = sha256(open(live_glyph.__file__, "rb").read()).hexdigest()
    assert live_hash != bundle["expected_files"]["native_glyph_geometry.py"]
    # The frozen verifier's glyph reference is the pinned implementation.
    assert frozen_sv.native_word_ink_geometry is not live_glyph.native_word_ink_geometry


def test_tampered_frozen_bytes_are_refused():
    """A pinned bundle with altered expected hashes must not be honoured."""
    bad = deepcopy(fr._PINNED_VERIFIERS["native_paragraph_glyph_v2"])
    bad["expected_files"]["source_verification.py"] = "0" * 64
    with pytest.raises(fr.FrozenVerifierUnavailable):
        fr._verify_bundle_bytes(bad)


def test_unknown_policy_is_rejected_not_replayed():
    """A stored policy with no pinned verifier must reject, never silently pass."""
    unknown = dict(
        mode="paragraph_native_glyph_v2",
        glyph_verifier_sha256="a" * 64,
        verifier_sha256="b" * 64,
        rendered_reader_sha256="c" * 64,
        normalization_sha256="d" * 64,
    )
    assert fr.find_pinned_verifier(canonical_hash(unknown)) is None
    with pytest.raises(ValueError):
        fr.replay_native_with_policy(unknown, {"schema": "x"}, None, b"", tenant_id=TENANT)


def test_live_policy_routes_through_live_replay(monkeypatch):
    """When stored policy equals the live verifier, use the normal cached replay."""
    from proofops.adapters.local import native_replay_cache as cache

    cache._replays.clear()
    monkeypatch.setattr(
        source_verification,
        "_rendered_text",
        lambda *a, **k: dict(status="read", text="Page 1 emissions 1234 tCO2e"),
    )
    source = pdf()
    batch = replace(
        candidate(
            "cached", [("P", "paragraph", "Page 1 emissions 1234 tCO2e", (70, 710, 300, 740), ())]
        ),
        source_sha256=sha256(source).hexdigest(),
    )
    graph = fuse_candidates((batch,), tenant_id=TENANT)
    receipt = source_verification.attest_native_sources(graph, source, tenant_id=TENANT)
    replayed = fr.replay_native_with_policy(
        native_paragraph_policy(), receipt, graph, source, tenant_id=TENANT
    )
    assert replayed.blocks[0].quality == "verified"
