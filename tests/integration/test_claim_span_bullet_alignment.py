"""R19: the opt-in bullet-alignment wrapper, on real measured geometry.

Two layers are under test and nothing else:

* the admission rule, replayed against the REAL pinned-box token geometry the
  frozen readers produced on the NAVER physical page 90 run (``d5f7908d``),
  stored in ``tests/fixtures/pipeline/bullet-spacing.json``. The 7 real
  recoveries, the 7 tampered negative controls and every structural refusal are
  exercised against measured coordinates, not invented ones;
* the wiring: a new run pins the wrapper's own policy, the real worker
  publishes its own receipt schema, the normal ``LocalClaimStore`` load admits
  only the per-record promoted spans, and the R15 render-resolution recovery it
  wraps is preserved rather than replaced.

No model, network or AWS call. The OCR step is the existing monkeypatched
fixture in the wiring test; the replay layer needs no PDF at all.
"""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from proofops.adapters.local import claim_source_policies as policies
from proofops.adapters.local import claim_source_verification as base
from proofops.adapters.local import claim_span_bullet_alignment as wrapper
from proofops.adapters.local import claim_span_render_resolution as r15
from proofops.adapters.local import selected_cell_table_verification as cell_reader
from proofops.application.evidence.citations import _normalized
from proofops.application.ingest.graph_fusion import fuse_candidates

from tests.acceptance.test_parsing import TENANT, candidate
from tests.integration.test_batch_attestation_cache import discovery_for
from tests.integration.test_claim_source_verification import inputs

RESULTS = Path(__file__).resolve().parents[2] / "tests/fixtures/pipeline"
EVIDENCE = RESULTS / "bullet-spacing.json"
# The coordinator's recorded KB page-30 differences: the SAME defect with the
# other observed bullet (U+00B7). Recorded from that run's own glyph data, no
# new OCR or model call.
KB_EVIDENCE = RESULTS / "kb-native-differences.json"


def measured():
    """The real run's cases, each with its real pinned-box token geometry."""
    return json.loads(EVIDENCE.read_text())["cases"]


def tokens(case, key="selected_tokens_geometry"):
    return [dict(text=t["text"], bbox=tuple(t["bbox"])) for t in case[key]]


def kb_cases():
    return json.loads(KB_EVIDENCE.read_text())


def line(*spans):
    """Same-ink-line tokens from (text, x0, x1) triples; identical y band."""
    return [dict(text=t, bbox=(x0, 0.0, x1, 10.0)) for t, x0, x1 in spans]


# --- the admission rule on real geometry ------------------------------------


def test_every_real_case_admits_exactly_the_bullet_boundary():
    cases = measured()
    assert len(cases) == 8
    for case in cases:
        transform = wrapper.bullet_alignment(tokens(case))
        assert transform is not None, case["source_id"]
        assert transform["boundary_token_index"] == wrapper._BOUNDARY_INDEX == 1
        assert transform["deleted_inserted_spaces"] == 1
        assert transform["mode"] == wrapper.MODE
        # The admitted boundary's real ink gap is strictly the tightest.
        assert transform["ink_gap_pt"] < transform["next_smallest_ink_gap_pt"]
        assert transform["ink_gap_pt"] == case["ink_geometry"]["spurious_boundary_gap_pt"]


def test_one_deletion_reproduces_the_parser_text_on_every_real_case():
    for case in measured():
        selected = tokens(case)
        transform = wrapper.bullet_alignment(selected)
        unaligned = wrapper._join(selected)
        aligned = wrapper._join(selected, drop=transform["boundary_token_index"])
        # The base's own verdict is reproduced (mismatch) and one deletion fixes it.
        assert _normalized(unaligned) != _normalized(case["parser_raw_text"])
        assert _normalized(aligned) == _normalized(case["parser_raw_text"])
        # Exactly one space fewer: no other whitespace is touched.
        assert unaligned.count(" ") - aligned.count(" ") == 1


