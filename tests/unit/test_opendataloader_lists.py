"""OD list items can contain ordinary continuation text; do not silently lose it."""

from proofops.adapters.parsing.opendataloader import _batch
from proofops.application.ingest.graph_fusion import ParserProfile, fuse_candidates

from tests.acceptance.test_parsing import MANIFEST, TENANT, source


def test_list_item_text_reaches_canonical_paragraph_with_native_identity_and_parent():
    text = "시민 참여 프로그램을 통해 환경 교육 활동을 수행하고 있습니다."
    raw = {
        "kids": [
            {
                "id": 1,
                "type": "list",
                "page number": 1,
                "bounding box": [10, 10, 300, 50],
                "list items": [
                    {
                        "id": 2,
                        "type": "list item",
                        "bounding box": [10, 10, 300, 50],
                        "content": text,
                        "kids": [],
                    }
                ],
            }
        ]
    }
    geometry = {
        "1": {
            "crop": {"width_pt": 600, "height_pt": 800, "rotation": 0, "crop_box": [0, 0, 600, 800]}
        }
    }
    batch = _batch(raw, geometry, source(b"synthetic bytes"), ParserProfile(MANIFEST), "b" * 64)
    graph = fuse_candidates((batch,), tenant_id=TENANT)
    found = [b for b in graph.blocks if b.raw_text == text]
    assert len(found) == 1
    block = found[0]
    assert block.kind == "paragraph"
    assert block.source_ref().quote == text
    assert block.source_ref().verification_state == "candidate"
    assert block.sources[0].source_native_id == "2"
    parent = next(b for b in graph.blocks if b.sources[0].source_native_id == "1")
    assert any(
        e.source_id == block.source_id
        and e.target_id == parent.source_id
        and e.relation == "section_parent"
        for e in graph.edges
    )
