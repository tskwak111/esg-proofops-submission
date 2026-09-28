"""R24: the opt-in rendered-side-only typography wrapper.

Scope, deliberately narrow. The end-to-end demonstration on real original
bytes (8 refs, 2 reports, both rendered paths, real OCR refusals, the boundary
case) is the separate fresh proof under
``tests/fixtures/pipeline/typography-wrapper/``; these checks cover what a
proof run cannot: the fold table's shape, the two read paths, the gate's
asymmetry, the receipt pins, strict replay refusal, and that the three older
policies and the byte-pinned reader files are untouched.

No model, network or AWS call. The rendered reader is the existing
monkeypatched fixture pattern already used by the R15/R19 tests.
"""

import json
from dataclasses import replace
from hashlib import sha256
from io import BytesIO
from pathlib import Path

import pytest
from proofops.adapters.local import claim_source_policies as policies
from proofops.adapters.local import claim_source_verification as base
from proofops.adapters.local import claim_span_bullet_alignment as r19
from proofops.adapters.local import claim_span_render_resolution as r15
from proofops.adapters.local import claim_span_typography as wrapper
from proofops.adapters.local import native_paragraph_typography as fold_helper
from proofops.adapters.local import selected_cell_table_verification as cell_reader
from proofops.application.evidence import citations
from proofops.application.evidence.citations import _normalized
from proofops.application.ingest.graph_fusion import fuse_candidates

from tests.acceptance.test_parsing import TENANT, candidate
from tests.integration.test_batch_attestation_cache import discovery_for

LOCAL = Path(__file__).resolve().parents[2] / "packages/proofops/adapters/local"

# The document's own ink: U+00B7. The rendered reader returns U+2022 for it.
DOT, BULLET = "\u00b7", "\u2022"
NATIVE_TEXT = f"emissions 1234 tCO2e in 2025 {DOT}"
RENDERED_TEXT = NATIVE_TEXT.replace(DOT, BULLET)
QUOTE = f"1234 tCO2e in 2025 {DOT}"
BOX = (70, 710, 300, 741)
# ``\267`` is U+00B7 under WinAnsiEncoding. It is the LAST glyph on the line
# because the non-embedded base-14 metrics for periodcentered differ between
# pdfminer and pdfium, which desynchronizes the glyph matcher for any glyph
# after it. Real reports embed their fonts; the R24 original-byte proof
# exercises the mid-line case on two of them.
LINE = r"emissions 1234 tCO2e in 2025 \267"


def winansi_pdf(text):
    """One page, Helvetica/WinAnsiEncoding so ``\\267`` really decodes to U+00B7.

    ``tests/integration/test_claim_source_verification.geometry_pdf`` leaves the
    base-14 default encoding, where that byte is U+2022; the difference under
    test needs the two codepoints to be distinguishable, so the encoding is
    named explicitly here and nothing else changes.
    """
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    font = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
                NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
            }
        )
    )
    page = writer.add_blank_page(width=600, height=800)
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def dot_inputs(quote=QUOTE, line=LINE, text=NATIVE_TEXT):
    """A real pinned paragraph whose ink carries U+00B7, plus one ref."""
    source = winansi_pdf(line)
    batch = replace(
        candidate("span-r24", [("P", "paragraph", text, BOX, ())]),
        source_sha256=sha256(source).hexdigest(),
    )
    graph = fuse_candidates((batch,), tenant_id=TENANT)
    start = text.index(quote)
    ref = replace(
        graph.blocks[0].source_ref(),
        char_start=start,
        char_end=start + len(quote),
        quote=quote,
    )
    return source, graph, ref


def render_as_bullet(monkeypatch, text=RENDERED_TEXT):
    """Both wrapped rendered readers spell the document's U+00B7 as U+2022."""
    monkeypatch.setattr(base, "_rendered_text", lambda *a, **k: dict(status="read", text=text))
    monkeypatch.setattr(
        cell_reader, "_rendered_cell", lambda page, box: dict(status="read", text=text, scale=11)
    )


