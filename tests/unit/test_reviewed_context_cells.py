"""Literal context-cell attestation: what it admits and what it must refuse.

Most tests here are **local integration checks**. They read the operator review
under ``tests/fixtures/pipeline/reviewed-table-layout`` and the customer PDFs under
``tests/fixtures/source-documents``, which are local git-ignored inputs, and they run the
real on-device rendered reader. They skip when those inputs or the Swift
toolchain are absent and are not portable CI coverage.
``test_selection_must_be_a_bounded_unique_list_of_ids`` and
``test_policy_pins_the_readers_it_reuses`` are portable.

The acceptance case is real: the merged Lotte metric cell and its unit cell, which
the selected-cell verifier can never promote. The contrast cases are the ones that
matter -- a declared literal the source does not show, a foreign source, a silently
changed graph and an edited receipt must all refuse.
"""

import copy
import json
import os
import sys
from pathlib import Path

import pytest
from proofops.adapters.local import reviewed_context_verification as context
from proofops.adapters.local.reviewed_table import graph_from_review
from proofops.adapters.local.selected_cell_table_verification import attest_tables

APP = Path(__file__).resolve().parents[2]
REVIEW = APP / "tests/fixtures/pipeline/reviewed-table-layout/lotte-candidates.json"
PDF = APP / "tests/fixtures/source-documents/lotte.pdf"
KIA_REVIEW = APP / "tests/fixtures/pipeline/reviewed-table-layout/kia-candidates.json"
KIA_PDF = APP / "tests/fixtures/source-documents/kia.pdf"
_CLI_TOOLS = Path("/Library/Developer/CommandLineTools")

# Lotte p117 row 1: the merged metric cell (row_span 6), its unit, its 2025 value
# and its footnote cell. Only the literal text of the first two may be admitted.
METRIC = "r1c0"
UNIT = "r1c2"
VALUE = "r1c3"
FOOTNOTE = "r1c6"

local_inputs = pytest.mark.skipif(
    not (
        REVIEW.exists()
        and PDF.exists()
        and sys.platform == "darwin"
        and (os.environ.get("DEVELOPER_DIR") or _CLI_TOOLS.exists())
    ),
    reason="local operator review, customer PDF and macOS Swift toolchain required",
)


@pytest.fixture(scope="module", autouse=True)
def _toolchain():
    """The rendered reader shells out to `swift`; point it at the CLI tools."""
    previous = os.environ.get("DEVELOPER_DIR")
    if previous is None and _CLI_TOOLS.exists():
        os.environ["DEVELOPER_DIR"] = str(_CLI_TOOLS)
    yield
    if previous is None:
        os.environ.pop("DEVELOPER_DIR", None)


def _graph(review_path=REVIEW, pdf_path=PDF, edit=None):
    review = json.loads(review_path.read_text())
    if edit is not None:
        review = copy.deepcopy(review)
        edit(review)
    source = pdf_path.read_bytes()
    graph, _ = graph_from_review(review, source)
    ids = {
        candidate.source.source_native_id.removeprefix("reviewed-cell:"): block.source_id
        for block in graph.blocks
        for candidate in block.candidates
    }
    return graph, source, ids


def _keep(*keys):
    """Trim the real review to a few of its own cells; nothing is added or rewritten."""

    def edit(review):
        review["cells"] = {key: cell for key, cell in review["cells"].items() if key in keys}

    return edit


@pytest.fixture(scope="module")
def year_header():
    """A trimmed real Lotte grid whose one v3 selection is 1,840,806 under 2025.

    Trimmed only to keep the test fast: v3 reads every numeric cell of a table, so
    the untrimmed page 117 grid costs ~101s. That full run was performed once
    outside the suite and proved the same header literals (r0c3/r0c4/r0c5); this
    fixture exercises the identical code path in ~10s.
    """
    graph, source, ids = _graph(edit=_keep("r0c2", "r0c3", UNIT, VALUE))
    proof = attest_tables(graph, source, tenant_id=graph.tenant_id)
    return graph, source, ids, proof


@pytest.fixture(scope="module")
def lotte():
    graph, source, ids = _graph()
    selection = [ids[key] for key in (METRIC, UNIT, VALUE, FOOTNOTE)]
    receipt = context.attest_context_cells(graph, source, selection, tenant_id=graph.tenant_id)
    return graph, source, ids, selection, receipt


