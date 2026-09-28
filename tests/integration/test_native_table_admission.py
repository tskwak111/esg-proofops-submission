"""Real PDF bytes, synthetic graph and explicitly mocked OCR; live reports are separate."""

from dataclasses import asdict, replace
from hashlib import sha256
from io import BytesIO

import pytest
from proofops.application.ingest.graph_fusion import QualityIssue, fuse_candidates
from proofops.domain.provenance import canonical_hash
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from tests.acceptance.test_parsing import TENANT, candidate


def fixture():
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    rows = [["Year", "2024", "2025"], ["MWh", "25", "79"]]
    stream = DecodedStreamObject()
    stream.set_data(
        (
            "\n".join(
                f"BT /F1 10 Tf {25+c*100} {750-r*30} Td ({text}) Tj ET"
                for r, row in enumerate(rows)
                for c, text in enumerate(row)
            )
        ).encode()
    )
    page[NameObject("/Contents")] = writer._add_object(stream)
    out = BytesIO()
    writer.write(out)
    source = out.getvalue()
    raw = [("T", "table", "Year\t2024\t2025\nMWh\t25\t79", (20, 710, 320, 770), ())]
    edges = []
    for r, row in enumerate(rows):
        raw.append(
            (f"R{r}", "table_row", "\t".join(row), (20, 740 - r * 30, 320, 770 - r * 30), ())
        )
        edges.append((f"R{r}", "T", "table_parent"))
        for c, text in enumerate(row):
            raw.append(
                (
                    f"C{r}{c}",
                    "table_cell",
                    text,
                    (20 + c * 100, 740 - r * 30, 120 + c * 100, 770 - r * 30),
                    (),
                )
            )
            edges.extend([(f"C{r}{c}", f"R{r}", "table_parent"), (f"C{r}{c}", "T", "table_parent")])
    batch = candidate("test", raw, edges)
    batch = replace(
        batch,
        source_sha256=sha256(source).hexdigest(),
        blocks=tuple(
            replace(
                b,
                table_native_id="T",
                row_number=int(b.source.source_native_id[1]),
                column_number=int(b.source.source_native_id[2]),
            )
            if b.kind == "table_cell"
            else b
            for b in batch.blocks
        ),
    )
    graph = fuse_candidates((batch,), tenant_id=TENANT)
    table = next(b for b in graph.blocks if b.kind == "table")
    graph = replace(
        graph,
        issues=graph.issues
        + (
            QualityIssue(
                "vision", "table_vision_not_run", 1, (table.source_id,), "open", "not_run"
            ),
        ),
    )
    return source, graph


def ocr(page, box, **kwargs):
    return {
        "status": "read",
        "text": page.crop(box).extract_text() or "",
        "image_sha256": "a" * 64,
        "reader": "mocked_for_test",
    }


def test_verified_table_replays_and_rejects_tampering(monkeypatch):
    from proofops.adapters.local import table_source_verification as v

    v._attest_json.cache_clear()
    source, graph = fixture()
    before = canonical_hash(asdict(graph))
    monkeypatch.setattr(v, "_rendered_text", ocr)
    receipt = v.attest_tables(graph, source, tenant_id=TENANT)
    assert receipt["records"][0]["status"] == "verified"
    checked = v.replay_tables(receipt, graph, source, tenant_id=TENANT)
    assert all(b.quality == "verified" for b in checked.blocks)
    assert checked.issues[0].state == "resolved"
    assert canonical_hash(asdict(graph)) == before
    with pytest.raises(ValueError):
        v.replay_tables(receipt, graph, source + b"x", tenant_id=TENANT)
    with pytest.raises(ValueError):
        v.replay_tables(receipt, graph, source, tenant_id="22222222-2222-4222-8222-222222222222")
    bad = dict(receipt)
    bad["records"] = []
    with pytest.raises(ValueError):
        v.replay_tables(bad, graph, source, tenant_id=TENANT)


