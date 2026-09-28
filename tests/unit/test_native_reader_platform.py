"""Unsupported hosts must not invoke the Apple Vision reader or compile Swift."""

import pytest
from proofops.adapters.local import source_verification


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_unavailable_apple_vision_has_a_stable_receipt_without_process_launch(
    monkeypatch, platform
):
    monkeypatch.setattr(source_verification.sys, "platform", platform)

    def forbidden(*args, **kwargs):
        raise AssertionError("Apple Vision cannot execute on this platform")

    monkeypatch.setattr(source_verification.subprocess, "run", forbidden)
    expected = {
        "status": "unresolved",
        "reason": "rendered_reader_unavailable",
        "error": "UnsupportedPlatform",
    }
    assert source_verification._rendered_text(None, None) == expected
    assert source_verification._rendered_text(None, None) == expected