@local_inputs
def test_merged_metric_and_unit_literals_are_attested_natively_and_rendered(lotte):
    graph, _, ids, _, receipt = lotte
    records = {record["source_id"]: record for record in receipt["records"]}
    metric, unit = records[ids[METRIC]], records[ids[UNIT]]
    assert metric["status"] == "verified" and unit["status"] == "verified"
    assert metric["normalized_literal"] == "직접 온실가스(Scope 1) 배출량 - 사업장별"
    assert unit["normalized_literal"] == "tCO2eq"
    # The merged cell is admitted as the merged cell it is, not sliced per row.
    assert metric["row"] == 1 and metric["row_span"] == 6
    for record in (metric, unit):
        assert record["attestation"] == "native_and_rendered_full_literal"
        # The rendered crop came from the cell's own stored bbox, provably.
        assert record["render_crop_pixel_bbox"] == context._render_crop_box(record["bbox"])
    # Literal text only: no role, unit, year, scope, linkage or grade is claimed.
    assert receipt["scope"] == "caller_selected_table_cell_full_literal_text_only"
    assert receipt["semantic_binding"] == "undetermined"
    assert receipt["grade_effect"] == "none"
    for field in (
        "role_binding",
        "metric_binding",
        "unit_binding",
        "year_binding",
        "scope_binding",
        "value_linkage",
    ):
        assert receipt[field] == "not_attested"
    assert receipt["source_sha256"] == graph.source_sha256
    assert receipt["interactive_content_present"] is False


@local_inputs
def test_numeric_and_unreadable_cells_stay_held(lotte):
    """A number belongs to the selected-cell verifier; a mismatch stays held."""
    _, _, ids, _, receipt = lotte
    records = {record["source_id"]: record for record in receipt["records"]}
    assert records[ids[VALUE]] == dict(
        source_id=ids[VALUE],
        status="held",
        reason="numeric_cell_requires_selected_cell_verifier",
    )
    # The footnote cell's source really prints a curly quote where the declared
    # literal has one too, but the recognizer reads a straight quote: held, not
    # repaired and not quietly dropped.
    assert records[ids[FOOTNOTE]]["status"] == "held"
    assert records[ids[FOOTNOTE]]["reason"] == "rendered_cell_literal_mismatch"


@local_inputs
def test_replay_promotes_only_the_attested_cells_and_leaves_the_graph_alone(lotte):
    graph, source, ids, selection, receipt = lotte
    before = {block.source_id: block.quality for block in graph.blocks}
    replayed = context.replay_context_cells(
        receipt, graph, source, selection, tenant_id=graph.tenant_id
    )
    after = {block.source_id: block.quality for block in replayed.blocks}
    promoted = {sid for sid, quality in after.items() if quality != before[sid]}
    assert promoted == {ids[METRIC], ids[UNIT]}
    assert after[ids[METRIC]] == after[ids[UNIT]] == "verified"
    # The caller's graph object is untouched, and so are the table node, the other
    # cells and every issue.
    assert {block.source_id: block.quality for block in graph.blocks} == before
    assert after[ids["reviewed-table"]] == before[ids["reviewed-table"]] == "unverified"
    assert before[ids[VALUE]] == after[ids[VALUE]] == "unverified"
    assert replayed.issues == graph.issues
    assert [block.kind for block in replayed.blocks] == [block.kind for block in graph.blocks]


@local_inputs
def test_a_declared_literal_the_source_does_not_show_is_refused():
    """The unit cell is relabelled `tCO2e`; its own pinned ink still says otherwise."""

    def edit(review):
        review["cells"][UNIT]["raw_text"] = "tCO2e"

    graph, source, ids = _graph(edit=edit)
    receipt = context.attest_context_cells(graph, source, [ids[UNIT]], tenant_id=graph.tenant_id)
    assert receipt["records"][0]["status"] == "held"
    assert receipt["records"][0]["reason"] == "text_mismatch"
    replayed = context.replay_context_cells(
        receipt, graph, source, [ids[UNIT]], tenant_id=graph.tenant_id
    )
    assert {block.quality for block in replayed.blocks} == {"unverified"}


