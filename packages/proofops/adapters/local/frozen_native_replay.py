"""Versioned replay of native paragraph attestations against pinned verifier source.

A frozen local KB stores a native attestation receipt together with the exact
verifier policy that produced it. When the live verifier source evolves, the
stored policy hash no longer equals ``native_paragraph_policy()`` and cold
replay raises ``NATIVE_PARAGRAPH_CHECKPOINT_INVALID``. That guard is correct:
we must never accept a receipt we cannot reproduce.

This module recovers the *only* safe compatibility path: it vendors the exact
historical verifier source (byte-identical to the git commit that produced the
receipt) under ``_frozen_verifiers/`` and re-executes it to recompute the
receipt. A historical policy is honoured *only* when every vendored file still
hashes to the pinned digest, so an operator can never allowlist a policy hash
without shipping the verifier bytes that reproduce it. Tampered or unknown
policies are rejected exactly like the live path.

Dependency isolation: the vendored ``source_verification.py`` byte-identically
imports the absolute name ``proofops.adapters.local.native_glyph_geometry``.
To run the *historical* glyph geometry (whose hash is pinned by the policy)
without editing the vendored bytes and without mutating global ``sys.modules``,
we load each vendored file under a private module name and then rebind the
``native_word_ink_geometry`` global on the private ``source_verification``
instance to the frozen glyph implementation. ``attest_native_sources`` resolves
that name from its module namespace at call time, so the rebind is race-free
and process-local. No network, model, or AWS calls are made.
"""

from __future__ import annotations

import importlib.util
from hashlib import sha256
from pathlib import Path
from types import ModuleType

from proofops.domain.provenance import canonical_hash

_FROZEN_ROOT = Path(__file__).with_name("_frozen_verifiers")

# Each pinned policy maps its canonical policy hash to the vendored verifier
# bundle that reproduces it. ``expected_files`` lists every source file whose
# sha256 is embedded in the stored policy; the bundle is refused unless all
# bytes match, so a policy hash can never be honoured without its verifier.
#
# native_paragraph_glyph_v2 corresponds to git commit
# d70dc8851e33c5bf2773df9ea5e51e340f78e89e
# ("Verify paragraph crops against source-bound glyph geometry").
_PINNED_VERIFIERS = {
    "native_paragraph_glyph_v2": {
        "policy_hash": "2f68a213aa7c32ce25110bb57cb9dc5b03036d808956ffb417a243b85c07556e",
        "dir": "native_paragraph_glyph_v2",
        "expected_files": {
            "source_verification.py": (
                "1d53a9cc0241668c9f429a9132f3ae94e1c2fb9138e6c02a99cd022222c9873e"
            ),
            "native_glyph_geometry.py": (
                "5418c0e4c917db4ea3f85d805dfe2e05b010126019485818f31b7fbec22c9a31"
            ),
            "native_ocr.swift": (
                "0136208b3c4cb65e7bc3f2b7bc126949940fea234952eb9fc81374dde064fefa"
            ),
        },
    },
    # native_paragraph_glyph_v2_cpu_swift corresponds to HEAD at commit
    # a3d66741f3c342ad0622d65727f3e3d94345a8d3 ("Avoid unsupported Vision
    # execution and record repaired live source pipeline") -- the same
    # source_verification.py / native_glyph_geometry.py as the live verifier
    # today, plus the native_ocr.swift blob committed at HEAD. A local,
    # uncommitted edit to native_ocr.swift (forcing Vision to run
    # usesCPUOnly) changed the *live* rendered_reader_sha256 away from this
    # HEAD-committed value, so checkpoints stamped against the committed
    # verifier now fail live replay. This bundle lets those checkpoints
    # replay against the exact HEAD bytes without touching the dirty
    # native_ocr.swift file or any other live verifier source.
    "native_paragraph_glyph_v2_cpu_swift": {
        "policy_hash": "cf201df19cfa9770a7f66ad67d1906388de7417863d7421ae84093938df0f8f6",
        "dir": "native_paragraph_glyph_v2_cpu_swift",
        "expected_files": {
            "source_verification.py": (
                "e9062fb57f98ae0db3a90a2431328c65d315bee87a23c0d11898503f2fdb8fe0"
            ),
            "native_glyph_geometry.py": (
                "71da3f12656dc00bc6791d78aee83cda8ce2f233503b7f6320a8bc2e6b154685"
            ),
            "native_ocr.swift": (
                "0136208b3c4cb65e7bc3f2b7bc126949940fea234952eb9fc81374dde064fefa"
            ),
        },
    },
}