def test_the_tampered_negative_controls_are_all_refused():
    """A changed number, a changed genuine space, a changed word or punctuation
    can never be reached by the single admitted deletion."""
    by_id = {case["source_id"]: case for case in measured()}
    controls = [
        ("b80b6685-c935-5688-a1b0-74d72aa28683", "0.2m", "0.3m"),
        ("290f55ba-6c80-5763-bb8d-445c7fa3e01b", "500년", "600년"),
        ("53059cfd-e155-56e5-ac0e-e2bf8a4bdb56", "90m", "90km"),
        ("290f55ba-6c80-5763-bb8d-445c7fa3e01b", "하천 월류", "하천월류"),
        ("4e3a8644-8e59-5afd-abc1-b4389b918206", "남측 구내", "남 측 구내"),
        ("22012e41-6561-586b-8b50-66b59455db02", "없는", "있는"),
        ("4e3a8644-8e59-5afd-abc1-b4389b918206", "수(도시침수):", "수 도시침수:"),
    ]
    for source_id, old, new in controls:
        case = by_id[source_id]
        tampered = case["parser_raw_text"].replace(old, new)
        assert tampered != case["parser_raw_text"], (source_id, old)
        selected = tokens(case)
        transform = wrapper.bullet_alignment(selected)
        aligned = wrapper._join(selected, drop=transform["boundary_token_index"])
        assert _normalized(aligned) != _normalized(tampered), (source_id, old, new)
    # And the rule is strictly narrower than stripping whitespace, which would
    # wrongly accept the two genuine-space controls.
    case = by_id["290f55ba-6c80-5763-bb8d-445c7fa3e01b"]
    stripped = _normalized(wrapper._join(tokens(case))).replace(" ", "")
    tampered = case["parser_raw_text"].replace("하천 월류", "하천월류")
    assert stripped == _normalized(tampered).replace(" ", "")


@pytest.mark.parametrize(
    "selected",
    [
        # Not a bullet line.
        line(("-", 0, 5), ("시", 7, 15), ("설", 16, 30), ("영향", 32, 50)),
        # Second token is a digit, or more than one character.
        line(("\u2022", 0, 5), ("9", 7, 15), ("억", 16, 30), ("원", 32, 50)),
        line(("\u2022", 0, 5), ("시설", 7, 20), ("영향", 21, 35), ("평가", 37, 55)),
        # Third token starts with a digit, or with punctuation that is not the
        # observed hyphen, or with a hyphen not followed by a letter.
        line(("\u2022", 0, 5), ("약", 7, 15), ("90m", 16, 30), ("이상", 32, 50)),
        line(("\u2022", 0, 5), ("시", 7, 15), ("(설)", 16, 30), ("영향", 32, 50)),
        line(("\u2022", 0, 5), ("K", 7, 15), ("-9", 16, 30), ("이상", 32, 50)),
        # Fewer than three tokens.
        line(("\u2022", 0, 5), ("시", 7, 15)),
        # Only one same-ink-line boundary, so there is no baseline to compare
        # the candidate gap against.
        [
            dict(text="\u2022", bbox=(0.0, 0.0, 5.0, 10.0)),
            dict(text="시", bbox=(7.0, 0.0, 15.0, 10.0)),
            dict(text="설", bbox=(0.0, 12.0, 14.0, 22.0)),
        ],
    ],
)
def test_structurally_inadmissible_shapes_are_refused(selected):
    assert wrapper.bullet_alignment(selected) is None


def test_a_gap_tie_and_a_tighter_gap_elsewhere_are_both_refused():
    # Bullet boundary gap 1.0 ties with a later boundary: refused.
    tie = line(("\u2022", 0, 5), ("시", 6, 15), ("설", 16, 30), ("영향", 31, 50))
    assert wrapper.bullet_alignment(tie) is None
    # The tightest gap is a later boundary, not the bullet one: refused, so a
    # genuinely tight non-bullet boundary is never joined.
    elsewhere = line(("\u2022", 0, 5), ("시", 8, 15), ("설", 18, 30), ("영향", 30.1, 50))
    assert wrapper.bullet_alignment(elsewhere) is None


