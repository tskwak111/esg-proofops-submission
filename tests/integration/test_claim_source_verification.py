from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest
from proofops.application.ingest.graph_fusion import fuse_candidates

from tests.acceptance.test_parsing import TENANT, candidate, pdf


def inputs():
    source = pdf()
    batch = replace(
        candidate(
            "span", [("P", "paragraph", "Page 1 emissions 1234 tCO2e", (70, 710, 300, 740), ())]
        ),
        source_sha256=sha256(source).hexdigest(),
    )
    graph = fuse_candidates((batch,), tenant_id=TENANT)
    whole = graph.blocks[0].source_ref()
    quote = "emissions 1234 tCO2e"
    ref = replace(whole, char_start=7, char_end=7 + len(quote), quote=quote)
    return source, graph, ref


def test_claim_span_survives_unrelated_ocr_error_but_not_changed_value(monkeypatch):
    from proofops.adapters.local import claim_source_verification as verifier

    source, graph, ref = inputs()
    monkeypatch.setattr(
        verifier,
        "_rendered_text",
        lambda *a, **k: dict(status="read", text="Page I emissions 1234 tCO2e"),
    )
    receipt = verifier.attest_claim_spans(graph, source, (ref,), tenant_id=TENANT)
    assert receipt["records"][0]["status"] == "verified"
    assert graph.blocks[0].quality == "unverified"
    for text in ["Page 1 emissions 1235 tCO2e", "", "emissions 1234 tCO2e emissions 1234 tCO2e"]:
        monkeypatch.setattr(
            verifier, "_rendered_text", lambda *a, **k: dict(status="read", text=text)
        )
        assert (
            verifier.attest_claim_spans(graph, source, (ref,), tenant_id=TENANT)["records"][0][
                "status"
            ]
            == "unresolved"
        )
    with pytest.raises(ValueError):
        verifier.attest_claim_spans(graph, source + b"x", (ref,), tenant_id=TENANT)


def test_ocr_dropping_the_inter_word_space_stays_unresolved_not_verified(monkeypatch):
    """R13 real observation (KB run 9e96e8d2, source 33896b5d, quote '9.9억 원'):
    two adjacent native PDF word tokens with a real physical x-gap (measured
    ~1.26pt) are both individually glyph-ink-resolved and jointly equal the
    block's own raw text natively, but Apple Vision's rendered OCR of the
    combined crop drops the inter-word space, reading '9.9억원' instead of
    '9.9억 원'. This must stay ``rendered_quote_unresolved`` / unresolved: a
    caller is never allowed to fold that difference away, because doing so
    would be exactly the generic whitespace-normalization-to-manufacture-a-
    match this reader's docstring forbids, and because a missing space is a
    token-boundary loss (a real risk for a value+unit pair), not a visual
    glyph variant like the existing curly-quote typography wrapper folds.
    """
    from proofops.adapters.local import claim_source_verification as verifier

    source, graph, ref = inputs()
    # Same fixture as the sibling OCR-typo test above, but drop only the space
    # between "emissions" and "1234" the way Vision dropped the space between
    # "9.9억" and "원" in the real KB crop; digits/unit stay intact.
    monkeypatch.setattr(
        verifier,
        "_rendered_text",
        lambda *a, **k: dict(status="read", text="Page I emissions1234 tCO2e"),
    )
    receipt = verifier.attest_claim_spans(graph, source, (ref,), tenant_id=TENANT)
    record = receipt["records"][0]
    assert record["status"] == "unresolved"
    assert record["reason"] == "rendered_quote_unresolved"
    assert graph.blocks[0].quality == "unverified"


def test_navigation_form_guard_is_spatial_and_keeps_input_forms_blocked():
    from pdfminer.psparser import LIT
    from proofops.adapters.local.claim_source_verification import (
        _appearance_overlaps,
        _pushbuttons_only,
    )

    button = {"FT": LIT("Btn"), "Ff": 65536, "Subtype": LIT("Widget")}
    assert _pushbuttons_only({"Fields": [button]})
    assert not _pushbuttons_only({"Fields": [dict(button, FT=LIT("Tx"))]})
    assert not _pushbuttons_only({"Fields": [], "XFA": 1})
    annotation = {"data": {"AP": True}, "x0": 50, "top": 50, "x1": 60, "bottom": 60}
    assert not _appearance_overlaps(SimpleNamespace(annots=[annotation]), (10, 10, 30, 30))
    assert _appearance_overlaps(SimpleNamespace(annots=[annotation]), (45, 45, 65, 65))
    assert _appearance_overlaps(SimpleNamespace(annots=[{"data": {"AP": True}}]), (10, 10, 30, 30))