@pytest.mark.parametrize(
    "mutation",
    ["number", "year", "unit", "column", "footnote", "ocr", "partial", "unlocated", "shifted"],
)
def test_wrong_or_unresolved_table_cannot_promote(monkeypatch, mutation):
    from proofops.adapters.local import table_source_verification as v

    v._attest_json.cache_clear()
    source, graph = fixture()
    monkeypatch.setattr(v, "_rendered_text", ocr)
    if mutation in ("number", "year", "unit", "column"):
        match = {"number": "25", "year": "2025", "unit": "MWh", "column": "25"}[mutation]
        batch = graph.candidates[0]

        def changed(c):
            if mutation != "column":
                wrong = {"number": "26", "year": "2026", "unit": "GWh"}[mutation]
                return replace(
                    c, source=replace(c.source, raw_text=c.source.raw_text.replace(match, wrong))
                )
            if c.kind != "table_cell" or c.source.raw_text != match:
                return c
            return (
                replace(c, column_number=2)
                if mutation == "column"
                else replace(
                    c,
                    source=replace(
                        c.source, raw_text={"number": "26", "year": "2026", "unit": "GWh"}[mutation]
                    ),
                )
            )

        graph = fuse_candidates(
            (replace(batch, blocks=tuple(changed(c) for c in batch.blocks)),), tenant_id=TENANT
        )
    elif mutation == "unlocated":
        graph = replace(
            graph,
            blocks=tuple(
                replace(b, winner=None, quality="conflicted") if b.kind == "table" else b
                for b in graph.blocks
            ),
        )
    elif mutation == "shifted":
        batch = graph.candidates[0]
        graph = fuse_candidates(
            (
                replace(
                    batch,
                    blocks=tuple(
                        replace(c, source=replace(c.source, native_bbox=(121, 710, 220, 740)))
                        if c.kind == "table_cell" and c.source.raw_text == "25"
                        else c
                        for c in batch.blocks
                    ),
                ),
            ),
            tenant_id=TENANT,
        )
    elif mutation == "footnote":
        table = next(b for b in graph.blocks if b.kind == "table")
        graph = replace(
            graph,
            issues=graph.issues
            + (
                QualityIssue(
                    "note", "table_note_review", 1, (table.source_id,), "open", "unknown ownership"
                ),
            ),
        )
    elif mutation == "ocr":
        monkeypatch.setattr(
            v,
            "_rendered_text",
            lambda *a, **kw: dict(status="read", text="999", image_sha256="b" * 64),
        )
    elif mutation == "partial":
        monkeypatch.setattr(v, "MAX_CELLS", 2)
    receipt = v.attest_tables(graph, source, tenant_id=TENANT)
    assert not any(r["status"] == "verified" for r in receipt["records"])
    checked = v.replay_tables(receipt, graph, source, tenant_id=TENANT)
    assert all(b.quality != "verified" for b in checked.blocks)


def test_parser_persists_attestation_and_preserves_default_profile(tmp_path, monkeypatch):
    import json

    from proofops.adapters.local import table_source_verification as v
    from proofops.adapters.parsing.opendataloader import OpenDataLoaderParser, ParseFailure
    from proofops.application.ingest.graph_fusion import ParserProfile

    from tests.acceptance.test_parsing import JAVA, MANIFEST, VERSION, pdf, source

    v._attest_json.cache_clear()
    monkeypatch.setattr(v, "_rendered_text", ocr)
    item = source(pdf(table=True))
    default = ParserProfile(MANIFEST, physical_pages=(2,), java_executable=JAVA)
    assert "table_source_policy_sha256" not in default.invocation_snapshot()
    with pytest.raises(ValueError):
        replace(default, table_source_policy_sha256="bad")
    profile = replace(default, table_source_policy_sha256=v.policy_sha256())
    assert profile.config_hash != default.config_hash
    parser = OpenDataLoaderParser(tmp_path)
    graph = parser.parse(item, profile, tenant_id=TENANT)
    # Actual dual-parser fixture retains unresolved duplicate/unlocated rows.
    assert any(i.state == "open" for i in graph.issues)
    assert parser.load_verified(item, profile, tenant_id=TENANT) == graph
    folder = tmp_path / TENANT / VERSION / MANIFEST
    receipt = json.loads((folder / "table-source.json").read_text())
    assert receipt["semantic_binding"] == "undetermined"
    assert receipt["records"]
    assert all(r["status"] == "unresolved" for r in receipt["records"])
    (folder / "table-source.json").chmod(0o600)
    (folder / "table-source.json").write_text("{}")
    with pytest.raises(ParseFailure):
        parser.load_verified(item, profile, tenant_id=TENANT)