class FrozenVerifierUnavailable(RuntimeError):
    """The pinned verifier bundle for a stored policy is missing or altered."""


def _verify_bundle_bytes(bundle: dict) -> Path:
    """Confirm every vendored file still hashes to its pinned digest."""
    base = _FROZEN_ROOT / bundle["dir"]
    for name, digest in bundle["expected_files"].items():
        path = base / name
        if not path.is_file():
            raise FrozenVerifierUnavailable(f"missing frozen verifier file: {name}")
        actual = sha256(path.read_bytes()).hexdigest()
        if actual != digest:
            raise FrozenVerifierUnavailable(
                f"frozen verifier {name} hash {actual} != pinned {digest}"
            )
    return base


def _exec_private(base: Path, filename: str, module_name: str) -> ModuleType:
    """Load a vendored file under a private module name (no global registration)."""
    spec = importlib.util.spec_from_file_location(module_name, base / filename)
    if spec is None or spec.loader is None:
        raise FrozenVerifierUnavailable(f"cannot load frozen module {filename}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def find_pinned_verifier(stored_policy_hash: str) -> dict | None:
    """Return the pinned verifier bundle for a stored policy hash, if registered."""
    for bundle in _PINNED_VERIFIERS.values():
        if bundle["policy_hash"] == stored_policy_hash:
            return bundle
    return None


def frozen_policy(bundle: dict) -> dict:
    """Recompute the historical policy dict from the (hash-verified) vendored bytes."""
    _verify_bundle_bytes(bundle)
    from proofops.application.evidence import citations

    return dict(
        mode="paragraph_native_glyph_v2",
        glyph_verifier_sha256=bundle["expected_files"]["native_glyph_geometry.py"],
        verifier_sha256=bundle["expected_files"]["source_verification.py"],
        rendered_reader_sha256=bundle["expected_files"]["native_ocr.swift"],
        normalization_sha256=sha256(Path(citations.__file__).read_bytes()).hexdigest(),
    )


def _load_frozen_verifier(bundle: dict) -> ModuleType:
    """Load the pinned verifier with the pinned glyph geometry bound in.

    Both files are loaded under private module names. The historical
    ``source_verification`` imports the *live* glyph at module top level; we
    rebind ``native_word_ink_geometry`` on the private instance to the frozen
    glyph implementation, which ``attest_native_sources`` resolves at call time.
    This uses the historical glyph logic without mutating global state.
    """
    base = _verify_bundle_bytes(bundle)
    glyph = _exec_private(
        base,
        "native_glyph_geometry.py",
        "proofops.adapters.local._frozen.native_glyph_geometry_v2",
    )
    frozen_sv = _exec_private(
        base,
        "source_verification.py",
        "proofops.adapters.local._frozen.source_verification_v2",
    )
    # Dynamic module loaded from vendored bytes; rebind the glyph dependency.
    setattr(frozen_sv, "native_word_ink_geometry", glyph.native_word_ink_geometry)
    return frozen_sv


def replay_native_sources_frozen(bundle, receipt, graph, source, *, tenant_id):
    """Re-execute the pinned historical verifier to reproduce and replay the receipt."""
    frozen_sv = _load_frozen_verifier(bundle)
    return frozen_sv.replay_native_sources(receipt, graph, source, tenant_id=tenant_id)


def replay_native_with_policy(stored_policy, receipt, graph, source, *, tenant_id):
    """Replay a native receipt using the live verifier or a pinned historical one.

    Returns the replayed graph. Raises ValueError (matching the live replay
    contract) when the receipt cannot be reproduced under any accepted verifier.
    """
    from proofops.adapters.local.native_replay_cache import replay_cached
    from proofops.adapters.local.run_artifacts import native_paragraph_policy

    live_hash = canonical_hash(native_paragraph_policy())
    stored_hash = canonical_hash(stored_policy)
    if stored_hash == live_hash:
        return replay_cached(receipt, graph, source, tenant_id=tenant_id)
    bundle = find_pinned_verifier(stored_hash)
    if bundle is None:
        # Unknown/tampered policy: no verifier reproduces it. Reject.
        raise ValueError("native attestation policy has no pinned verifier")
    if canonical_hash(frozen_policy(bundle)) != stored_hash:
        raise ValueError("frozen verifier does not reproduce stored policy")
    return replay_native_sources_frozen(bundle, receipt, graph, source, tenant_id=tenant_id)