def test_real_worker_publishes_v2_and_replays_verified_claim_without_promoting_paragraph(
    tmp_path, monkeypatch
):
    import json

    from proofops.adapters.local import claim_source_verification as verifier
    from proofops.adapters.local.claim_store import LocalClaimStore
    from proofops.adapters.local.run_store import LocalSQLiteRunStore
    from proofops.application.ports.jobs import JobMessage

    from tests.integration import test_run_lifecycle as lifecycle
    from tests.integration.test_real_extract_runner import FakeProbe, real_setup

    original = lifecycle.setup

    def configured(path):
        service, body = original(path)
        service.claim_source_policy = verifier.claim_source_policy()
        return service, body

    monkeypatch.setattr(lifecycle, "setup", configured)
    monkeypatch.setattr(
        verifier,
        "_rendered_text",
        lambda *a, **k: dict(status="read", text="Page I emissions 1234 tCO2e"),
    )
    quote = "emissions 1234 tCO2e"
    probe = FakeProbe(json.dumps({"claims": [quote]}))
    service, run_id, runner, now, probe = real_setup(tmp_path, monkeypatch, limit=1, probe=probe)
    # OD omits the repeated page header in this fixture. Add one explicit
    # paragraph parser candidate over its real PDF text to exercise this gate.
    from proofops.adapters.local.run_artifacts import load_run_graph

    base = load_run_graph(
        service.store, service.uploads, runner.parser, tenant_id=TENANT, run_id=run_id
    )
    batch = candidate(
        "span-integration",
        [("P", "paragraph", "Page 1 emissions 1234 tCO2e", (70, 710, 300, 740), ())],
    )
    batch = replace(
        batch,
        document_version_id=base.document_version_id,
        parse_manifest_id=base.parse_manifest_id,
        source_sha256=base.source_sha256,
        blocks=tuple(
            replace(
                b,
                source=replace(
                    b.source,
                    document_version_id=base.document_version_id,
                    parse_manifest_id=base.parse_manifest_id,
                ),
            )
            for b in batch.blocks
        ),
    )
    graph_input = fuse_candidates((batch,), tenant_id=TENANT)
    for module in ("proofops_worker.extract_runner", "proofops.adapters.local.claim_store"):
        monkeypatch.setattr(module + ".load_run_graph", lambda *a, **k: graph_input)
    run = service.store.jobs.get_run(TENANT, run_id)
    parse_before = service.store.jobs.read_checkpoint(JobMessage(**run["parse_job"]))
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
    envelope, discovery, graph = runner.claims.load_evidence(TENANT, run_id)
    assert envelope["schema"] == "local_extract_checkpoint_v2"
    assert discovery.claims and any(c.source_quality == "verified" for c in discovery.claims)
    assert all(b.quality != "verified" for b in graph.blocks)
    reopened = LocalClaimStore(
        LocalSQLiteRunStore(service.store.path), service.uploads, runner.parser
    )
    assert reopened.load(TENANT, run_id) == discovery
    assert service.store.jobs.read_checkpoint(JobMessage(**run["parse_job"])) == parse_before
    assert len(probe.calls) == 1


def test_quote_matching_does_not_accept_numeric_prefix_or_overlapping_occurrences():
    from proofops.adapters.local.claim_source_verification import _unique_quote

    assert not _unique_quote("20250", "2025")
    assert not _unique_quote("1000 tonnes", "100")
    assert not _unique_quote("aaaa", "aaa")
    assert _unique_quote("2025년 100톤.", "2025년 100톤")


def geometry_pdf(lines):
    """One page with explicit text baselines in bottom-left points (Helvetica 12pt)."""
    from io import BytesIO

    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    font = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
    )
    page = writer.add_blank_page(width=600, height=800)
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    stream = DecodedStreamObject()
    stream.set_data(
        "\n".join(f"BT /F1 12 Tf {x} {y} Td ({text}) Tj ET" for x, y, text in lines).encode()
    )
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def geometry_case(lines, text, bbox):
    """Pin one paragraph candidate over a real PDF page; bbox is bottom-left points."""
    source = geometry_pdf(lines)
    batch = replace(
        candidate("span", [("P", "paragraph", text, bbox, ())]),
        source_sha256=sha256(source).hexdigest(),
    )
    return source, fuse_candidates((batch,), tenant_id=TENANT)


def quote_ref(graph, text, quote):
    start = text.index(quote)
    return replace(
        graph.blocks[0].source_ref(),
        char_start=start,
        char_end=start + len(quote),
        quote=quote,
    )


# Baselines 740 and 720 put the upper line's glyph ink at y 51.26-60.14 and the
# paragraph's at 71.25-80.14 in top-left points. A box whose top edge is 59
# therefore holds the whole paragraph while the line above crosses it.
HEADING = (72, 740, "ENV HEADING 2030")
PARAGRAPH_TEXT = "emissions 1234 tCO2e in 2025"
PARAGRAPH = (72, 720, PARAGRAPH_TEXT)
CROSSED_BOX = (70, 710, 300, 741)