def test_a_line_break_at_the_bullet_boundary_is_refused():
    wrapped = [
        dict(text="\u2022", bbox=(0.0, 0.0, 5.0, 10.0)),
        dict(text="시", bbox=(6.0, 0.0, 15.0, 10.0)),
        dict(text="설", bbox=(0.0, 12.0, 14.0, 22.0)),  # next ink line
        dict(text="영향", bbox=(16.0, 12.0, 34.0, 22.0)),
    ]
    assert wrapper.bullet_alignment(wrapped) is None


def test_the_other_observed_bullet_is_admitted_on_the_real_kb_geometry():
    """U+00B7 (KB) behaves exactly like U+2022 (NAVER); nothing else is added."""
    cases = kb_cases()
    assert cases
    for case in cases:
        selected = tokens(case, "tokens")
        assert selected[0]["text"] == "\u00b7"
        transform = wrapper.bullet_alignment(selected)
        assert transform is not None, case["source_id"]
        assert transform["boundary_token_index"] == 1
        assert transform["ink_gap_pt"] < transform["next_smallest_ink_gap_pt"]
        unaligned = wrapper._join(selected)
        aligned = wrapper._join(selected, drop=transform["boundary_token_index"])
        assert _normalized(unaligned) == _normalized(case["native"])
        assert _normalized(unaligned) != _normalized(case["parser"])
        assert _normalized(aligned) == _normalized(case["parser"])
        assert unaligned.count(" ") - aligned.count(" ") == 1


def test_only_the_two_observed_bullets_are_admitted():
    for bullet_char in ("\u2022", "\u00b7"):
        assert (
            wrapper.bullet_alignment(
                line((bullet_char, 0, 5), ("시", 8, 15), ("설", 16, 30), ("영향", 32, 50))
            )
            is not None
        )
    # Every other list marker, dash or leading glyph is refused.
    for other in ("\u2023", "\u25cf", "\u002a", "\u2013", "-", "\u25aa", "1."):
        assert (
            wrapper.bullet_alignment(
                line((other, 0, 5), ("시", 8, 15), ("설", 16, 30), ("영향", 32, 50))
            )
            is None
        ), other


# --- policy and dispatch ----------------------------------------------------


def test_the_policy_is_distinct_and_pins_the_baseline_and_both_readers():
    policy = wrapper.claim_source_policy()
    assert policy["schema"] == "claim_span_bullet_alignment_policy_v1"
    assert policy["mode"] == wrapper.MODE
    assert policy["base"] == r15.claim_source_policy()
    assert policy["base"]["schema"] == "claim_span_render_resolution_policy_v1"
    # The frozen base verifier's policy is reachable through, not replaced by,
    # the two wrapper layers.
    assert policy["base"]["base"] == base.claim_source_policy()
    for key in ("wrapper_sha256", "glyph_reader_sha256", "rendered_reader_sha256", "base_sha256"):
        assert len(policy[key]) == 64
    assert wrapper.SCHEMA == "claim_span_bullet_alignment_attestation_v1" != r15.SCHEMA


def test_dispatch_is_additive_and_every_existing_policy_still_routes_as_before():
    assert policies.claim_source_reader(wrapper.claim_source_policy()) is wrapper
    assert policies.claim_source_reader(r15.claim_source_policy()) is r15
    assert policies.claim_source_reader(base.claim_source_policy()) is base
    assert policies.publication_reader(wrapper.claim_source_policy()) is wrapper
    assert policies.publication_reader(r15.claim_source_policy()) is r15
    assert policies.publication_reader(base.claim_source_policy()) is base
    assert wrapper.claim_source_policy()["schema"] in policies.CLAIM_SOURCE_POLICY_SCHEMAS
    # An altered wrapper policy is refused, never replayed under different code.
    altered = dict(wrapper.claim_source_policy(), wrapper_sha256="0" * 64)
    with pytest.raises(ValueError, match="CLAIM_SOURCE_POLICY_UNKNOWN"):
        policies.claim_source_reader(altered)
    with pytest.raises(ValueError, match="CLAIM_SOURCE_POLICY_MISMATCH"):
        policies.publication_reader(altered)