# --- the fold table ---------------------------------------------------------


def test_the_fold_table_is_the_shipped_one_plus_exactly_one_dot_entry():
    assert wrapper._RENDERED_FOLD == {**fold_helper._QUOTE_FOLD, ord(DOT): BULLET}
    # Pinned as the five admitted entries, literally; not a punctuation class.
    assert wrapper._ADMITTED_ENTRIES == (
        ("U+00B7", "U+2022"),
        ("U+2018", "U+0027"),
        ("U+2019", "U+0027"),
        ("U+201C", "U+0022"),
        ("U+201D", "U+0022"),
    )
    assert wrapper._entries(wrapper._RENDERED_FOLD) == wrapper._ADMITTED_ENTRIES
    # The shipped helper is reused as CODE, so the quote half cannot drift.
    assert wrapper._quote_folded is fold_helper._quote_folded
    assert wrapper._rendered_folded(f"a{DOT}b \u201cq\u201d") == 'a\u2022b "q"'
    # Nothing outside those five entries is folded.
    for text in (
        "1 234",
        "0.5",
        "1,000",
        "Aa",
        "a-b",
        "a\u2013b",
        "H\u2082O",
        "5\u2032",
        "5\u00b0",
        "a\u2192b",
        "\u00ab q \u00bb",
    ):
        assert wrapper._rendered_folded(text) == _normalized(text), text


# --- the gate's asymmetry: the native side is never folded -------------------


def test_the_native_gate_is_unfolded_and_is_what_refuses_a_forged_dot():
    """The R24 proof's guard probe, as a unit check: flipping the dot the
    document does NOT contain is accepted by the rendered gate alone and
    refused by this shape, because the native gate never folds."""
    forged = QUOTE.replace(DOT, BULLET)
    gate = wrapper._typography_gate(NATIVE_TEXT, RENDERED_TEXT, forged)
    assert gate["rendered_gate_folded"] is True  # rendered side alone would pass
    assert gate["native_gate_unfolded"] is False  # the unfolded native gate refuses
    assert gate["native_side_folded"] is False
    assert gate["accepted"] is False
    # Folding the native side too (the rejected V3 shape) would false-accept it.
    assert base._unique_quote(
        wrapper._rendered_folded(NATIVE_TEXT), wrapper._rendered_folded(forged)
    )
    # The honest quote is accepted, and only via the rendered fold.
    honest = wrapper._typography_gate(NATIVE_TEXT, RENDERED_TEXT, QUOTE)
    assert honest["accepted"] and honest["native_gate_unfolded"]
    assert honest["applied_fold_entries"] == ["U+00B7->U+2022"]
    assert not base._unique_quote(honest["rendered_normalized"], honest["quote_normalized"])


@pytest.mark.parametrize(
    "rendered",
    [
        f"emissions 1235 tCO2e in 2025 {BULLET}",  # changed number
        f"emissions 1234 tC02e in 2025 {BULLET}",  # real letter/digit OCR error
        f"emissions 1234 tCO2e in2025 {BULLET}",  # deleted space
        f"emissions 1234 tCO2e in 2025 {BULLET} 1234 tCO2e in 2025 {BULLET}",  # ambiguous
        "",
    ],
)
def test_a_real_rendered_error_is_still_refused_after_the_fold(rendered):
    gate = wrapper._typography_gate(NATIVE_TEXT, rendered, QUOTE)
    assert gate["native_gate_unfolded"] is True
    assert gate["rendered_gate_folded"] is False
    assert gate["accepted"] is False


# --- the two read paths, and the boundary case that has neither -------------


def base_shaped_receipt(**overrides):
    receipt = dict(
        records=[
            dict(
                ref=dict(source_id="s1", quote=QUOTE),
                status="unresolved",
                reason="text_mismatch",
            )
        ],
        readings={
            "s1": dict(
                reason="rendered_quote_unresolved",
                rendered=dict(status="read", text=RENDERED_TEXT),
            )
        },
        bullet_alignments={},
    )
    receipt.update(overrides)
    return receipt


