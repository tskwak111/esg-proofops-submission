from copy import deepcopy
from dataclasses import replace
from hashlib import sha256

import pytest
from proofops.application.ingest.graph_fusion import fuse_candidates

from tests.acceptance.test_parsing import TENANT, pdf
from tests.acceptance.test_tables import table


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"BT /F1 12 Tf 610 720 Td (invisible note) Tj ET",
        b"BT /F1 12 Tf 0 1 -1 0 100 100 Tm (rotated note) Tj ET",
    ],
)
def test_no_readable_source_never_calls_model_or_accepts_off_page_words(tmp_path, content):
    from io import BytesIO

    from proofops.adapters.local.note_extraction import run
    from proofops.adapters.local.table_notes import prepare
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import DecodedStreamObject, NameObject

    from tests.acceptance.test_parsing import candidate

    writer = PdfWriter()
    writer.add_page(PdfReader(BytesIO(pdf())).pages[0])
    stream = DecodedStreamObject()
    stream.set_data(content)
    writer.pages[0][NameObject("/Contents")] = writer._add_object(stream)
    out = BytesIO()
    writer.write(out)
    source = out.getvalue()
    graph = fuse_candidates(
        (replace(candidate("opendataloader", []), source_sha256=sha256(source).hexdigest()),),
        tenant_id=TENANT,
    )
    if b"610" in content:
        with pytest.raises(ValueError, match="word geometry"):
            prepare(graph, source, [], tenant_id=TENANT, page=1)
        table_graph = fuse_candidates(
            (replace(table([["Metric"]]), source_sha256=sha256(source).hexdigest()),),
            tenant_id=TENANT,
        )
        tid = next(b.source_id for b in table_graph.blocks if b.kind == "table")
        with pytest.raises(ValueError, match="word geometry"):
            prepare(table_graph, source, [tid], tenant_id=TENANT)
        return

    class Client:
        model = "synthetic-test"
        calls = 0

        def summary(self):
            return dict(calls=self.calls)

        def complete(self, *args, **kwargs):
            self.calls += 1
            return dict(content='{"notes":[]}', provider_model=self.model)

    client = Client()
    result = run(graph, source, [], client, tmp_path / "empty", tenant_id=TENANT, page=1)
    assert client.calls == 0
    assert result["status"] == "not_run" and result["error"] == "NO_READABLE_NATIVE_WORDS"
    assert result["coverage_status"] == "unknown" and result["notes"] == []


def test_page_only_discovery_keeps_disjoint_ruled_columns_in_separate_requests(tmp_path):
    import json
    from io import BytesIO

    from proofops.adapters.local.note_extraction import run
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import DecodedStreamObject, NameObject

    from tests.acceptance.test_parsing import candidate

    reader = PdfReader(BytesIO(pdf()))
    writer = PdfWriter()
    writer.add_page(reader.pages[0])
    page = writer.pages[0]
    commands = [page.get_contents().get_data()]
    for left, label in ((50, "Left"), (350, "Right")):
        commands.extend(f"{left} {y} m {left + 180} {y} l S".encode() for y in (600, 630, 660))
        commands.append(
            f"BT /F1 12 Tf {left} 580 Td (1\u0029 {label} note) Tj ET".replace(
                "1)", "1\\)"
            ).encode()
        )
    stream = DecodedStreamObject()
    stream.set_data(b"\n".join(commands))
    page[NameObject("/Contents")] = writer._add_object(stream)
    out = BytesIO()
    writer.write(out)
    source = out.getvalue()
    graph = fuse_candidates(
        (
            replace(
                candidate("opendataloader", [("p", "paragraph", "Source", (10, 10, 50, 30), ())]),
                source_sha256=sha256(source).hexdigest(),
            ),
        ),
        tenant_id=TENANT,
    )

    class Client:
        model = "synthetic-test"
        seen = []

        def summary(self):
            return dict(calls=len(self.seen))

        def complete(self, system, content, **kwargs):
            self.seen.append(json.loads(content))
            return dict(content='{"notes":[]}', provider_model=self.model)

    client = Client()
    result = run(graph, source, [], client, tmp_path / "columns", tenant_id=TENANT, page=1)
    assert result["status"] == "source_bound_proposals"
    assert len(client.seen) == 2
    assert all(not ("Left note" in str(wire) and "Right note" in str(wire)) for wire in client.seen)


def test_selected_small_subscript_stays_with_its_unique_note_line():
    from proofops.adapters.local.note_extraction import join_note_lines

    fragments = [
        dict(id="f0", text="1) NF emissions", bbox=[10, 10, 100, 20]),
        dict(id="f1", text="3", bbox=[25, 15, 28, 21]),
        dict(id="f2", text="1234", bbox=[110, 10, 140, 20]),
    ]
    notes = [dict(fragment_ids=[f["id"]], target_ids=[], kind="unknown") for f in fragments]
    result = join_note_lines(notes, fragments)
    assert len(result) == 1 and set(result[0]["fragment_ids"]) == {"f0", "f1"}
    ambiguous = fragments + [dict(id="f3", text="2) competing note", bbox=[10, 10, 100, 20])]
    result = join_note_lines(
        notes + [dict(fragment_ids=["f3"], target_ids=[], kind="unknown")], ambiguous
    )
    assert all("f1" not in n["fragment_ids"] for n in result)


