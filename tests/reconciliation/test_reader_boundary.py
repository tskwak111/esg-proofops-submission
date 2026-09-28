"""The coordinator exercises both the rich reader and frozen callable fallback."""

import hashlib

import pytest
from proofops.adapters.reconciliation import FileSourceReader
from proofops.application.reconciliation.sources import validate_source_bytes


@pytest.mark.parametrize("adapter", [False, True])
@pytest.mark.parametrize(
    "format_name,payload,quote",
    [
        ("xml", '<!DOCTYPE r [<!ENTITY e "forged">]><r id="x">&e;</r>'.encode("utf-16"), "forged"),
        ("xml", b'<r><a id="x">first</a><b id="x">second</b></r>', "first"),
        ("html", b'<html><p id="x">first</p><p id="x">second</p></html>', "first"),
        ("html", b'<html><div id="x">inside<br></div><p>outside</p></html>', "outside"),
    ],
)
def test_ambiguous_or_expanded_source_is_rejected(tmp_path, adapter, format_name, payload, quote):
    ref = {
        "source_id": "s",
        "document_id": "d",
        "artifact_sha256": hashlib.sha256(payload).hexdigest(),
        "locator": "id:x",
        "quote": quote,
    }
    (tmp_path / "source").write_bytes(payload)
    reader = FileSourceReader(
        tmp_path, {"d": {"path": "source", "format": format_name, "sha256": ref["artifact_sha256"]}}
    )
    with pytest.raises(ValueError):
        if adapter:
            reader.validate(ref)
        else:
            validate_source_bytes(payload, ref)


def test_void_tags_do_not_hide_valid_text():
    validate_source_bytes(
        b'<html><p id="x">inside<br>still inside</p></html>',
        {"locator": "id:x", "quote": "still inside"},
    )