class FakeGraph:
    def __init__(self, raw_text):
        self.blocks = (type("B", (), dict(source_id="s1", raw_text=raw_text))(),)


def test_the_accepted_bullet_alignment_rendered_read_is_preferred_over_the_base_read():
    aligned = dict(
        status="aligned",
        native_normalized=NATIVE_TEXT,
        rendered=dict(status="read", text=RENDERED_TEXT),
    )
    native, rendered, path = wrapper.typography_read(
        base_shaped_receipt(bullet_alignments={"s1": aligned}), FakeGraph("other"), "s1"
    )
    assert path == "bullet_alignment_rendered_read"
    assert native == NATIVE_TEXT and rendered["text"] == RENDERED_TEXT
    assert wrapper._typography_gate(native, rendered["text"], QUOTE)["accepted"]


def test_the_base_rendered_read_is_used_when_there_is_no_accepted_alignment():
    native, rendered, path = wrapper.typography_read(
        base_shaped_receipt(), FakeGraph(NATIVE_TEXT), "s1"
    )
    assert path == "base_rendered_read"
    assert native == _normalized(NATIVE_TEXT) and rendered["text"] == RENDERED_TEXT


def test_a_refused_token_boundary_has_no_rendered_read_and_stays_unassessed():
    """The bullet wrapper's boundary refusal is not relaxed: with no rendered
    string there is nothing to compare, so the record is neither a match nor a
    mismatch. This is the proof's B1 (a quote carrying 2030/60%) in shape."""
    receipt = base_shaped_receipt(
        readings={"s1": dict(reason="text_mismatch")},
        bullet_alignments={"s1": dict(status="refused", reason="boundary_not_admissible")},
    )
    assert wrapper.typography_read(receipt, FakeGraph(NATIVE_TEXT), "s1") == (
        None,
        None,
        "no_rendered_read",
    )
    # The R15 retry read is deliberately not a third path.
    retry = base_shaped_receipt(
        readings={"s1": dict(reason="text_mismatch")},
        bullet_alignments={},
        render_retries={"s1": dict(rendered=dict(status="read", text=RENDERED_TEXT))},
    )
    assert wrapper.typography_read(retry, FakeGraph(NATIVE_TEXT), "s1")[2] == "no_rendered_read"


def test_only_read_unresolved_reasons_are_ever_eligible():
    for reason in (
        "source_invalid",
        "clipped_or_rotated_words",
        "glyph_geometry_unresolved",
        "interactive_visibility_requires_review",
        "geometry_unsupported",
        "rendered_reader_unavailable",
    ):
        receipt = base_shaped_receipt()
        receipt["records"][0]["reason"] = reason
        assert wrapper._eligible_records(receipt) == []
    verified = base_shaped_receipt()
    verified["records"][0]["status"] = "verified"
    assert wrapper._eligible_records(verified) == []
    assert wrapper._eligible_records(base_shaped_receipt()) == [0]


# --- policy, dispatch and the real run-creation gate ------------------------


def test_the_policy_is_distinct_and_pins_the_wrapper_baseline_and_fold_helper():
    policy = wrapper.claim_source_policy()
    assert policy["schema"] == "claim_span_typography_policy_v1" == wrapper.POLICY_SCHEMA
    assert policy["mode"] == wrapper.MODE
    assert policy["base"] == r19.claim_source_policy()
    assert policy["base"]["base"] == r15.claim_source_policy()
    assert policy["base"]["base"]["base"] == base.claim_source_policy()
    assert policy["wrapper_sha256"] == sha256(Path(wrapper.__file__).read_bytes()).hexdigest()
    assert (
        policy["fold_helper_sha256"] == sha256(Path(fold_helper.__file__).read_bytes()).hexdigest()
    )
    for key in ("base_sha256", "rendered_fold_sha256"):
        assert len(policy[key]) == 64
    assert wrapper.SCHEMA == "claim_span_typography_attestation_v1"
    assert wrapper.SCHEMA not in {r19.SCHEMA, r15.SCHEMA, "claim_source_attestation_v1"}