@pytest.mark.parametrize("unnumbered_prefix", [False, True])
def test_grouped_numbered_notes_keep_their_own_continuations(unnumbered_prefix):
    from proofops.adapters.local.note_extraction import join_note_lines

    fragments = [
        dict(id="a", text="1) first note", bbox=[10, 10, 100, 20]),
        dict(id="b", text="first continuation", bbox=[14, 22, 100, 32]),
        dict(id="c", text="2) second note", bbox=[10, 34, 100, 44]),
        dict(id="d", text="second continuation", bbox=[14, 46, 100, 56]),
    ]
    ids, expected = ["a", "b", "c", "d"], [["a", "b"], ["c", "d"]]
    if unnumbered_prefix:
        fragments.insert(
            0, dict(id="p", text="Unnumbered scope qualification", bbox=[10, 0, 100, 8])
        )
        ids.insert(0, "p")
        expected.insert(0, ["p"])
    notes = [dict(fragment_ids=list(ids), target_ids=[], kind="unknown")]
    result = join_note_lines(notes, fragments)
    assert [n["fragment_ids"] for n in result] == expected
    assert notes[0]["fragment_ids"] == ids


def test_page_only_notes_are_source_bound_and_cannot_gain_ownership(tmp_path):
    import json

    from proofops.adapters.local.note_extraction import run
    from proofops.adapters.local.table_notes import (
        freeze_note_review,
        prepare,
        replay_note_reviews,
        validate,
    )
    from proofops.domain.numeric import unresolved_source_issue_ids

    from tests.acceptance.test_parsing import candidate

    source = pdf()
    batch = replace(
        candidate("opendataloader", [("p", "paragraph", "Source", (10, 10, 50, 30), ())]),
        source_sha256=sha256(source).hexdigest(),
    )
    graph = fuse_candidates((batch,), tenant_id=TENANT)
    packet = prepare(graph, source, [], tenant_id=TENANT, page=1)
    assert not packet["sources"] and not packet["layout_sources"]

    class Client:
        model = "synthetic-test"
        calls = 0

        def summary(self):
            return dict(calls=self.calls)

        def complete(self, instruction, content, **kwargs):
            self.calls += 1
            wire = json.loads(content)
            assert not wire["targets"]
            return dict(
                content=json.dumps(dict(notes=[dict(fragment_ids=[wire["fragments"][0][0]])])),
                provider_model=self.model,
            )

    client = Client()
    result = run(graph, source, [], client, tmp_path / "page", tenant_id=TENANT, page=1)
    assert client.calls == 1  # No binding request, even for an unnumbered native fragment.
    assert result["status"] == "source_bound_proposals"
    note = result["notes"][0]
    assert not note["target_source_refs"] and note["association_status"] == "unknown"
    assert not note["eligible_for_scoring"] and result["coverage_status"] == "unknown"
    artifact = freeze_note_review(graph, source, packet, result, tenant_id=TENANT)
    view = replay_note_reviews((artifact,), graph, source, tenant_id=TENANT)
    assert view.blocks == graph.blocks and view.edges == graph.edges
    assert unresolved_source_issue_ids(view, {graph.blocks[0].source_id})
    for page in (0, True, 4):
        with pytest.raises(ValueError):
            prepare(graph, source, [], tenant_id=TENANT, page=page)
    forged = deepcopy(packet)
    forged["untrusted_document_data"]["fragments"][0]["text"] = "invented"
    with pytest.raises(ValueError):
        validate(dict(notes=[]), forged, graph, source, tenant_id=TENANT)
    with pytest.raises(ValueError):
        validate(
            dict(notes=[dict(fragment_ids=note["fragment_ids"], target_ids=["c0"], kind="scope")]),
            packet,
            graph,
            source,
            tenant_id=TENANT,
        )


def test_note_proposals_bind_exact_page_fragments_and_targets_without_approving():
    from evaluation.table_notes import prepare, validate

    source = pdf()
    graph = fuse_candidates(
        (replace(table([["Metric"], ["Value"]]), source_sha256=sha256(source).hexdigest()),),
        tenant_id=TENANT,
    )
    tid = next(b.source_id for b in graph.blocks if b.kind == "table")
    packet = prepare(graph, source, [tid], tenant_id=TENANT)
    data = packet["untrusted_document_data"]
    fragment = data["fragments"][0]
    target = next(t for t in data["targets"] if t["kind"] == "table_cell")
    proposal = dict(fragment_ids=[fragment["id"]], target_ids=[target["id"]], kind="scope")
    payload = dict(notes=[proposal])
    result = validate(payload, packet, graph, source, tenant_id=TENANT)
    note = result["notes"][0]
    assert note["source_fragments"][0]["text"] == fragment["text"]
    assert note["target_source_refs"][0]["source_id"] == target["source_id"]
    assert note["association_status"] == "model_proposed"
    assert not note["eligible_for_scoring"] and result["decision"] is None
    assert result["unassigned_fragment_ids"] == [f["id"] for f in data["fragments"][1:]]
    assert (
        validate(dict(notes=[]), packet, graph, source, tenant_id=TENANT)["coverage_status"]
        == "unknown"
    )
    for bad in (
        dict(notes=[dict(proposal, target_ids=["invented"])]),
        dict(notes=[dict(proposal, fragment_ids=["invented"])]),
        dict(notes=[proposal, proposal]),
        dict(notes=[dict(proposal, kind="present")]),
        dict(notes=[dict(proposal, quote="invented")]),
    ):
        with pytest.raises(ValueError):
            validate(bad, packet, graph, source, tenant_id=TENANT)
    changed = deepcopy(packet)
    changed["untrusted_document_data"]["fragments"][0]["text"] = "forged"
    with pytest.raises(ValueError, match="identity"):
        validate(payload, changed, graph, source, tenant_id=TENANT)
    with pytest.raises(ValueError, match="source"):
        validate(payload, packet, graph, b"wrong PDF", tenant_id=TENANT)