def test_the_real_run_service_admits_the_new_policy_and_still_refuses_an_unknown_one(tmp_path):
    """The actual RunService constructor gate, not a source grep."""
    from proofops.adapters.local.run_store import LocalSQLiteRunStore
    from proofops.application.runs import RunService

    from tests.integration import test_run_lifecycle as lifecycle

    service, _ = lifecycle.setup(tmp_path)

    def build(policy):
        return RunService(
            LocalSQLiteRunStore(service.store.path),
            service.uploads,
            service.registry,
            extraction_profile=service.extraction_profile,
            # The gate admits a claim-source policy only in probe mode.
            extraction_mode="upstage_probe",
            claim_source_policy=policy,
        )

    assert build(wrapper.claim_source_policy()).claim_source_policy["schema"] == (
        "claim_span_bullet_alignment_policy_v1"
    )
    # The policy it wraps is still admissible on its own.
    assert build(r15.claim_source_policy()) is not None
    with pytest.raises(ValueError, match="invalid claim source policy"):
        build(dict(wrapper.claim_source_policy(), schema="claim_span_bullet_alignment_policy_v2"))


# --- wiring: real worker publication and claim-store load -------------------

QUOTE = "emissions 1234 tCO2e"
NATIVE = "Page 1 emissions 1234 tCO2e"
BASE_RENDERED = "Page 1 emissions1234 tCO2e"


def render_gap(monkeypatch):
    """Reuses the R15 fixture: the base crop loses a space, the deterministic
    scale crop does not. This wrapper's own path is exercised on top."""
    monkeypatch.setattr(
        base, "_rendered_text", lambda *a, **k: dict(status="read", text=BASE_RENDERED)
    )
    monkeypatch.setattr(
        cell_reader, "_rendered_cell", lambda page, box: dict(status="read", text=NATIVE, scale=11)
    )


def test_real_worker_publishes_the_wrapper_receipt_and_claim_store_admits_its_spans(
    tmp_path, monkeypatch
):
    from proofops.adapters.local.claim_store import LocalClaimStore
    from proofops.adapters.local.run_store import LocalSQLiteRunStore
    from proofops.application.ports.jobs import JobMessage

    from tests.integration import test_run_lifecycle as lifecycle
    from tests.integration.test_real_extract_runner import FakeProbe, real_setup

    original = lifecycle.setup

    def configured(path):
        service, body = original(path)
        service.claim_source_policy = wrapper.claim_source_policy()
        return service, body

    monkeypatch.setattr(lifecycle, "setup", configured)
    render_gap(monkeypatch)
    probe = FakeProbe(json.dumps({"claims": [QUOTE]}))
    service, run_id, runner, now, probe = real_setup(tmp_path, monkeypatch, limit=1, probe=probe)

    from proofops.adapters.local.run_artifacts import load_run_graph

    real = load_run_graph(
        service.store, service.uploads, runner.parser, tenant_id=TENANT, run_id=run_id
    )
    batch = candidate("span-r19", [("P", "paragraph", NATIVE, (70, 710, 300, 740), ())])
    batch = replace(
        batch,
        document_version_id=real.document_version_id,
        parse_manifest_id=real.parse_manifest_id,
        source_sha256=real.source_sha256,
        blocks=tuple(
            replace(
                block,
                source=replace(
                    block.source,
                    document_version_id=real.document_version_id,
                    parse_manifest_id=real.parse_manifest_id,
                ),
            )
            for block in batch.blocks
        ),
    )
    graph_input = fuse_candidates((batch,), tenant_id=TENANT)
    for module in ("proofops_worker.extract_runner", "proofops.adapters.local.claim_store"):
        monkeypatch.setattr(module + ".load_run_graph", lambda *a, **k: graph_input)

    run = service.store.jobs.get_run(TENANT, run_id)
    parse_before = service.store.jobs.read_checkpoint(JobMessage(**run["parse_job"]))
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"

    envelope, discovery, graph = runner.claims.load_evidence(TENANT, run_id)
    receipt = envelope["claim_source_attestation"]
    assert envelope["schema"] == "local_extract_checkpoint_v2"
    # This wrapper's OWN schema, with BOTH inner layers preserved, not relabelled.
    assert receipt["schema"] == wrapper.SCHEMA
    assert receipt["policy"] == wrapper.claim_source_policy()
    assert receipt["graph_sha256"] == envelope["graph_sha256"]
    assert "baseline_records" in receipt and "base_records" in receipt
    assert "render_retries" in receipt and "bullet_alignments" in receipt
    # R15's recovery is PRESERVED: its verdicts are carried, not overridden.
    assert all(record["status"] == "unresolved" for record in receipt["base_records"])
    assert any(record["status"] == "verified" for record in receipt["baseline_records"])
    assert discovery.claims and any(c.source_quality == "verified" for c in discovery.claims)
    # Per-record scope only: no block quality is promoted wholesale.
    assert all(block.quality != "verified" for block in graph.blocks)
    reopened = LocalClaimStore(
        LocalSQLiteRunStore(service.store.path), service.uploads, runner.parser
    )
    assert reopened.load(TENANT, run_id) == discovery
    assert service.store.jobs.read_checkpoint(JobMessage(**run["parse_job"])) == parse_before
    assert len(probe.calls) == 1


