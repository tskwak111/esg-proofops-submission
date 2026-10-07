import io
from hashlib import sha256
from uuid import uuid4

from proofops.adapters.parsing.upstage_document import candidate_batch, selected_pdf
from proofops.application.ingest.graph_fusion import ParserProfile, SourceArtifact
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject


def test_upstage_coordinates_and_native_cells_are_grounded():
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
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 50 200 Td (2035 target) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    content = output.getvalue()
    source = SourceArtifact(
        str(uuid4()), str(uuid4()), str(uuid4()), sha256(content).hexdigest(), "v1", content
    )
    profile = ParserProfile(str(uuid4()), physical_pages=(1,), parser_mode="upstage")
    coords = [
        {"x": 0.1, "y": 0.2},
        {"x": 0.5, "y": 0.2},
        {"x": 0.5, "y": 0.5},
        {"x": 0.1, "y": 0.5},
    ]
    response = {
        "model": "document-parse-260128",
        "usage": {"pages": 1, "standard": [1]},
        "elements": [
            {
                "page": 1,
                "category": "paragraph",
                "coordinates": coords,
                "content": {"text": "2035 target"},
            },
            {
                "page": 1,
                "category": "table",
                "coordinates": coords,
                "content": {
                    "text": "",
                    "html": "<table><tr><td>2035</td>" "<td>target</td></tr></table>",
                },
            },
            {
                "page": 1,
                "category": "paragraph",
                "coordinates": coords,
                "content": {"text": "2036 target"},
            },
        ],
    }
    assert selected_pdf(content, (1,)).startswith(b"%PDF")
    batch, metrics = candidate_batch(
        source, profile, (1,), [response], mode="standard", config_hash="0" * 64
    )
    assert metrics["cells"] == metrics["grounded_cells"] == 2
    assert metrics["vision_only"] == 1
    assert sum(block.source.native_bbox is None for block in batch.blocks) == 1
    assert len(batch.edges) == 2
