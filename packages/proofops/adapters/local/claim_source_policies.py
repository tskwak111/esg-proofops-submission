"""Claim-source policy dispatch shared by run creation, publication and load.

The base verifier (``claim_source_verification``) keeps its own known-version
dispatch untouched, including its vendored frozen predecessors. This module only
adds the explicitly opted-in render-resolution wrapper
(``claim_span_render_resolution``) as a SECOND admissible policy and gives every
caller -- run snapshot validation, the worker's single-shot and batch extract
publication, and the claim store's replay -- one shared decision instead of a
duplicated branch per call site.

Rollback is "do not pin the new policy on future runs": nothing here reinterprets
a stored receipt. A run that pinned the base policy still resolves to the base
verifier, a run that pinned a frozen predecessor still replays under that frozen
module, and both keep their original receipt schema and bytes.
"""

from collections.abc import Mapping

RENDER_RESOLUTION_POLICY_SCHEMA = "claim_span_render_resolution_policy_v1"
# R19 bullet-alignment wrapper. It WRAPS the render-resolution wrapper rather
# than replacing it, so pinning it preserves render resolution instead of
# trading one recovery for the other.
BULLET_ALIGNMENT_POLICY_SCHEMA = "claim_span_bullet_alignment_policy_v1"
# R24 rendered-side-only typography wrapper. It WRAPS the bullet-alignment
# wrapper rather than replacing it, so pinning it preserves both inner
# recoveries. Its native gate is the base verifier's, unfolded.
TYPOGRAPHY_POLICY_SCHEMA = "claim_span_typography_policy_v1"
# Accepted in a run snapshot. The two base schemas are the base verifier's own;
# the others are each wrapper's distinct schema, so no receipt or policy of one
# kind can ever be read as the other.
CLAIM_SOURCE_POLICY_SCHEMAS = frozenset(
    {
        "claim_source_policy_v1",
        "claim_source_policy_v2",
        RENDER_RESOLUTION_POLICY_SCHEMA,
        BULLET_ALIGNMENT_POLICY_SCHEMA,
        TYPOGRAPHY_POLICY_SCHEMA,
    }
)


def _render_resolution():
    from proofops.adapters.local import claim_span_render_resolution

    return claim_span_render_resolution


def _bullet_alignment():
    from proofops.adapters.local import claim_span_bullet_alignment

    return claim_span_bullet_alignment


def _typography():
    from proofops.adapters.local import claim_span_typography

    return claim_span_typography


def _wrappers():
    """Every wrapper module, outermost first. Additive: the base verifier's own
    dispatch is untouched and each wrapper keeps its own distinct policy."""
    return (_typography(), _bullet_alignment(), _render_resolution())


def claim_source_reader(policy):
    """Replay dispatch for a policy already pinned in a run snapshot.

    The wrapper policy resolves to the wrapper module, and only when it matches
    that module's current policy byte-for-byte: an altered wrapper hash is
    refused rather than replayed under different code. Every other policy is
    handed to the base verifier's own dispatch unchanged, so legacy and frozen
    runs keep replaying under the verifier that produced them.
    """
    from proofops.adapters.local import claim_source_verification as base

    if isinstance(policy, Mapping):
        for schema, module in (
            (TYPOGRAPHY_POLICY_SCHEMA, _typography),
            (BULLET_ALIGNMENT_POLICY_SCHEMA, _bullet_alignment),
            (RENDER_RESOLUTION_POLICY_SCHEMA, _render_resolution),
        ):
            if policy.get("schema") != schema:
                continue
            wrapper = module()
            if policy != wrapper.claim_source_policy():
                raise ValueError("CLAIM_SOURCE_POLICY_UNKNOWN")
            return wrapper
    return base.claim_source_reader(policy)


def publication_reader(policy):
    """Reader admissible for a NEW publication: the current base verifier or the
    current opt-in wrapper, nothing else.

    Frozen predecessors stay replay-only exactly as before this module existed:
    a run pinned to an older base policy is still rejected with
    ``CLAIM_SOURCE_POLICY_MISMATCH`` instead of publishing a fresh attestation
    under code it was not created with.
    """
    from proofops.adapters.local import claim_source_verification as base

    for module in (base, *_wrappers()):
        if policy == module.claim_source_policy():
            return module
    raise ValueError("CLAIM_SOURCE_POLICY_MISMATCH")


def attest_claims(*, reader, graph, source, refs, tenant_id, cache):
    """Publish an attestation under ``reader``'s own semantics.

    ``cache`` keeps each existing call site's behaviour: the batch path reuses
    the base reader's incremental per-ref attestation cache, the single-shot
    path does not. No wrapper is ever routed through that cache regardless:
    a wrapper receipt carries whole-receipt fields (``base_records``,
    ``render_retries``, ``bullet_alignments``, ``typography_reads``) that a
    per-record cache composition would leave inconsistent with a fresh
    recompute, and every
    wrapper's replay recomputes the receipt in full, so a composed receipt
    would be refused at load time. The separate replay cache in
    ``native_replay_cache`` is keyed on the whole receipt and stays usable for
    both readers.

    A wrapper receipt is therefore recomputed in full here and then remembered
    whole, not composed: ``remember_wrapper_attestation`` stores these exact
    canonical bytes so the strict replay that immediately follows the batch
    publication can match them instead of repeating the same whole-ref read.
    The published bytes are unchanged either way.
    """
    if cache and reader not in _wrappers():
        from proofops.adapters.local.native_replay_cache import attest_claims_cached

        return attest_claims_cached(
            reader=reader, graph=graph, source=source, refs=refs, tenant_id=tenant_id
        )
    receipt = reader.attest_claim_spans(graph, source, tuple(refs), tenant_id=tenant_id)
    if cache:
        from proofops.adapters.local.native_replay_cache import remember_wrapper_attestation

        remember_wrapper_attestation(
            reader=reader,
            policy=reader.claim_source_policy(),
            graph=graph,
            source=source,
            tenant_id=tenant_id,
            receipt=receipt,
        )
    return receipt