def test_the_wrapped_r15_receipt_is_byte_identical_and_still_replays_on_its_own(monkeypatch):
    """Old receipts stay readable: the inner layers are carried unmodified and
    an R15 receipt for the same inputs still replays under the R15 reader."""
    render_gap(monkeypatch)
    source, graph, ref = inputs()
    discovery = discovery_for(graph, (ref,))
    refs = base.discovery_refs(discovery)

    standalone = r15.attest_claim_spans(graph, source, refs, tenant_id=TENANT)
    combined = wrapper.attest_claim_spans(graph, source, refs, tenant_id=TENANT)
    assert combined["baseline_records"] == standalone["records"]
    assert combined["base_records"] == standalone["base_records"]
    assert combined["baseline_attestation_sha256"] == standalone["artifact_sha256"]
    assert combined["render_retries"] == standalone["render_retries"]
    # The R15 receipt is still independently replayable and unchanged.
    replayed, scoped = r15.replay_claim_spans(
        standalone, graph, source, discovery, tenant_id=TENANT
    )
    assert [c.source_quality for c in replayed.claims] == ["verified"]
    assert all(block.quality != "verified" for block in scoped.blocks)


def test_replay_refuses_an_altered_receipt_or_a_different_ref_set(monkeypatch):
    render_gap(monkeypatch)
    source, graph, ref = inputs()
    discovery = discovery_for(graph, (ref,))
    receipt = wrapper.attest_claim_spans(
        graph, source, base.discovery_refs(discovery), tenant_id=TENANT
    )
    replayed, scoped = wrapper.replay_claim_spans(
        receipt, graph, source, discovery, tenant_id=TENANT
    )
    assert [c.source_quality for c in replayed.claims] == ["verified"]
    assert all(block.quality != "verified" for block in scoped.blocks)

    for altered in (
        dict(receipt, records=[dict(receipt["records"][0], status="verified", reason="forced")]),
        dict(receipt, baseline_records=[dict(receipt["baseline_records"][0], status="unresolved")]),
        dict(receipt, bullet_alignments={"x": {"status": "aligned"}}),
        dict(receipt, schema=r15.SCHEMA),
        {k: v for k, v in receipt.items() if k != "base_records"},
    ):
        with pytest.raises(ValueError):
            wrapper.replay_claim_spans(altered, graph, source, discovery, tenant_id=TENANT)

    other = replace(ref, char_start=0, char_end=6, quote="Page 1")
    with pytest.raises(ValueError, match="RECEIPT_MISMATCH"):
        wrapper.replay_claim_spans(
            receipt, graph, source, discovery_for(graph, (other,)), tenant_id=TENANT
        )