def test_neighbor_line_crossing_the_paragraph_box_is_recovered_without_widening_tolerance(
    monkeypatch,
):
    """The real Lotte blocker: an adjacent heading's ink reaches into the box.

    The rendered reader is stubbed here exactly as the tests above do; the real
    Apple Vision confirmation over this path is exercised by the offline Lotte
    probe in outputs/pipeline-recovery-20260920/source-span/.
    """
    from proofops.adapters.local import claim_source_verification as verifier

    source, graph = geometry_case([HEADING, PARAGRAPH], PARAGRAPH_TEXT, CROSSED_BOX)
    quote = "emissions 1234 tCO2e"
    ref = quote_ref(graph, PARAGRAPH_TEXT, quote)
    monkeypatch.setattr(
        verifier, "_rendered_text", lambda *a, **k: dict(status="read", text=PARAGRAPH_TEXT)
    )
    receipt = verifier.attest_claim_spans(graph, source, (ref,), tenant_id=TENANT)
    record = receipt["records"][0]
    assert record["status"] == "verified"
    reading = receipt["readings"][ref.source_id]
    # Only the paragraph's own glyphs become claim text; the crossing heading
    # words are recorded as excluded neighbours, never read as evidence.
    assert [w["text"] for w in reading["words"]] == PARAGRAPH_TEXT.split()
    assert [w["text"] for w in reading["neighbor_words"]] == ["ENV", "HEADING", "2030"]
    assert graph.blocks[0].quality == "unverified"
    with pytest.raises(ValueError):
        verifier.attest_claim_spans(
            graph, source, (ref,), tenant_id="22222222-2222-4222-8222-222222222222"
        )


@pytest.mark.parametrize(
    "lines,text,bbox,quote,reason",
    [
        # The paragraph's own line continues past the right edge: the crop cuts
        # real claim text, so the quote stays unverified.
        (
            [HEADING, PARAGRAPH, (330, 720, "and 5678 tCO2e more")],
            PARAGRAPH_TEXT,
            (70, 710, 340, 741),
            "emissions 1234 tCO2e",
            "clipped_or_rotated_words",
        ),
        # No word of the pinned box is fully contained at all.
        (
            [HEADING, PARAGRAPH],
            "ENV HEADING 2030",
            (70, 742, 90, 755),
            "ENV HEADING",
            "clipped_or_rotated_words",
        ),
        # The quote occurs twice inside the pinned paragraph: position stays
        # ambiguous instead of taking the first hit.
        (
            [HEADING, (72, 720, "emissions 1234 tCO2e emissions 1234 tCO2e")],
            "emissions 1234 tCO2e emissions 1234 tCO2e",
            (70, 710, 400, 741),
            "emissions 1234 tCO2e",
            "rendered_quote_unresolved",
        ),
        # A number that only exists on the neighbouring line is not claim text.
        (
            [(72, 740, "target 999 tCO2e"), PARAGRAPH],
            PARAGRAPH_TEXT,
            (70, 710, 300, 741),
            "999 tCO2e",
            "source_invalid",
        ),
    ],
    ids=["clipped_quote_line", "no_contained_word", "repeated_quote", "neighbor_number"],
)
def test_crossing_words_on_claim_lines_wrong_numbers_and_ambiguity_stay_unverified(
    monkeypatch, lines, text, bbox, quote, reason
):
    from proofops.adapters.local import claim_source_verification as verifier

    source, graph = geometry_case(lines, text, bbox)
    ref = (
        quote_ref(graph, text, quote)
        if quote in text
        else replace(graph.blocks[0].source_ref(), char_start=0, char_end=len(quote), quote=quote)
    )
    monkeypatch.setattr(verifier, "_rendered_text", lambda *a, **k: dict(status="read", text=text))
    record = verifier.attest_claim_spans(graph, source, (ref,), tenant_id=TENANT)["records"][0]
    assert record["status"] == "unresolved"
    assert record["reason"] == reason


def test_stored_policies_replay_under_their_own_frozen_verifier_bytes():
    from proofops.adapters.local import (
        claim_source_verification as verifier,
    )
    from proofops.adapters.local import (
        claim_source_verification_v1,
        claim_source_verification_v2,
    )

    live = verifier.claim_source_policy()
    for frozen in (claim_source_verification_v1, claim_source_verification_v2):
        policy = frozen.claim_source_policy()
        assert policy != live
        assert policy["verifier_sha256"] == sha256(Path(frozen.__file__).read_bytes()).hexdigest()
        assert verifier.claim_source_reader(policy) is frozen
    assert verifier.claim_source_reader(live) is verifier
    with pytest.raises(ValueError):
        verifier.claim_source_reader(dict(live, verifier_sha256="f" * 64))


@pytest.mark.parametrize(
    "text,quote",
    [
        ("100.5", "100."),
        ("-100 tonnes", "100 tonnes"),
        ("1,000 tonnes", "000 tonnes"),
        ("100%p", "100%"),
    ],
)
def test_rendered_quote_cannot_cut_decimal_sign_grouping_or_unit(text, quote):
    from proofops.adapters.local.claim_source_verification import _unique_quote

    assert not _unique_quote(text, quote)