def test_note_run_preserves_receipts_and_invalid_output_as_unknown(tmp_path):
    import json

    from evaluation.table_notes import run

    source = pdf()
    graph = fuse_candidates(
        (replace(table([["Metric"], ["Value"]]), source_sha256=sha256(source).hexdigest()),),
        tenant_id=TENANT,
    )
    tid = next(b.source_id for b in graph.blocks if b.kind == "table")

    class Client:
        model = "synthetic-test"
        calls = 0

        def summary(self):
            return dict(calls=self.calls)

        def complete(self, system, body, **kwargs):
            self.calls += 1
            assert "source_id" not in json.loads(body)["targets"][0]
            return dict(content='{"notes": [{"invented": true}]}', provider_model=self.model)

    client = Client()
    result = run(graph, source, [tid], client, tmp_path / "run", tenant_id=TENANT)
    assert result["status"] == "invalid_or_failed" and client.calls == 1
    assert result["coverage_status"] == "unknown" and result["notes"] == []
    assert (tmp_path / "run" / "response.json").exists()
    from pathlib import Path

    from proofops.adapters.local import table_layout_context, table_notes

    request = json.loads((tmp_path / "run" / "request.json").read_text())
    assert (
        request["validator_sha256"] == sha256(Path(table_notes.__file__).read_bytes()).hexdigest()
    )
    assert (
        request["layout_sha256"]
        == sha256(Path(table_layout_context.__file__).read_bytes()).hexdigest()
    )
    with pytest.raises(FileExistsError):
        run(graph, source, [tid], client, tmp_path / "run", tenant_id=TENANT)
    assert client.calls == 1


def test_unlocated_cells_remain_note_targets_with_explicit_location_uncertainty():
    from evaluation.table_notes import prepare

    source = pdf()
    batch = table([["Metric1"], ["Value"]])
    batch = replace(
        batch,
        source_sha256=sha256(source).hexdigest(),
        blocks=tuple(
            replace(b, source=replace(b.source, native_bbox=None)) if b.kind == "table_cell" else b
            for b in batch.blocks
        ),
    )
    graph = fuse_candidates((batch,), tenant_id=TENANT)
    tid = next(b.source_id for b in graph.blocks if b.kind == "table")
    packet = prepare(graph, source, [tid], tenant_id=TENANT)
    cells = [t for t in packet["untrusted_document_data"]["targets"] if t["kind"] == "table_cell"]
    assert {t["text"] for t in cells} == {"Metric1", "Value"}
    assert all(t["bbox"] is None for t in cells)
    assert all(packet["sources"][t["id"]]["location_quality"] == "unlocated" for t in cells)


def test_two_stage_notes_send_all_context_then_only_detected_notes(tmp_path):
    import json

    from evaluation.table_notes import run

    source = pdf()
    graph = fuse_candidates(
        (replace(table([["Metric"], ["Value"]]), source_sha256=sha256(source).hexdigest()),),
        tenant_id=TENANT,
    )
    tid = next(b.source_id for b in graph.blocks if b.kind == "table")

    class Client:
        model = "synthetic-test"
        calls = 0

        def summary(self):
            return dict(calls=self.calls)

        def complete(self, system, body, **kwargs):
            self.calls += 1
            wire = json.loads(body)
            if self.calls == 1:
                assert "styled_words" not in wire
                assert len(wire["fragments"][0][2]) == 4
                assert all(t["kind"] == "table" for t in wire["targets"])
                self.fragment = wire["fragments"][0][0]
                targets = []
            else:
                assert len(wire["fragments"]) == 1
                targets = [next(t["id"] for t in wire["targets"] if t["kind"] == "table_cell")]
            return dict(
                content=json.dumps(
                    dict(
                        notes=[dict(fragment_ids=[self.fragment])]
                        if self.calls == 1
                        else [dict(fragment_ids=[self.fragment], target_ids=targets, kind="scope")]
                    )
                ),
                provider_model=self.model,
            )

    client = Client()
    result = run(graph, source, [tid], client, tmp_path / "run", tenant_id=TENANT)
    assert client.calls == 2 and result["notes"][0]["target_source_refs"]
    assert len(result["requests"]) == 2
    assert (tmp_path / "run" / "binding-response.json").exists()


