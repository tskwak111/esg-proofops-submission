"""Original PDF marker ownership is bounded to one leaf, never a table scope."""

import json
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from io import BytesIO

import pytest
from proofops.adapters.local.table_notes import freeze_note_review, prepare, validate
from proofops.application.ingest.graph_fusion import fuse_candidates
from proofops.domain.provenance import canonical_hash
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject

from tests.acceptance.test_parsing import FOREIGN, TENANT, candidate, pdf


def sample(
    *,
    raised=True,
    numeric=False,
    duplicate=False,
    other_column=False,
    tight=False,
    note="2) Rounded totals",
    extra_note=None,
    extra_note_x=70,
    note_x=70,
    other_text="Total 2)",
    marker_size=None,
    marker_rise=3,
):
    writer = PdfWriter()
    writer.add_page(PdfReader(BytesIO(pdf())).pages[0])
    base = "12345" if numeric else "Total"
    # Helvetica widths keep the superscript adjacent but in a separate native word.
    marker_x = 107.36 if numeric else 100.676
    size = marker_size if marker_size is not None else (6 if raised else 12)
    commands = [
        f"BT /F1 12 Tf 70 700 Td ({base}) Tj ET",
        f"BT /F1 {size} Tf {marker_x} {700 + marker_rise if raised else 700} Td (2\\)) Tj ET",
    ]
    cellbox = (69, 699, 115, 709) if tight else (65, 690, 150, 720)
    blocks = [
        ("table", "table", base + " 2)", (60, 680, 290, 730), ()),
        ("cell", "table_cell", base + " 2)", cellbox, ()),
    ]
    edges = [("cell", "table", "table_parent")]
    if duplicate or other_column:
        x = 370 if other_column else 170
        commands += [
            f"BT /F1 12 Tf {x} 700 Td (Total) Tj ET",
            f"BT /F1 6 Tf {x + 30.676} 703 Td (2\\)) Tj ET",
        ]
        blocks += [("other", "table_cell", other_text, (x - 5, 690, x + 80, 720), ())]
        if other_column:
            blocks += [("other-table", "table", "Total 2)", (350, 680, 590, 730), ())]
        edges += [("other", "other-table" if other_column else "table", "table_parent")]
    commands += [f"BT /F1 9 Tf {note_x} 650 Td (" + note.replace(")", r"\)") + ") Tj ET"]
    if extra_note:
        commands += [
            f"BT /F1 9 Tf {extra_note_x} 636 Td (" + extra_note.replace(")", r"\)") + ") Tj ET"
        ]
    stream = DecodedStreamObject()
    stream.set_data("\n".join(commands).encode())
    writer.pages[0][NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    source = output.getvalue()
    graph = fuse_candidates(
        (
            replace(
                candidate("native-marker", blocks, edges), source_sha256=sha256(source).hexdigest()
            ),
        ),
        tenant_id=TENANT,
    )
    packet = prepare(
        graph,
        source,
        sorted(b.source_id for b in graph.blocks if b.kind == "table"),
        tenant_id=TENANT,
        contract_version=3,
    )
    parts = [f for f in packet["untrusted_document_data"]["fragments"] if f["bbox"][1] > 140]
    extracted = validate({"notes": []}, packet, graph, source, tenant_id=TENANT)
    artifact = freeze_note_review(graph, source, packet, extracted, tenant_id=TENANT)
    fragment = dict(
        note_artifact_sha256=json.loads(artifact)["artifact_sha256"],
        packet_sha256=canonical_hash(packet),
        physical_page=1,
        fragment_ids=[f["id"] for f in parts],
        native_word_indices=sorted({i for f in parts for i in f["word_indices"]}),
    )
    cell = next(b for b in graph.blocks if b.sources[0].source_native_id == "cell")
    table = next(b for b in graph.blocks if b.sources[0].source_native_id == "table")
    target = dict(
        source_id=cell.source_id,
        table_id=table.source_id,
        row=None,
        column=None,
        row_span=None,
        column_span=None,
    )
    return graph, source, fragment, [target], (artifact,)


def prove(args, **kwargs):
    from proofops.adapters.local.native_note_ownership import prove_native_note_marker

    return prove_native_note_marker(*args, tenant_id=kwargs.get("tenant_id", TENANT))


@pytest.mark.parametrize("options", [{}, {"tight": True}, {"other_column": True}])
def test_proves_only_original_unique_raised_leaf_marker(options):
    args = sample(**options)
    before = canonical_hash(args[0].to_dict())
    proof = prove(args)
    assert proof is not None
    assert set(proof) == {
        "schema",
        "tenant_id",
        "document_version_id",
        "parse_manifest_id",
        "source_sha256",
        "graph_sha256",
        "fragment_id",
        "target_source_ids",
        "marker",
        "base_word_index",
        "marker_word_index",
        "note_word_indices",
        "proof_sha256",
    }
    assert proof["schema"] == "native_note_marker_v1" and proof["marker"] == "2)"
    assert proof["target_source_ids"] == [args[3][0]["source_id"]]
    assert proof["fragment_id"] == canonical_hash(args[2])
    assert proof["note_word_indices"] == args[2]["native_word_indices"]
    assert proof["base_word_index"] != proof["marker_word_index"]
    assert proof["proof_sha256"] == canonical_hash(
        {k: v for k, v in proof.items() if k != "proof_sha256"}
    )
    assert prove(args) == proof and canonical_hash(args[0].to_dict()) == before


@pytest.mark.parametrize(
    "options",
    [
        {"raised": False},
        {"numeric": True},
        {"duplicate": True},
        {"note": "2)"},
        {"note": "3) Wrong marker"},
        {"extra_note": "3) Different note"},
        {"note": "2) Note 3) Unrelated note"},
        {"duplicate": True, "other_text": "Total"},
        {"extra_note": "Unrelated column", "extra_note_x": 200},
        {"note_x": 350},
        {"marker_size": 12},
        {"marker_rise": 0},
    ],
)
def test_rejects_numeric_values_competing_owners_and_non_notes(options):
    assert prove(sample(**options)) is None


def test_rejects_tampered_or_uncommitted_fragments_and_scope_expansion():
    args = sample()
    for field, value in [
        ("fragment_ids", []),
        ("native_word_indices", [0]),
        ("packet_sha256", "0" * 64),
        ("physical_page", 2),
    ]:
        bad = list(args)
        bad[2] = args[2] | {field: value}
        assert prove(bad) is None
    assert prove(args, tenant_id=FOREIGN) is None
    assert prove((args[0], args[1] + b"changed", *args[2:])) is None
    assert prove((*args[:4], ())) is None
    assert prove((*args[:4], args[4] * 2)) is None
    for targets in ([], args[3] * 2, [args[3][0] | {"source_id": args[3][0]["table_id"]}]):
        assert prove((*args[:3], targets, args[4])) is None
    bad = deepcopy(args[2])
    bad["native_word_indices"].append(bad["native_word_indices"][-1])
    assert prove((*args[:2], bad, *args[3:])) is None


@pytest.mark.parametrize(
    "extra,x,expected",
    [("2) Another explanation", 70, False), ("2) Another column", 350, True), ("2)", 70, True)],
)
def test_unselected_same_number_note_anchor_must_also_be_unique(extra, x, expected):
    args = list(sample(extra_note=extra, extra_note_x=x))
    parts = json.loads(args[4][0])["packet"]["untrusted_document_data"]["fragments"]
    first = next(p for p in parts if p["id"] == args[2]["fragment_ids"][0])
    args[2] = args[2] | {
        "fragment_ids": [first["id"]],
        "native_word_indices": sorted(first["word_indices"]),
    }
    assert (prove(args) is not None) == expected