def test_dispatch_is_additive_and_every_older_policy_still_routes_as_before():
    assert policies.claim_source_reader(wrapper.claim_source_policy()) is wrapper
    assert policies.claim_source_reader(r19.claim_source_policy()) is r19
    assert policies.claim_source_reader(r15.claim_source_policy()) is r15
    assert policies.claim_source_reader(base.claim_source_policy()) is base
    assert policies.publication_reader(wrapper.claim_source_policy()) is wrapper
    assert policies.publication_reader(r19.claim_source_policy()) is r19
    assert wrapper.POLICY_SCHEMA in policies.CLAIM_SOURCE_POLICY_SCHEMAS
    altered = dict(wrapper.claim_source_policy(), wrapper_sha256="0" * 64)
    with pytest.raises(ValueError, match="CLAIM_SOURCE_POLICY_UNKNOWN"):
        policies.claim_source_reader(altered)
    with pytest.raises(ValueError, match="CLAIM_SOURCE_POLICY_MISMATCH"):
        policies.publication_reader(altered)


def test_the_real_run_service_admits_the_new_policy_and_still_refuses_an_unknown_one(tmp_path):
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
            extraction_mode="upstage_probe",
            claim_source_policy=policy,
        )

    assert build(wrapper.claim_source_policy()).claim_source_policy["schema"] == (
        wrapper.POLICY_SCHEMA
    )
    assert build(r19.claim_source_policy()) is not None
    with pytest.raises(ValueError, match="invalid claim source policy"):
        build(dict(wrapper.claim_source_policy(), schema="claim_span_typography_policy_v2"))


def test_the_pilot_cli_opt_in_is_new_run_only_and_requires_the_wrapped_flag():
    from types import SimpleNamespace

    from evaluation import local_upstage_pilot as pilot

    def policy(**flags):
        return pilot.claim_source_policy_for(SimpleNamespace(**flags))

    assert (
        policy(claim_span_typography=True, claim_span_bullet_spacing=True)
        == wrapper.claim_source_policy()
    )
    # Every older selection is unchanged, including the plain default.
    assert policy(claim_span_typography=False, claim_span_bullet_spacing=True) == (
        r19.claim_source_policy()
    )
    assert policy(claim_span_render_resolution=True) == r15.claim_source_policy()
    assert policy() == base.claim_source_policy()

    # New-run only: the chain is required, resume cannot add it, the manifest
    # records it and an existing state directory refuses a changed value.
    source = Path(pilot.__file__).read_text()
    for needle in (
        '"--claim-span-typography",',
        '"--claim-span-typography requires --claim-span-bullet-spacing"',
        '"--resume cannot add claim-span typography; create a new run"',
        'args.claim_span_typography = bool(saved.get("claim_span_typography", False))',
        'manifest["claim_span_typography"] = True',
        'manifest.get("claim_span_typography", False) != args.claim_span_typography',
    ):
        assert needle in source, needle


# --- end to end on real bytes: creation, receipt pins, strict replay --------