@pytest.mark.parametrize("suffix", ["", ")"])
@pytest.mark.parametrize("separator", [" ", ""])
def test_numbered_note_requires_unique_native_raised_marker_target(suffix, separator, tmp_path):
    from io import BytesIO

    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import DecodedStreamObject, NameObject

    from evaluation.table_notes import prepare, validate

    writer = PdfWriter()
    writer.append(PdfReader(BytesIO(pdf())))
    stream = DecodedStreamObject()
    stream.set_data(
        b"BT /F1 12 Tf 100 400 Td (Metric) Tj /F1 7 Tf 4 Ts (1"
        + suffix.replace(")", r"\)").encode()
        + b") Tj "
        + (
            f"0 Ts /F1 12 Tf 0 -40 Td (1.{separator}domestic only) Tj "
            "0 -20 Td (2. mean only) Tj ET"
        ).encode()
    )
    writer.pages[0][NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    source = output.getvalue()
    graph = fuse_candidates(
        (
            replace(
                table([["Metric1" + suffix], ["Value"]]), source_sha256=sha256(source).hexdigest()
            ),
        ),
        tenant_id=TENANT,
    )
    tid = next(b.source_id for b in graph.blocks if b.kind == "table")
    packet = prepare(graph, source, [tid], tenant_id=TENANT)
    data = packet["untrusted_document_data"]
    f = next(f for f in data["fragments"] if f["text"] == f"1.{separator}domestic only")
    wrong = next(t["id"] for t in data["targets"] if t["kind"] == "table")
    right = next(t["id"] for t in data["targets"] if t["text"] == "Metric1" + suffix)

    def payload(targets):
        return dict(notes=[dict(fragment_ids=[f["id"]], target_ids=targets, kind="scope")])

    other = next(x["id"] for x in data["fragments"] if x["id"] != f["id"])
    bypass = payload([wrong])
    bypass["notes"][0]["fragment_ids"].insert(0, other)
    with pytest.raises(ValueError, match="marker"):
        validate(bypass, packet, graph, source, tenant_id=TENANT)
    with pytest.raises(ValueError, match="marker"):
        validate(payload([wrong]), packet, graph, source, tenant_id=TENANT)
    assert validate(payload([right]), packet, graph, source, tenant_id=TENANT)["notes"][0][
        "target_ids"
    ] == [right]
    assert (
        validate(payload([]), packet, graph, source, tenant_id=TENANT)["notes"][0][
            "association_status"
        ]
        == "unknown"
    )

    # A missing marker target must not consume a second call that can only guess.
    import json

    from evaluation.table_notes import run

    unbound = fuse_candidates(
        (replace(table([["Other"], ["Value"]]), source_sha256=sha256(source).hexdigest()),),
        tenant_id=TENANT,
    )

    class Client:
        model = "synthetic-test"
        calls = 0

        def summary(self):
            return dict(calls=self.calls)

        def complete(self, system, body, **kwargs):
            self.calls += 1
            return dict(
                content=json.dumps(
                    dict(
                        notes=[
                            dict(
                                fragment_ids=[
                                    x["id"]
                                    for x in data["fragments"]
                                    if x["text"].startswith(("1.", "2."))
                                ]
                            )
                        ]
                    )
                ),
                provider_model=self.model,
            )

    client = Client()
    result = run(
        unbound,
        source,
        [b.source_id for b in unbound.blocks if b.kind == "table"],
        client,
        tmp_path / "unknown",
        tenant_id=TENANT,
    )
    assert client.calls == 1
    assert len(result["notes"]) == 2
    assert result["notes"][0]["association_status"] == "unknown"


def test_standalone_numeric_fragment_cannot_trigger_unrestricted_note_binding():
    from evaluation.table_notes import marker_targets

    packet = dict(untrusted_document_data=dict(fragments=[dict(id="f0", text="363")]))
    assert marker_targets(["f0"], packet) == []
    packet["untrusted_document_data"]["fragments"][0]["text"] = "단위: %"
    assert marker_targets(["f0"], packet) is None


def test_explicit_coverage_note_targets_unique_aligned_table_not_neighbor_metric():
    from evaluation.table_notes import marker_targets

    packet = dict(
        untrusted_document_data=dict(
            fragments=[dict(id="f0", text="데이터 커버리지 : 국내", bbox=[210, 90, 280, 96])],
            targets=[
                dict(id="left", source_id="L", kind="table"),
                dict(id="right", source_id="R", kind="table"),
                dict(id="metric", source_id="M", kind="table_cell"),
            ],
        ),
        layout_sources=[
            dict(table_id="L", bbox=[0, 100, 290, 200]),
            dict(table_id="R", bbox=[300, 100, 600, 200]),
        ],
    )
    assert marker_targets(["f0"], packet) == ["left"]
    packet["layout_sources"].append(dict(table_id="L2", bbox=[0, 300, 290, 400]))
    packet["untrusted_document_data"]["targets"].append(
        dict(id="left2", source_id="L2", kind="table")
    )
    assert marker_targets(["f0"], packet) == []


@pytest.mark.parametrize(
    "failure", [None, "transport", "outside", "overlap", "columns", "binding", "small"]
)
def test_oversized_discovery_preserves_fragments_and_deduplicates_overlap(tmp_path, failure):
    import json
    from io import BytesIO

    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import DecodedStreamObject, NameObject

    from evaluation.table_notes import run

    writer = PdfWriter()
    writer.append(PdfReader(BytesIO(pdf())))
    stream = DecodedStreamObject()
    lines = [f"Line {i}" for i in range(20)]
    lines[10] = "domestic only" if failure == "binding" else "1. domestic only"
    selected_text = lines[10]
    if failure == "small":
        lines = ["Line 0", selected_text, "Line 2", "Line 3"]
    stream.set_data(
        (
            "BT /F1 12 Tf 100 750 Td " + " 0 -20 Td ".join(f"({line}) Tj" for line in lines) + " ET"
        ).encode()
    )
    if failure == "columns":
        stream.set_data(
            " ".join(
                f"BT /F1 12 Tf 1 0 0 1 {50 if i < 10 else 350} {750 - (i % 10) * 20} "
                f"Tm ({line}) Tj ET"
                for i, line in enumerate(lines)
            ).encode()
        )
    writer.pages[0][NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    source = output.getvalue()
    batch = replace(table([["Metric"], ["Value"]]), source_sha256=sha256(source).hexdigest())
    batches = [batch]
    if failure == "columns":
        from tests.acceptance.test_parsing import candidate

        batches[0] = replace(
            batch,
            blocks=tuple(
                replace(block, source=replace(block.source, native_bbox=(0, 0, 290, 790)))
                if block.kind == "table"
                else block
                for block in batch.blocks
            ),
        )
        batches.append(
            replace(
                candidate("B", [("R", "table", "Right", (310, 0, 590, 790), ())]),
                source_sha256=sha256(source).hexdigest(),
            )
        )
    graph = fuse_candidates(tuple(batches), tenant_id=TENANT)

    class Client:
        model = "synthetic-test"
        calls = 0
        seen = set()

        def summary(self):
            return dict(calls=self.calls)

        def complete(self, system, body, **kwargs):
            wire = json.loads(body)
            if failure == "transport":
                raise ValueError("UPSTREAM_UNAVAILABLE")
            if len(wire["fragments"]) > (2 if failure == "small" else 12):
                raise ValueError("PROBE_REQUEST_TOO_LARGE")
            self.calls += 1
            self.seen.update(f[0] for f in wire["fragments"])
            notes = [dict(fragment_ids=[f[0]]) for f in wire["fragments"] if f[1] == selected_text]
            if "detected_notes" in wire:
                notes = [dict(n, target_ids=[], kind="unknown") for n in notes]
            if failure == "outside":
                notes = [dict(fragment_ids=["f19"])]
            if failure == "overlap" and self.calls == 1:
                notes = [dict(fragment_ids=["f9", "f10"])]
            return dict(content=json.dumps(dict(notes=notes)), provider_model=self.model)

    client = Client()
    result = run(
        graph,
        source,
        [b.source_id for b in graph.blocks if b.kind == "table"],
        client,
        tmp_path / "run",
        tenant_id=TENANT,
    )
    if failure not in (None, "columns", "binding", "small"):
        assert result["status"] == "invalid_or_failed"
        assert result["coverage_status"] == "unknown"
        assert client.calls <= 2
        assert (
            result["error"]
            == {
                "transport": "UPSTREAM_UNAVAILABLE",
                "outside": "note cites fragments outside this request",
                "overlap": "discovery overlap disagrees on note extent",
            }[failure]
        )
        return
    assert result["status"] == "source_bound_proposals"
    assert client.calls == (2 if failure == "columns" else 5 if failure == "binding" else 4)
    assert client.seen == {f"f{i}" for i in range(len(lines))}
    assert len(result["notes"]) == 1
    assert result["notes"][0]["source_fragments"][0]["text"] == selected_text
    assert result["coverage_status"] == "unknown"
    assert result["notes"][0]["association_status"] == "unknown"
    assert len(list((tmp_path / "run").glob("*response.json"))) == client.calls


def test_native_hanging_note_lines_join_without_crossing_columns_or_next_numbers():
    from evaluation.table_notes import join_note_lines

    fragments = [
        dict(id="f0", text="1) rounded", bbox=[10, 100, 200, 106]),
        dict(id="f1", text="2) other column", bbox=[310, 100, 500, 106]),
        dict(id="f2", text="domestic only", bbox=[15, 108, 200, 114]),
        dict(id="f3", text="boundary unchanged", bbox=[15, 116, 200, 122]),
        dict(id="f4", text="2) restated", bbox=[10, 124, 200, 130]),
        dict(id="f5", text="2024 1)", bbox=[10, 10, 30, 16]),
        dict(id="f6", text="unrelated nonindented", bbox=[10, 132, 200, 138]),
    ]
    notes = [dict(fragment_ids=[f["id"]], target_ids=[], kind="unknown") for f in fragments]
    result = join_note_lines(notes, fragments)
    assert [n["fragment_ids"] for n in result] == [["f0", "f2", "f3"], ["f1"], ["f4"], ["f6"]]
    assert all(n["target_ids"] == [] and n["kind"] == "unknown" for n in result)


@pytest.mark.parametrize("cell_text", ["1)Metric", "prefix 1) Metric", "Metric 1)"])
def test_repeated_separate_markers_are_scoped_to_the_notes_table_column(cell_text):
    from evaluation.table_notes import marker_targets

    packet = dict(
        untrusted_document_data=dict(
            fragments=[dict(id="f", text="1) domestic only", bbox=[10, 200, 180, 206])],
            targets=[
                dict(id="a", kind="table_cell", table_id="L", text=cell_text),
                dict(id="b", kind="table_cell", table_id="R", text=cell_text),
            ],
        ),
        layout_sources=[
            dict(
                table_id=tid,
                bbox=[x, 0, x + 200, 190],
                styled_words=[],
                clipped_word_indices=[],
                separate_markers=[dict(marker=dict(text="1)"), base=dict(text="Metric"))],
            )
            for tid, x in (("L", 0), ("R", 300))
        ],
    )
    assert marker_targets(["f"], packet) == ["a"]
    packet["untrusted_document_data"]["fragments"][0]["bbox"] = [310, 200, 480, 206]
    assert marker_targets(["f"], packet) == ["b"]
    packet["untrusted_document_data"]["fragments"][0]["bbox"] = [10, 200, 480, 206]
    assert marker_targets(["f"], packet) == []
    packet["untrusted_document_data"]["fragments"][0]["bbox"] = [10, 200, 180, 206]
    packet["untrusted_document_data"]["targets"].append(
        dict(id="duplicate", kind="table_cell", table_id="L", text=cell_text)
    )
    assert marker_targets(["f"], packet) == []
    packet["untrusted_document_data"]["targets"] = packet["untrusted_document_data"]["targets"][:1]
    packet["layout_sources"] = packet["layout_sources"][:1]
    packet["untrusted_document_data"]["fragments"][0]["bbox"] = [10, 200, 480, 206]
    assert marker_targets(["f"], packet) == []
    # A repeated marker far below another table cannot borrow this table's marker.
    packet["untrusted_document_data"]["fragments"][0]["bbox"] = [10, 500, 180, 506]
    assert marker_targets(["f"], packet) == []


@pytest.mark.parametrize("text", ["11)Metric", "SubMetric1)"])
def test_separate_marker_cannot_match_another_marker_number_or_word_suffix(text):
    from evaluation.table_notes import marker_targets

    packet = dict(
        untrusted_document_data=dict(
            fragments=[dict(id="f", text="1) domestic only", bbox=[10, 200, 180, 206])],
            targets=[dict(id="a", kind="table_cell", table_id="L", text=text)],
        ),
        layout_sources=[
            dict(
                table_id="L",
                bbox=[0, 0, 200, 190],
                styled_words=[],
                clipped_word_indices=[],
                separate_markers=[dict(marker=dict(text="1)"), base=dict(text="Metric"))],
            )
        ],
    )
    assert marker_targets(["f"], packet) == []


def test_conflicting_table_does_not_hide_original_page_note_context(tmp_path):
    from proofops.adapters.local.table_layout_context import table_layout_context
    from proofops.adapters.local.table_notes import prepare, validate

    source = pdf()
    a = replace(table([["Metric"], ["Value"]]), source_sha256=sha256(source).hexdigest())
    b = table([["Metric"], ["Other"]], parser="B")
    b = replace(
        b,
        source_sha256=a.source_sha256,
        blocks=tuple(
            replace(c, source=replace(c.source, raw_text="Different table", char_end=15))
            if c.kind == "table"
            else c
            for c in b.blocks
        ),
    )
    graph = fuse_candidates((a, b), tenant_id=TENANT)
    tables = [t for t in graph.blocks if t.kind == "table"]
    assert len(tables) == 1 and tables[0].winner is None
    assert any(i.kind == "parse_conflict" for i in graph.issues)
    table_ids = [t.source_id for t in tables]
    with pytest.raises(ValueError, match="unconflicted"):
        table_layout_context(graph, source, table_ids, tenant_id=TENANT)
    packet = prepare(graph, source, table_ids, tenant_id=TENANT)
    data = packet["untrusted_document_data"]
    assert data["targets"] == [] and packet["sources"] == {}
    assert set(data["unresolved_source_ids"]) == {b.source_id for b in graph.blocks}
    assert packet["layout_sources"][0]["bbox"] is None
    fragment = data["fragments"][0]
    assert "emissions" in fragment["text"]
    result = validate(
        {"notes": [{"fragment_ids": [fragment["id"]], "target_ids": [], "kind": "unknown"}]},
        packet,
        graph,
        source,
        tenant_id=TENANT,
    )
    assert result["notes"][0]["association_status"] == "unknown"
    assert result["coverage_status"] == "unknown" and result["decision"] is None
    assert tables[0].winner is None
    with pytest.raises(ValueError, match="source"):
        prepare(graph, source + b"tampered", table_ids, tenant_id=TENANT)
    # New unresolved packets cannot claim the old, resolved-only implementation profile.
    import json

    from proofops.adapters.local.table_notes import freeze_note_review, replay_note_reviews
    from proofops.domain.provenance import canonical_hash
    from proofops.domain.rulepacks import canonical_json

    artifact = freeze_note_review(graph, source, packet, result, tenant_id=TENANT)
    view = replay_note_reviews((artifact,), graph, source, tenant_id=TENANT)
    assert view.blocks == graph.blocks and set(graph.issues) <= set(view.issues)
    body = json.loads(artifact)
    body["validator_sha256"] = "9d38885dc178d90e5a1fec071f9282262d51b17b019fbc948f87222ac93101b1"
    body["layout_sha256"] = "d7bc170774fe9019ef2b2573d7e25a0a14163317ed222fc8558008a43622d15e"
    body["artifact_sha256"] = canonical_hash(
        {k: v for k, v in body.items() if k != "artifact_sha256"}
    )
    with pytest.raises(ValueError, match="mismatch"):
        replay_note_reviews((canonical_json(body),), graph, source, tenant_id=TENANT)

    from types import SimpleNamespace

    from proofops.adapters.local.note_extraction import run

    calls = []

    def complete(instruction, content, **kwargs):
        calls.append(json.loads(content))
        assert calls[-1]["fragments"][0][0] == "n0"
        return {"content": '{"notes":[{"fragment_ids":[" n0 "]}]}', "provider_model": "synthetic"}

    client = SimpleNamespace(model="synthetic", summary=lambda: {}, complete=complete)
    extracted = run(graph, source, table_ids, client, tmp_path / "discovery", tenant_id=TENANT)
    assert extracted["notes"][0]["fragment_ids"] == [fragment["id"]]
    assert len(calls) == 1
    assert calls[0]["targets"]
    assert all(t["location_status"] == "unresolved_candidate" for t in calls[0]["targets"])
    assert all(t["bbox"] is not None for t in calls[0]["targets"])
    assert packet["sources"] == {}  # Hypotheses must not become source-ref targets.

    for wrong in (fragment["id"], "n999"):
        client.complete = lambda *args, **kwargs: {
            "content": json.dumps({"notes": [{"fragment_ids": [wrong]}]}),
            "provider_model": "synthetic",
        }
        rejected = run(graph, source, table_ids, client, tmp_path / wrong, tenant_id=TENANT)
        assert rejected["status"] == "invalid_or_failed" and rejected["notes"] == []


@pytest.mark.parametrize("unresolved", [True, False])
@pytest.mark.parametrize("crop", [(50, 100, 550, 750), (0, 0, 550, 750)])
def test_native_note_context_rejects_actual_cropped_page(unresolved, crop):
    from io import BytesIO

    from proofops.adapters.local.table_notes import prepare
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import RectangleObject

    writer = PdfWriter()
    writer.add_page(PdfReader(BytesIO(pdf())).pages[0])
    writer.pages[0].cropbox = RectangleObject(crop)
    buffer = BytesIO()
    writer.write(buffer)
    source = buffer.getvalue()
    graph = fuse_candidates(
        (replace(table([["Metric"], ["Value"]]), source_sha256=sha256(source).hexdigest()),),
        tenant_id=TENANT,
    )
    if unresolved:
        graph = replace(
            graph,
            blocks=tuple(replace(b, winner=None) if b.kind == "table" else b for b in graph.blocks),
        )
    with pytest.raises(ValueError, match="geometry unsupported"):
        prepare(
            graph,
            source,
            [b.source_id for b in graph.blocks if b.kind == "table"],
            tenant_id=TENANT,
        )


def test_nested_table_cells_are_in_note_inventory_with_legacy_replay():
    from proofops.adapters.local.table_notes import prepare, validate

    source = pdf()
    batch = table([["Row"], ["Value"]])
    batch = replace(
        batch,
        source_sha256=sha256(source).hexdigest(),
        blocks=tuple(
            replace(b, kind="table_row") if b.source.source_native_id == "r0c0" else b
            for b in batch.blocks
        ),
        edges=tuple(
            replace(e, target_native_id="r0c0") if e.source_native_id == "r1c0" else e
            for e in batch.edges
        ),
    )
    graph = fuse_candidates((batch,), tenant_id=TENANT)
    tid = next(b.source_id for b in graph.blocks if b.kind == "table")
    cell = next(b for b in graph.blocks if b.kind == "table_cell")
    current = prepare(graph, source, [tid], tenant_id=TENANT)
    targets = current["untrusted_document_data"]["targets"]
    assert any(t["source_id"] == cell.source_id and t["table_id"] == tid for t in targets)
    assert current["contract"]["version"] == 3
    old = prepare(graph, source, [tid], tenant_id=TENANT, contract_version=2)
    assert cell.source_id not in {t["source_id"] for t in old["untrusted_document_data"]["targets"]}
    assert (
        validate({"notes": []}, old, graph, source, tenant_id=TENANT)["coverage_status"]
        == "unknown"
    )
    parent = next(b for b in graph.blocks if b.kind == "table_row")
    conflicted = replace(
        graph,
        blocks=tuple(
            replace(b, winner=None, quality="conflicted") if b == parent else b
            for b in graph.blocks
        ),
    )
    pending = prepare(conflicted, source, [tid], tenant_id=TENANT)["untrusted_document_data"]
    assert cell.source_id in pending["unresolved_source_ids"]
    assert cell.source_id not in {t["source_id"] for t in pending["targets"]}
    for unsupported in (True, 1, 4, "3"):
        with pytest.raises(ValueError):
            prepare(graph, source, [tid], tenant_id=TENANT, contract_version=unsupported)


def test_old_validator_pin_cannot_claim_new_target_inventory():
    import json

    from proofops.adapters.local.table_notes import (
        freeze_note_review,
        prepare,
        replay_note_reviews,
        validate,
    )
    from proofops.domain.provenance import canonical_hash
    from proofops.domain.rulepacks import canonical_json

    source = pdf()
    graph = fuse_candidates(
        (replace(table([["Metric"], ["Value"]]), source_sha256=sha256(source).hexdigest()),),
        tenant_id=TENANT,
    )
    tid = next(b.source_id for b in graph.blocks if b.kind == "table")
    for version in (2, 3):
        packet = prepare(graph, source, [tid], tenant_id=TENANT, contract_version=version)
        result = validate({"notes": []}, packet, graph, source, tenant_id=TENANT)
        raw = json.loads(freeze_note_review(graph, source, packet, result, tenant_id=TENANT))
        raw.pop("artifact_sha256")
        raw["validator_sha256"] = "1b705722e36c0860ef19728743ef8167c7c2ee78cd8f4d59bdae6d6c24d27994"
        artifact = canonical_json({**raw, "artifact_sha256": canonical_hash(raw)})
        if version == 2:
            assert replay_note_reviews((artifact,), graph, source, tenant_id=TENANT).issues
        else:
            with pytest.raises(ValueError, match="artifact mismatch"):
                replay_note_reviews((artifact,), graph, source, tenant_id=TENANT)


@pytest.mark.parametrize("shape", ["cycle", "two_rows", "orphan", "non_row"])
def test_note_inventory_keeps_invalid_nested_ancestry_unresolved(shape):
    from proofops.adapters.local.table_notes import prepare

    source = pdf()
    batch = table([["R1"], ["R2"], ["Cell"]])
    blocks = tuple(
        replace(b, kind="table_row") if b.source.source_native_id in ("r0c0", "r1c0") else b
        for b in batch.blocks
    )
    edge = batch.edges[0]

    links = [("r0c0", "T"), ("r1c0", "T"), ("r2c0", "r1c0")]
    if shape == "cycle":
        links = [("r0c0", "T"), ("r0c0", "r1c0"), ("r1c0", "r0c0"), ("r2c0", "r1c0")]
    elif shape == "two_rows":
        links.append(("r2c0", "r0c0"))
    elif shape == "orphan":
        links = [("r0c0", "r1c0"), ("r1c0", "r0c0"), ("r2c0", "r1c0")]
    else:
        blocks = tuple(
            replace(b, kind="paragraph") if b.source.source_native_id == "r1c0" else b
            for b in blocks
        )
    batch = replace(
        batch,
        blocks=blocks,
        source_sha256=sha256(source).hexdigest(),
        edges=tuple(replace(edge, source_native_id=a, target_native_id=b) for a, b in links),
    )
    graph = fuse_candidates((batch,), tenant_id=TENANT)
    tid = next(b.source_id for b in graph.blocks if b.kind == "table")
    cell = next(b for b in graph.blocks if b.kind == "table_cell")
    data = prepare(graph, source, [tid], tenant_id=TENANT)["untrusted_document_data"]
    assert cell.source_id in data["unresolved_source_ids"]
    assert cell.source_id not in {t["source_id"] for t in data["targets"]}


def test_compact_note_marker_grammar_does_not_reinterpret_decimal_values():
    from proofops.adapters.local.table_notes import NUMBERED_NOTE_START

    assert NUMBERED_NOTE_START.match("1.2024년부터 산정 범위 변경")[1] == "1"
    assert NUMBERED_NOTE_START.match("2)국내 사업장만 포함")[1] == "2"
    assert NUMBERED_NOTE_START.match("1.5 tCO2eq") is None
    assert NUMBERED_NOTE_START.match("2024년 배출량") is None


def test_explicit_coverage_survives_model_omission_without_inventing_a_binding():
    from proofops.adapters.local.note_extraction import join_note_lines

    fragments = [
        dict(id="a", text="데이터 커버리지 : 국내+해외 생산공장", bbox=[10, 10, 190, 16]),
        dict(id="b", text="Data coverage: overseas sites", bbox=[310, 10, 490, 16]),
        dict(id="c", text="We improve data coverage", bbox=[10, 30, 190, 36]),
    ]
    existing = [dict(fragment_ids=["a"], target_ids=[], kind="unknown")]
    notes = join_note_lines(existing, fragments)
    assert notes == [
        dict(fragment_ids=["a"], target_ids=[], kind="unknown"),
        dict(fragment_ids=["b"], target_ids=[], kind="unknown"),
    ]