@local_inputs
@pytest.mark.skipif(not KIA_PDF.exists(), reason="second customer PDF is local only")
def test_a_foreign_source_is_refused(lotte):
    graph, _, _, selection, _ = lotte
    with pytest.raises(ValueError, match="CONTEXT_SOURCE_MISMATCH"):
        context.attest_context_cells(
            graph, KIA_PDF.read_bytes(), selection, tenant_id=graph.tenant_id
        )


@local_inputs
def test_a_silently_changed_graph_cannot_replay_an_earlier_receipt(lotte):
    """Same PDF, same manifest id, one edited cell: the receipt must not replay."""
    _, _, ids, _, receipt = lotte

    def edit(review):
        review["cells"][UNIT]["raw_text"] = "tCO2e"

    changed, source, changed_ids = _graph(edit=edit)
    selection = [changed_ids[key] for key in (METRIC, UNIT, VALUE, FOOTNOTE)]
    assert changed.parse_manifest_id == receipt["parse_manifest_id"]
    with pytest.raises(ValueError, match="CONTEXT_CELL_RECEIPT_MISMATCH"):
        context.replay_context_cells(
            receipt, changed, source, selection, tenant_id=changed.tenant_id
        )


@local_inputs
@pytest.mark.parametrize(
    "forge",
    [
        pytest.param(lambda r, i: r["records"][2].update(status="verified"), id="held_to_verified"),
        pytest.param(lambda r, i: r["records"][1].update(normalized_literal="tCO2e"), id="literal"),
        pytest.param(lambda r, i: r.update(artifact_sha256="0" * 64), id="artifact_hash"),
        pytest.param(lambda r, i: r["policy"].update(verifier_sha256="0" * 64), id="policy_hash"),
    ],
)
def test_a_forged_receipt_cannot_promote_anything(lotte, forge):
    graph, source, ids, selection, receipt = lotte
    forged = copy.deepcopy(receipt)
    forge(forged, ids)
    with pytest.raises(ValueError, match="CONTEXT_CELL_RECEIPT_MISMATCH"):
        context.replay_context_cells(forged, graph, source, selection, tenant_id=graph.tenant_id)


@pytest.mark.skipif(
    not (
        KIA_REVIEW.exists()
        and KIA_PDF.exists()
        and sys.platform == "darwin"
        and (os.environ.get("DEVELOPER_DIR") or _CLI_TOOLS.exists())
    ),
    reason="second local operator review, PDF and Swift toolchain required",
)
def test_the_same_path_holds_honestly_on_a_second_real_layout():
    """Kia p106: plain literals pass; a subscript-broken unit is held, not repaired."""
    graph, source, ids = _graph(KIA_REVIEW, KIA_PDF)
    # r2c0/r0c1 are plain literals; r2c1 is the unit whose CO2 subscript is a
    # separate text run, and r1c0 carries a trailing footnote reference digit.
    keys = ("r2c0", "r0c1", "r2c1", "r1c0")
    selection = [ids[key] for key in keys]
    receipt = context.attest_context_cells(graph, source, selection, tenant_id=graph.tenant_id)
    status = {key: record["status"] for key, record in zip(keys, receipt["records"])}
    assert status["r2c0"] == status["r0c1"] == "verified"
    assert status["r2c1"] == status["r1c0"] == "held"
    reasons = {key: record["reason"] for key, record in zip(keys, receipt["records"])}
    assert reasons["r2c1"] == reasons["r1c0"] == "rendered_cell_literal_mismatch"
    replayed = context.replay_context_cells(
        receipt, graph, source, selection, tenant_id=graph.tenant_id
    )
    promoted = {block.source_id for block in replayed.blocks if block.quality == "verified"}
    assert promoted == {ids["r2c0"], ids["r0c1"]}


@local_inputs
def test_a_period_header_literal_is_unreachable_without_the_selected_cell_proof(year_header):
    """`2025` is a bare number, so by itself it is refused like any value."""
    graph, source, ids, _ = year_header
    receipt = context.attest_context_cells(graph, source, [ids["r0c3"]], tenant_id=graph.tenant_id)
    assert receipt["records"][0]["reason"] == "numeric_cell_requires_selected_cell_verifier"
    assert receipt["column_head_literal_source_ids"] == []
    assert receipt["selected_cell_receipt_sha256"] is None


