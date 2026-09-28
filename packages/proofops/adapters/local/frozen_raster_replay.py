"""Compatibility replay of v5 raster checkpoints pinned to a historical native verifier.

``raster_ocr_policy()`` (raster_visibility.py) embeds
``canonical_hash(native_paragraph_policy())`` as its ``native_policy_sha256``
field, and that whole policy dict is itself one of the values hashed into a
run's own ``input_hash`` at run-creation time (via the committed run
snapshot). ``raster_checkpoint.py`` and ``raster_visibility.py`` also hash
their *own* file bytes into that same policy (``checkpoint_helper_sha256``,
``composition_helper_sha256``). When the live native verifier later changes
for an accepted, pinned-historical reason (see ``frozen_native_replay.py`` /
``accepted_native_policy_digests``), the stored policy's
``native_policy_sha256`` no longer matches the live recomputation, and cold
replay of an already-published checkpoint fails -- even though every other
component is still byte-identical.

Editing ``raster_checkpoint.py`` or ``raster_visibility.py`` to add
tolerance is not an option: doing so changes their own file bytes, which
changes ``raster_ocr_policy()``'s live hash and retroactively invalidates
every currently-published v5 checkpoint (including other tenants' live
runs) -- exactly backwards from a compatibility fix.

This module follows the same pattern as ``frozen_native_replay.py``: it
verifies the hash-pinned verifier bundle for the stored
``native_policy_sha256``, then loads *private, unmodified-byte* copies of
``raster_visibility.py`` and ``raster_checkpoint.py`` (their current file
contents -- these two files are not being edited, so their current bytes are
exactly what is hash-verified) under private module names via
``importlib``, exactly as ``frozen_native_replay._exec_private`` already
does for the native layer. The only rebind is the private
``raster_visibility`` instance's ``replay_native_sources`` name, pointed at
the pinned historical verifier instead of the live one, so every other
check in the real, unmodified checkpoint/visibility/request logic runs
verbatim against the original stored ``snapshot``/``message``/``receipts``.
No process-global monkeypatch: only the private module instances are
rebound, and only when the stored policy's native digest is a verified
pinned historical one.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from proofops.adapters.local.run_artifacts import (
    accepted_native_policy_digests,
    raster_policy_matches_accepted,
)

_LOCAL = Path(__file__).parent


def raster_policy_requires_pinned_native(snapshot):
    """True only when the stored raster policy's `native_policy_sha256` is a
    verified pinned historical digest and every other field still matches
    the live composition exactly -- the one legitimate divergence this
    module exists to tolerate. False for a live-matching policy (the normal,
    unmodified `raster_checkpoint` path applies) and for any other kind of
    drift (the caller must still reject, never fall back to this module)."""
    if not isinstance(snapshot, dict) or "raster_ocr_policy" not in snapshot:
        return False
    policy = snapshot["raster_ocr_policy"]
    if not isinstance(policy, dict):
        return False
    from proofops.adapters.local.raster_visibility import raster_ocr_policy

    try:
        live = raster_ocr_policy(
            mode=policy.get("mode"),
            max_pages=policy.get("max_pages"),
            max_calls=policy.get("max_calls"),
        )
    except (ValueError, TypeError, KeyError):
        return False
    if policy == live:
        return False  # live-matching: let the unmodified path handle it.
    return raster_policy_matches_accepted(policy, raster_ocr_policy)


def _exec_private(path: Path, module_name: str) -> ModuleType:
    """Load a module's *current, on-disk* bytes under a private module name.

    Mirrors `frozen_native_replay._exec_private`. `raster_visibility.py` and
    `raster_checkpoint.py` are not edited by this compatibility path, so
    their current file bytes are exactly the hash-verified bytes; loading
    them privately (instead of importing the shared module object) lets us
    rebind one dependency on the private instance without any process-global
    monkeypatch of the real, shared module used by every other run.
    """
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load module {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_private_raster_modules(native_policy):
    """Load private `raster_visibility` + `raster_checkpoint` instances whose
    native-verifier dependency is rebound to the pinned historical replayer
    bound to this run's own trusted `native_policy`, never to the receipt."""
    from proofops.adapters.local.frozen_native_replay import replay_native_with_policy

    visibility = _exec_private(
        _LOCAL / "raster_visibility.py",
        "proofops.adapters.local._frozen.raster_visibility_pinned",
    )

    def _pinned_replay_native_sources(receipt, graph, source, *, tenant_id):
        return replay_native_with_policy(native_policy, receipt, graph, source, tenant_id=tenant_id)

    # Rebind only on this private instance; the shared, live module used by
    # every other run/tenant is untouched. `raster_ocr_policy()` reads
    # `native_paragraph_policy` as a bare name from its own module globals
    # (imported at the top of raster_visibility.py), so rebinding that name
    # -- not just `replay_native_sources` -- makes its embedded
    # `native_policy_sha256` match this run's trusted, pinned policy instead
    # of recomputing the live (possibly dirty) verifier.
    visibility.native_paragraph_policy = lambda: native_policy
    visibility.replay_native_sources = _pinned_replay_native_sources

    checkpoint = _exec_private(
        _LOCAL / "raster_checkpoint.py",
        "proofops.adapters.local._frozen.raster_checkpoint_pinned",
    )
    # The private checkpoint module imported the *real* visibility module's
    # names at load time; rebind it to resolve against the pinned instance.
    checkpoint.raster_ocr_policy = visibility.raster_ocr_policy
    checkpoint.corroborate_native_visibility = visibility.corroborate_native_visibility
    checkpoint.eligible_raster_sources = visibility.eligible_raster_sources
    checkpoint.replay_native_sources = _pinned_replay_native_sources
    return checkpoint


def replay_raster_records_frozen(
    snapshot, message, native, graph, source, registrations, receipts, native_policy
):
    """Replay a v5 raster checkpoint pinned to a verified historical native
    verifier, by delegating to private, unmodified-byte instances of the
    real `raster_checkpoint`/`raster_visibility` logic with only the native
    replayer rebound to the pinned historical verifier.

    `native_policy` must be the run's own durably bound native-paragraph
    policy (`store.jobs.parser_native_policy(message)`), the same trusted
    value the non-raster path binds via `checkpoint_native_attestation` /
    `replay_native_with_policy` -- never reconstructed from the receipt
    itself, so this never widens what counts as an accepted verifier.
    """
    policy = snapshot["raster_ocr_policy"]
    if (
        not isinstance(policy, dict)
        or policy.get("native_policy_sha256") not in accepted_native_policy_digests()
    ):
        raise ValueError("RASTER_CHECKPOINT_POLICY_CHANGED")
    checkpoint = _load_private_raster_modules(native_policy)
    return checkpoint.replay_raster_records(
        snapshot, message, native, graph, source, registrations, receipts
    )
