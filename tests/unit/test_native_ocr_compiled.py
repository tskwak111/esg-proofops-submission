"""Unit tests for the standalone compiled-binary OCR speed helper.

Scope: exercises `native_ocr_compiled.py` only. Does not modify or call into
any pinned reader/policy file. Skipped outside macOS or without a usable
Swift toolchain, matching the platform guard already used by the pinned
`_rendered_text` reader.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import proofops.adapters.local.native_ocr_compiled as native_ocr_compiled
import pytest
from proofops.adapters.local.native_ocr_compiled import ensure_compiled_binary

_NATIVE_OCR_SOURCE = Path(__file__).resolve().parents[2] / (
    "packages/proofops/adapters/local/native_ocr.swift"
)
_DEVELOPER_DIR = os.environ.get("DEVELOPER_DIR", "/Library/Developer/CommandLineTools")


def _toolchain_available() -> bool:
    if sys.platform != "darwin" or not _NATIVE_OCR_SOURCE.is_file():
        return False
    return shutil.which("swiftc") is not None


pytestmark = pytest.mark.skipif(
    not _toolchain_available(), reason="requires macOS + Swift toolchain (DEVELOPER_DIR)"
)


@pytest.fixture()
def sample_image(tmp_path):
    from PIL import Image

    path = tmp_path / "sample.png"
    Image.new("RGB", (40, 40), "white").save(path)
    return path


@pytest.fixture(autouse=True)
def _reset_default_cache_dir():
    # The default (no explicit cache_dir) path is process-global; isolate it
    # per test so tests cannot see each other's compiled binaries.
    native_ocr_compiled._cache_dir = None
    yield
    native_ocr_compiled._cache_dir = None


def test_second_identical_call_reuses_binary_without_recompiling(tmp_path):
    cache_dir = tmp_path / "cache"
    first = ensure_compiled_binary(
        _NATIVE_OCR_SOURCE, developer_dir=_DEVELOPER_DIR, cache_dir=cache_dir
    )
    assert first is not None and first.is_file()
    before_mtime = first.stat().st_mtime_ns

    with patch("subprocess.run", wraps=subprocess.run) as spy:
        second = ensure_compiled_binary(
            _NATIVE_OCR_SOURCE, developer_dir=_DEVELOPER_DIR, cache_dir=cache_dir
        )
        compile_calls = [call for call in spy.call_args_list if "-O" in call.args[0]]
        assert compile_calls == []  # identity lookups may run; no recompilation happens

    assert second == first
    assert second.stat().st_mtime_ns == before_mtime

    leftovers = list(cache_dir.glob("*.tmp")) + list(cache_dir.glob("source-*"))
    assert leftovers == []


def test_default_cache_dir_also_reuses_binary_across_calls_in_process():
    """Without an explicit cache_dir, the process-private default is reused."""
    first = ensure_compiled_binary(_NATIVE_OCR_SOURCE, developer_dir=_DEVELOPER_DIR)
    assert first is not None and first.is_file()

    with patch("subprocess.run", wraps=subprocess.run) as spy:
        second = ensure_compiled_binary(_NATIVE_OCR_SOURCE, developer_dir=_DEVELOPER_DIR)
        compile_calls = [call for call in spy.call_args_list if "-O" in call.args[0]]
        assert compile_calls == []

    assert second == first


def test_changed_source_bytes_invalidate_the_cached_binary(tmp_path):
    cache_dir = tmp_path / "cache"
    original = ensure_compiled_binary(
        _NATIVE_OCR_SOURCE, developer_dir=_DEVELOPER_DIR, cache_dir=cache_dir
    )
    assert original is not None

    mutated_source = tmp_path / "native_ocr.swift"
    mutated_source.write_text(_NATIVE_OCR_SOURCE.read_text() + "\n// mutated\n")
    mutated = ensure_compiled_binary(
        mutated_source, developer_dir=_DEVELOPER_DIR, cache_dir=cache_dir
    )

    assert mutated is not None
    assert mutated != original


def test_changed_compiler_identity_invalidates_the_cached_binary(tmp_path):
    """A different resolved-toolchain identity must not reuse a cached binary.

    Mocks only the identity probe (`xcrun --find swiftc` / `swiftc --version`
    output) rather than requiring a second real, licensed toolchain — this
    machine's Xcode.app install is unlicensed and must not be invoked.
    """
    cache_dir = tmp_path / "cache"
    first = ensure_compiled_binary(
        _NATIVE_OCR_SOURCE, developer_dir=_DEVELOPER_DIR, cache_dir=cache_dir
    )
    assert first is not None

    real_run = subprocess.run

    def fake_run(args, **kwargs):
        if args[:2] == ["swiftc", "--version"] or (
            isinstance(args, list) and args and args[0].endswith("swiftc") and "--version" in args
        ):
            fake = real_run(args, **kwargs)
            fake.stdout = fake.stdout + b" (mocked-different-identity)"
            return fake
        return real_run(args, **kwargs)

    with patch("subprocess.run", side_effect=fake_run):
        second = ensure_compiled_binary(
            _NATIVE_OCR_SOURCE, developer_dir=_DEVELOPER_DIR, cache_dir=cache_dir
        )
    assert second is not None
    assert second != first


def test_unresolvable_developer_dir_fails_closed_instead_of_raising(tmp_path):
    cache_dir = tmp_path / "cache"
    result = ensure_compiled_binary(
        _NATIVE_OCR_SOURCE, developer_dir="/nonexistent/toolchain/path", cache_dir=cache_dir
    )
    assert result is None


def test_compile_failure_falls_back_to_none_and_leaves_no_orphan_temp(tmp_path):
    broken_source = tmp_path / "broken.swift"
    broken_source.write_text("this is not valid swift {{{")
    cache_dir = tmp_path / "cache"
    result = ensure_compiled_binary(
        broken_source, developer_dir=_DEVELOPER_DIR, cache_dir=cache_dir
    )
    assert result is None
    if cache_dir.exists():
        assert list(cache_dir.iterdir()) == []


def test_compiled_binary_output_matches_pinned_script_mode_reader(tmp_path, sample_image):
    """Canonical-JSON equality against the actual pinned reader's subprocess call.

    Uses the same argv shape `_rendered_text` uses (`swift <script> <image>`),
    not a mock, so this compares real stdout bytes on a real image.
    """
    cache_dir = tmp_path / "cache"
    binary = ensure_compiled_binary(
        _NATIVE_OCR_SOURCE, developer_dir=_DEVELOPER_DIR, cache_dir=cache_dir
    )
    assert binary is not None

    env = dict(os.environ, DEVELOPER_DIR=_DEVELOPER_DIR)
    script_mode = subprocess.run(
        ["swift", str(_NATIVE_OCR_SOURCE), str(sample_image)],
        check=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    compiled_mode = subprocess.run(
        [str(binary), str(sample_image)], check=True, capture_output=True, timeout=30
    )

    assert json.loads(compiled_mode.stdout) == json.loads(script_mode.stdout)