@local_inputs
def test_a_revalidated_selected_cell_receipt_admits_the_header_literal_only(year_header):
    """The header literal passes; the value cell v3 promoted still does not."""
    graph, source, ids, proof = year_header
    selection = [ids["r0c3"], ids[UNIT], ids[VALUE]]
    receipt = context.attest_context_cells(
        graph, source, selection, tenant_id=graph.tenant_id, header_receipt=proof
    )
    records = {record["source_id"]: record for record in receipt["records"]}
    header = records[ids["r0c3"]]
    assert header["status"] == "verified" and header["normalized_literal"] == "2025"
    assert header["literal_role"] == "column_head_literal_proven_by_selected_cell_receipt"
    assert header["attestation"] == "native_and_rendered_full_literal"
    assert records[ids[UNIT]]["status"] == "verified"
    assert records[ids[UNIT]]["literal_role"] == "row_or_label_context_literal"
    # The proof is a structural role witness, never a period or a value licence.
    assert records[ids[VALUE]]["reason"] == "numeric_cell_requires_selected_cell_verifier"
    assert receipt["column_head_literal_source_ids"] == [ids["r0c3"]]
    assert receipt["selected_cell_receipt_sha256"] == proof["artifact_sha256"]
    assert receipt["year_binding"] == receipt["period_binding"] == "not_attested"
    replayed = context.replay_context_cells(
        receipt, graph, source, selection, tenant_id=graph.tenant_id, header_receipt=proof
    )
    promoted = {block.source_id for block in replayed.blocks if block.quality == "verified"}
    assert promoted == {ids["r0c3"], ids[UNIT]}


@local_inputs
def test_an_edited_selected_cell_receipt_is_refused(year_header):
    graph, source, ids, proof = year_header
    forged = copy.deepcopy(proof)
    forged["records"][0]["selections"][0]["header_text"] = "2024"
    with pytest.raises(ValueError, match="SELECTED_CELL_RECEIPT_MISMATCH"):
        context.attest_context_cells(
            graph, source, [ids["r0c3"]], tenant_id=graph.tenant_id, header_receipt=forged
        )


@local_inputs
def test_dropping_the_header_proof_at_replay_cannot_keep_the_promotion(year_header):
    graph, source, ids, proof = year_header
    selection = [ids["r0c3"]]
    receipt = context.attest_context_cells(
        graph, source, selection, tenant_id=graph.tenant_id, header_receipt=proof
    )
    with pytest.raises(ValueError, match="CONTEXT_CELL_RECEIPT_MISMATCH"):
        context.replay_context_cells(receipt, graph, source, selection, tenant_id=graph.tenant_id)


def test_selection_must_be_a_bounded_unique_list_of_ids():
    """Refused before any graph or source work; no silent truncation."""
    for selection, message in (
        ("not-a-list", "CONTEXT_CELL_SELECTION_INVALID"),
        ([], "CONTEXT_CELL_SELECTION_INVALID"),
        ([""], "CONTEXT_CELL_SELECTION_INVALID"),
        ([None], "CONTEXT_CELL_SELECTION_INVALID"),
        ([f"id-{i}" for i in range(context.MAX_CELLS + 1)], "CONTEXT_CELL_SELECTION_INVALID"),
        (["same", "same"], "CONTEXT_CELL_SELECTION_DUPLICATE"),
    ):
        with pytest.raises(ValueError, match=message):
            context.attest_context_cells(None, b"", selection, tenant_id="unused")


def test_policy_pins_the_readers_it_reuses():
    policy = context.context_cell_policy()
    assert policy["schema"] == context.SCHEMA
    # The reused source reader and its own pinned policy, including the native
    # paragraph policy, are part of this policy.
    assert policy["claim_source_policy"]["schema"] == "claim_source_policy_v2"
    assert policy["claim_source_policy"]["native_policy"]["mode"] == "paragraph_native_glyph_v2"
    assert len(policy["selected_cell_policy_sha256"]) == 64
    for field in (
        "verifier_sha256",
        "source_reader_sha256",
        "rendered_reader_sha256",
        "rendered_ocr_sha256",
        "glyph_verifier_sha256",
        "normalizer_sha256",
    ):
        assert len(policy[field]) == 64
    assert set(policy["readers"]) == {"pdfplumber", "pdfminer.six", "pypdfium2"}
