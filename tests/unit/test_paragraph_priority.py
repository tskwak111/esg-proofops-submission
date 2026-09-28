"""Bounded extraction/OCR selection must not depend on run-derived source UUIDs."""

from types import SimpleNamespace


def test_prose_priority_is_stable_under_source_id_changes():
    from proofops_worker.extract_runner import paragraph_priority

    prose = "The company reduced emissions and monitors the implementation of its climate plan."
    values = [
        SimpleNamespace(
            source_id="000", normalized_text="Signature", page_num=3, bbox=(1, 2, 3, 4)
        ),
        SimpleNamespace(source_id="999", normalized_text=prose, page_num=2, bbox=(1, 2, 3, 4)),
        SimpleNamespace(source_id="111", normalized_text=prose, page_num=1, bbox=(1, 2, 3, 4)),
    ]
    expected = [(1, prose), (2, prose), (3, "Signature")]
    assert [
        (b.page_num, b.normalized_text) for b in sorted(values, key=paragraph_priority)
    ] == expected
    for index, block in enumerate(values):
        block.source_id = str(100 - index)
    assert [
        (b.page_num, b.normalized_text) for b in sorted(reversed(values), key=paragraph_priority)
    ] == expected
