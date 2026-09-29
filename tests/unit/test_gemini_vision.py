import json
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pdfplumber
from proofops.adapters.parsing.gemini_vision import (
    _ground,
    _request,
    fragment,
    parse_pages,
    weak_pages,
)


def test_exact_grounding_and_fragment_checks():
    words = [
        dict(text="2035년까지", x0=1, top=1, x1=40, bottom=11),
        dict(text="20%", x0=42, top=1, x1=60, bottom=11),
        dict(text="감축한다.", x0=1, top=12, x1=55, bottom=22),
    ]
    assert _ground("2035년까지 20% 감축한다.", words)[0] == (1, 1, 60, 22)
    assert _ground("2036년까지 20% 감축한다.", words)[0] is None
    assert _ground("2035년까지 30% 감축한다.", words)[0] is None
    assert fragment("목표는")
    assert not fragment("배출량을 감축한다.")


def test_request_shape(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured.update(json.loads(request.data))
        assert request.get_header("Authorization") == "Bearer test"
        assert timeout == 120
        return BytesIO(b'{"choices":[{"message":{"content":"{\\"blocks\\":[]}"}}]}')

    monkeypatch.setattr("proofops.adapters.parsing.gemini_vision.urlopen", fake_urlopen)
    assert _request(b"PNG", "test")["blocks"] == []
    assert captured["model"] == "google/gemini-3.8-flash"
    assert captured["temperature"] == 0
    assert captured["max_tokens"] == 16384
    assert captured["response_format"] == {"type": "json_object"}
    assert captured["messages"][0]["content"][1]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )


def test_fake_provider_cache_and_vision_only(tmp_path: Path):
    # A real one-page PDF keeps native glyph extraction and rendering in the loop.
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    pdf = tmp_path / "source.pdf"
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    content_stream = DecodedStreamObject()
    content_stream.set_data(b"BT /F1 12 Tf 50 200 Td (2035 target) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(content_stream)
    with pdf.open("wb") as output:
        writer.write(output)
    content = pdf.read_bytes()
    from hashlib import sha256
    from uuid import uuid4

    from proofops.application.ingest.graph_fusion import (
        ParserProfile,
        SourceArtifact,
        fuse_candidates,
    )

    source = SourceArtifact(
        str(uuid4()), str(uuid4()), str(uuid4()), sha256(content).hexdigest(), "v1", content
    )
    profile = ParserProfile(str(uuid4()), physical_pages=(1,), vision_parse="all")
    graph = SimpleNamespace(blocks=(), issues=())
    calls = []

    def fake(image, key):
        calls.append((image[:8], key))
        if len(calls) == 1:
            raise json.JSONDecodeError("incomplete", "", 0)
        return {
            "blocks": [
                {"type": "paragraph", "text": "2035 target", "section": "Goal"},
                {"type": "paragraph", "text": "9999 invention", "section": "Goal"},
                {"type": "table", "text": "", "section": "Goal", "rows": [["2035", "target"]]},
            ],
            "usage": {"cost": 0.01},
        }

    batch, metrics = parse_pages(
        source, profile, graph, (1,), tmp_path / "cache", key="test", request=fake
    )
    assert len(batch.blocks) == 6
    assert batch.blocks[0].source.native_bbox is not None
    assert batch.blocks[1].source.native_bbox is None
    assert {b.kind for b in batch.blocks[2:]} == {"table_cell", "table_row", "table"}
    assert all(b.source.native_bbox is not None for b in batch.blocks[2:])
    fused = fuse_candidates((batch,), tenant_id=source.tenant_id)
    assert any(block.quality == "unlocated" for block in fused.blocks)
    assert metrics["pages"][0]["vision_only"] == 1
    assert metrics["pages"][0]["numbers_rejected"] == 1
    assert metrics["vision_only"][0]["text"] == "9999 invention"
    assert calls == [(b"\x89PNG\r\n\x1a\n", "test")] * 2
    _, cached = parse_pages(
        source,
        profile,
        graph,
        (1,),
        tmp_path / "cache",
        request=lambda *_: (_ for _ in ()).throw(AssertionError()),
    )
    assert cached["pages"][0]["cache_hit"]
    with pdfplumber.open(pdf) as document:
        assert weak_pages(graph, (1,), document.pages) == [1]
