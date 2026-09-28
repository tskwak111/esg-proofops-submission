"""The wrapper claim replay reuses the attestation it already recomputed.

Measured problem (R24, NAVER expanded run): every extract batch recomputes the
frozen bullet-alignment wrapper twice over the WHOLE ref set for the same pinned
inputs -- once to publish the new receipt
(``claim_source_policies.attest_claims``) and once more, out of the worker's
latency timer, when ``LocalClaimStore.load_evidence`` strictly replays the
revision it just published. Both calls run the identical
``claim_span_bullet_alignment.apply_bullet_alignment`` over the identical
(tenant, source bytes, graph, refs, policy), each reopening the PDF per ref.

These tests pin the reuse and its refusals. Nothing here relaxes the frozen
replay: the reuse only succeeds when the stored receipt is canonically
byte-identical to an attestation this very process recomputed from the original
bytes under the same strict key, and every other case still runs the frozen
wrapper replay. No model, network or AWS call.
"""

from dataclasses import replace

import pytest
from proofops.adapters.local import claim_source_policies as policies
from proofops.adapters.local import claim_source_verification as base
from proofops.adapters.local import claim_span_bullet_alignment as wrapper
from proofops.adapters.local import native_replay_cache as cache
from proofops.domain.rulepacks import canonical_json

from tests.acceptance.test_parsing import TENANT
from tests.integration.test_batch_attestation_cache import discovery_for
from tests.integration.test_claim_source_verification import inputs
from tests.integration.test_claim_span_bullet_alignment import render_gap

UNAVAILABLE = dict(status="unresolved", reason="rendered_reader_unavailable", error="OSError")


def clear():
    cache._claim_attestations.clear()
    cache._claim_replays.clear()
    cache._claim_wrapper_receipts.clear()