def test_the_unresolved_dot_record_is_recovered_and_every_layer_is_carried(monkeypatch):
    render_as_bullet(monkeypatch)
    source, graph, ref = dot_inputs()
    refs = (ref,)

    baseline = r19.attest_claim_spans(graph, source, refs, tenant_id=TENANT)
    result = wrapper.attest_claim_spans(graph, source, refs, tenant_id=TENANT)

    # Failing before: all three wrapped layers leave it unresolved on the
    # rendered string, and the boundary/alignment layer never applies.
    assert [r["status"] for r in baseline["base_records"]] == ["unresolved"]
    assert baseline["base_records"][0]["reason"] == "rendered_quote_unresolved"
    assert [r["status"] for r in baseline["baseline_records"]] == ["unresolved"]
    assert [r["status"] for r in baseline["records"]] == ["unresolved"]
    assert baseline["bullet_alignments"] == {}
    # Recovered here, and only here.
    assert result["schema"] == wrapper.SCHEMA
    assert [r["status"] for r in result["records"]] == ["verified"]
    assert result["records"][0]["reason"] is None
    # Every inner layer travels through byte-for-byte.
    assert result["bullet_alignment_records"] == baseline["records"]
    assert result["bullet_alignment_attestation_sha256"] == baseline["artifact_sha256"]
    for key in (
        "baseline_records",
        "base_records",
        "readings",
        "render_retries",
        "bullet_alignments",
        "baseline_attestation_sha256",
        "base_attestation_sha256",
    ):
        assert result[key] == baseline[key], key
    # The ref is immutable: quote, offsets and hash are untouched.
    assert result["records"][0]["ref"] == baseline["records"][0]["ref"]
    assert result["records"][0]["ref"]["quote"] == QUOTE

    # The receipt pins the precise reads and the recovery reason.
    read = result["typography_reads"]["0"]
    assert read["path"] == "base_rendered_read"
    assert read["baseline_reason"] == "rendered_quote_unresolved"
    assert read["reason"] == wrapper.RECOVERY_REASON
    assert (
        read["native_normalized"] == _normalized(NATIVE_TEXT) and DOT in read["native_normalized"]
    )
    assert read["rendered_normalized"] == RENDERED_TEXT
    assert read["rendered_folded"] == RENDERED_TEXT and read["quote_folded"] == QUOTE.replace(
        DOT, BULLET
    )
    assert read["applied_fold_entries"] == ["U+00B7->U+2022"]
    assert read["native_gate_unfolded"] is True and read["native_side_folded"] is False
    assert read["rendered_read_sha256"] is not None


def test_a_real_rendered_error_on_real_bytes_stays_unresolved(monkeypatch):
    for rendered in (
        f"emissions 1235 tCO2e in 2025 {BULLET}",
        f"emissions 1234 tC02e in 2025 {BULLET}",
    ):
        with pytest.MonkeyPatch.context() as patch:
            for target, value in ((base, "_rendered_text"), (cell_reader, "_rendered_cell")):
                patch.setattr(target, value, lambda *a, **k: dict(status="read", text=rendered))
            source, graph, ref = dot_inputs()
            result = wrapper.attest_claim_spans(graph, source, (ref,), tenant_id=TENANT)
            assert [r["status"] for r in result["records"]] == ["unresolved"]
            read = result["typography_reads"]["0"]
            assert read["reason"] == "rendered_fold_still_differs"
            assert read["native_gate_unfolded"] is True


def test_replay_recomputes_the_whole_receipt_and_refuses_every_tamper(monkeypatch):
    render_as_bullet(monkeypatch)
    source, graph, ref = dot_inputs()
    discovery = discovery_for(graph, (ref,))
    receipt = wrapper.attest_claim_spans(
        graph, source, base.discovery_refs(discovery), tenant_id=TENANT
    )
    replayed, scoped = wrapper.replay_claim_spans(
        receipt, graph, source, discovery, tenant_id=TENANT
    )
    assert [c.source_quality for c in replayed.claims] == ["verified"]
    # Per-record scope only: no block quality is promoted wholesale.
    assert all(block.quality != "verified" for block in scoped.blocks)

    reads = receipt["typography_reads"]
    for altered in (
        dict(receipt, records=[dict(receipt["records"][0], reason="forced")]),
        dict(receipt, bullet_alignment_records=[dict(receipt["records"][0], status="verified")]),
        dict(receipt, base_records=[dict(receipt["base_records"][0], status="verified")]),
        dict(receipt, typography_reads={"0": dict(reads["0"], native_side_folded=True)}),
        dict(receipt, typography_reads={"0": dict(reads["0"], rendered_read_sha256="0" * 64)}),
        dict(receipt, typography_reads={}),
        dict(receipt, readings={}),
        dict(receipt, policy=dict(receipt["policy"], rendered_fold_sha256="0" * 64)),
        dict(receipt, policy=r19.claim_source_policy()),
        dict(receipt, schema=r19.SCHEMA),
        {k: v for k, v in receipt.items() if k != "base_records"},
    ):
        with pytest.raises(ValueError, match="CLAIM_SPAN_TYPOGRAPHY_"):
            wrapper.replay_claim_spans(altered, graph, source, discovery, tenant_id=TENANT)
    # Changed source bytes and a different ref set are refused too.
    with pytest.raises(ValueError):
        wrapper.replay_claim_spans(
            receipt,
            graph,
            winansi_pdf(r"emissions 1234 tCO2e in 2026 \267"),
            discovery,
            tenant_id=TENANT,
        )
    other = replace(ref, char_start=0, char_end=9, quote="emissions")
    with pytest.raises(ValueError, match="RECEIPT_MISMATCH"):
        wrapper.replay_claim_spans(
            receipt, graph, source, discovery_for(graph, (other,)), tenant_id=TENANT
        )