def recomputes(monkeypatch):
    """Count full frozen-wrapper recomputes (the expensive per-ref PDF reads)."""
    calls = []
    original = wrapper.apply_bullet_alignment

    def counted(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(wrapper, "apply_bullet_alignment", counted)
    return calls


def case():
    source, graph, ref = inputs()
    discovery = discovery_for(graph, (ref,))
    return source, graph, discovery, base.discovery_refs(discovery)


def publish(graph, source, refs, *, cached=True):
    return policies.attest_claims(
        reader=wrapper,
        graph=graph,
        source=source,
        refs=refs,
        tenant_id=TENANT,
        cache=cached,
    )


def replay(*, receipt, graph, source, discovery, policy=None, tenant_id=TENANT):
    return cache.replay_claims_cached(
        reader=wrapper,
        policy=policy or wrapper.claim_source_policy(),
        receipt=receipt,
        graph=graph,
        source=source,
        discovery=discovery,
        tenant_id=tenant_id,
    )


def test_the_published_wrapper_receipt_stays_byte_identical_to_a_direct_attestation(monkeypatch):
    """Whole-wrapper receipt bytes are unchanged: no per-record composition."""
    render_gap(monkeypatch)
    clear()
    source, graph, discovery, refs = case()
    direct = wrapper.attest_claim_spans(graph, source, refs, tenant_id=TENANT)
    clear()
    published = publish(graph, source, refs)
    assert published == direct
    assert canonical_json(published) == canonical_json(direct)
    assert published["schema"] == wrapper.SCHEMA
    assert published["policy"] == wrapper.claim_source_policy()


def test_the_replay_reuses_the_attestation_recomputed_in_the_same_process(monkeypatch):
    render_gap(monkeypatch)
    clear()
    source, graph, discovery, refs = case()
    receipt = publish(graph, source, refs)
    frozen = wrapper.replay_claim_spans(receipt, graph, source, discovery, tenant_id=TENANT)
    # A COMPLETED read with a negative verdict is still reusable: only an
    # unavailable/errored reader is not (see the retry test below).
    assert any(record["status"] == "unresolved" for record in receipt["base_records"])
    assert not any(
        record.get("reason") == "rendered_reader_unavailable" for record in receipt["records"]
    )

    clear()
    receipt = publish(graph, source, refs)
    calls = recomputes(monkeypatch)
    replayed, scoped = replay(receipt=receipt, graph=graph, source=source, discovery=discovery)
    # The out-of-timer strict replay no longer repeats the whole-ref recompute.
    assert calls == []
    # ...and returns exactly what the frozen wrapper replay returns.
    assert (replayed, scoped) == frozen


def test_a_cold_process_still_runs_the_frozen_replay_and_then_may_reuse_it(monkeypatch):
    render_gap(monkeypatch)
    clear()
    source, graph, discovery, refs = case()
    receipt = publish(graph, source, refs, cached=False)
    clear()
    calls = recomputes(monkeypatch)
    cold = replay(receipt=receipt, graph=graph, source=source, discovery=discovery)
    assert len(calls) == 1
    cache._claim_replays.clear()
    assert replay(receipt=receipt, graph=graph, source=source, discovery=discovery) == cold
    assert len(calls) == 1


def test_reuse_is_bound_to_tenant_source_graph_refs_and_policy(monkeypatch):
    render_gap(monkeypatch)
    clear()
    source, graph, discovery, refs = case()
    receipt = publish(graph, source, refs)
    calls = recomputes(monkeypatch)
    pinned = dict(
        receipt=receipt, graph=graph, source=source, discovery=discovery, tenant_id=TENANT
    )

    # A different tenant, source, graph, ref set or policy must never reuse it.
    for changed, recomputed, refused in (
        (dict(tenant_id="00000000-0000-4000-8000-000000000001"), True, True),
        (dict(source=source + b"\n"), True, True),
        (dict(graph=replace(graph, source_sha256="0" * 64)), True, True),
        # An unpinned policy is not projected; the frozen replay runs as before.
        (dict(policy=dict(wrapper.claim_source_policy(), wrapper_sha256="0" * 64)), True, False),
        # A different ref set is refused by the wrapper before it even reads.
        (
            dict(discovery=discovery_for(graph, (replace(refs[0], char_start=0, char_end=6),))),
            False,
            True,
        ),
    ):
        before = len(calls)
        cache._claim_replays.clear()
        if refused:
            with pytest.raises(ValueError):
                replay(**(pinned | changed))
        else:
            assert replay(**(pinned | changed)) is not None
        assert (len(calls) > before) is recomputed, changed


def test_a_tampered_receipt_is_refused_instead_of_reused(monkeypatch):
    render_gap(monkeypatch)
    clear()
    source, graph, discovery, refs = case()
    receipt = publish(graph, source, refs)
    calls = recomputes(monkeypatch)
    for altered in (
        dict(receipt, records=[dict(receipt["records"][0], status="verified", reason="forced")]),
        dict(receipt, artifact_sha256="0" * 64),
        dict(receipt, readings={}),
        dict(receipt, schema="claim_span_render_resolution_attestation_v1"),
    ):
        cache._claim_replays.clear()
        with pytest.raises(ValueError):
            replay(receipt=altered, graph=graph, source=source, discovery=discovery)
    assert len(calls) >= 1


def test_an_unavailable_rendered_read_is_retried_and_never_remembered(monkeypatch):
    """A reader that could not read at all is retried, by BOTH caches.

    The wrapper's own receipt schema does not reach the base-schema reuse guard,
    so the generic full-replay cache must not retain such a result either.
    """
    render_gap(monkeypatch)
    monkeypatch.setattr(base, "_rendered_text", lambda *a, **k: dict(UNAVAILABLE))
    clear()
    source, graph, discovery, refs = case()
    receipt = publish(graph, source, refs)
    assert any(r.get("reason") == "rendered_reader_unavailable" for r in receipt["records"])
    assert not cache._claim_wrapper_receipts
    calls = recomputes(monkeypatch)
    first = replay(receipt=receipt, graph=graph, source=source, discovery=discovery)
    assert len(calls) == 1
    # Neither cache kept it, so the identical replay reads again.
    assert not cache._claim_wrapper_receipts
    assert not cache._claim_replays
    assert replay(receipt=receipt, graph=graph, source=source, discovery=discovery) == first
    assert len(calls) == 2
    assert not cache._claim_wrapper_receipts
    assert not cache._claim_replays


def test_reuse_is_bound_to_the_recorded_toolchain(monkeypatch):
    render_gap(monkeypatch)
    clear()
    source, graph, discovery, refs = case()
    receipt = publish(graph, source, refs)
    calls = recomputes(monkeypatch)
    monkeypatch.setenv("DEVELOPER_DIR", "/changed-toolchain")
    replay(receipt=receipt, graph=graph, source=source, discovery=discovery)
    assert len(calls) == 1