def test_the_older_policies_and_their_receipts_are_unchanged(monkeypatch):
    """Old readers and old run policies stay byte-identical.

    The reader file digests are the ones the R24 original-byte proof and the
    ACTIVE bullet-policy run pinned; the wrapper must not have touched any of
    them. ``native_replay_cache`` independently pins the bullet wrapper's own
    digest, so this also guards that pin.
    """
    from proofops.adapters.local import native_replay_cache

    pinned = json.loads(
        (
            Path(__file__).resolve().parents[2] / "tests/fixtures/pipeline/reader-hashes.json"
        ).read_text()
    )
    for name, digest in pinned.items():
        path = Path(citations.__file__) if name == "citations.py" else LOCAL / name
        assert sha256(path.read_bytes()).hexdigest() == digest, name
    assert native_replay_cache._BULLET_ALIGNMENT_WRAPPER == pinned["claim_span_bullet_alignment.py"]

    # No older policy mentions this wrapper, and none of them changed shape.
    for policy in (
        base.claim_source_policy(),
        r15.claim_source_policy(),
        r19.claim_source_policy(),
    ):
        assert wrapper.POLICY_SCHEMA not in json.dumps(policy)

    # The wrapped receipt for the same inputs is identical whether or not this
    # wrapper runs, and it still replays on its own.
    render_as_bullet(monkeypatch)
    source, graph, ref = dot_inputs()
    discovery = discovery_for(graph, (ref,))
    refs = base.discovery_refs(discovery)
    standalone = r19.attest_claim_spans(graph, source, refs, tenant_id=TENANT)
    combined = wrapper.attest_claim_spans(graph, source, refs, tenant_id=TENANT)
    assert combined["bullet_alignment_records"] == standalone["records"]
    replayed, scoped = r19.replay_claim_spans(
        standalone, graph, source, discovery, tenant_id=TENANT
    )
    assert [c.source_quality for c in replayed.claims] == ["unverified"]
    assert all(block.quality != "verified" for block in scoped.blocks)


def test_the_wrapper_receipt_is_never_composed_through_the_per_record_cache(monkeypatch):
    """The whole-receipt fields would be inconsistent with a fresh recompute, so
    the batch path must recompute this wrapper in full and the replay cache must
    refuse to project it."""
    from proofops.adapters.local import native_replay_cache

    render_as_bullet(monkeypatch)
    source, graph, ref = dot_inputs()
    discovery = discovery_for(graph, (ref,))
    refs = base.discovery_refs(discovery)
    policy = wrapper.claim_source_policy()
    receipt = policies.attest_claims(
        reader=wrapper, graph=graph, source=source, refs=refs, tenant_id=TENANT, cache=True
    )
    assert receipt == wrapper.attest_claim_spans(graph, source, refs, tenant_id=TENANT)
    assert not native_replay_cache._wrapper_projection_pinned(wrapper, policy, receipt)
    assert (
        native_replay_cache._reuse_wrapper_replay(
            wrapper, policy, receipt, graph, source, discovery, TENANT
        )
        is None
    )
