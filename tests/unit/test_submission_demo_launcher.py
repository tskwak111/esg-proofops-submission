"""Launcher-path tests for submission_demo.py (R34-day1 fix).

Covers the _assert_disjoint guard and _prepare_writable copy/reuse logic that
the R34 fix depends on.  No model calls, no network, no filesystem state
outside tmp_path.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import submission_demo as sd  # noqa: E402

# ---------------------------------------------------------------------------
# _assert_disjoint
# ---------------------------------------------------------------------------


def test_disjoint_same_path_raises(tmp_path):
    with pytest.raises(SystemExit, match="must differ"):
        sd._assert_disjoint(tmp_path, tmp_path)


def test_disjoint_demo_inside_seed_raises(tmp_path):
    with pytest.raises(SystemExit, match="must not live inside"):
        sd._assert_disjoint(tmp_path, tmp_path / "sub")


def test_disjoint_seed_inside_demo_raises(tmp_path):
    with pytest.raises(SystemExit, match="must not be an ancestor"):
        sd._assert_disjoint(tmp_path / "sub", tmp_path)


def test_disjoint_sibling_paths_pass(tmp_path):
    # Should not raise
    sd._assert_disjoint(tmp_path / "seed", tmp_path / "demo")


# ---------------------------------------------------------------------------
# _prepare_writable
# ---------------------------------------------------------------------------


def _make_seed(base: Path, run_id: str = "run-abc") -> Path:
    seed = base / "seed"
    seed.mkdir()
    (seed / "pilot.json").write_text(json.dumps({"run_id": run_id}))
    (seed / "state.sqlite3").write_bytes(b"db")
    return seed


def test_prepare_writable_creates_copy(tmp_path):
    seed = _make_seed(tmp_path)
    demo = tmp_path / "demo"
    sd._prepare_writable(seed, demo)
    assert demo.is_dir()
    assert (demo / "pilot.json").is_file()
    assert (demo / "state.sqlite3").is_file()


def test_prepare_writable_reuses_matching_demo(tmp_path):
    seed = _make_seed(tmp_path)
    demo = tmp_path / "demo"
    sd._prepare_writable(seed, demo)
    mtime_before = (demo / "pilot.json").stat().st_mtime
    # Second call must not error and must not clobber the copy
    sd._prepare_writable(seed, demo)
    assert (demo / "pilot.json").stat().st_mtime == mtime_before


def test_prepare_writable_rejects_mismatched_run_id(tmp_path):
    seed = _make_seed(tmp_path, run_id="run-abc")
    demo = tmp_path / "demo"
    sd._prepare_writable(seed, demo)
    # Overwrite demo's pilot.json with a different run_id
    (demo / "pilot.json").write_text(json.dumps({"run_id": "run-xyz"}))
    with pytest.raises(SystemExit, match="different run"):
        sd._prepare_writable(seed, demo)


def test_prepare_writable_rejects_demo_without_pilot_json(tmp_path):
    seed = _make_seed(tmp_path)
    demo = tmp_path / "demo"
    demo.mkdir()  # exists but no pilot.json
    with pytest.raises(SystemExit, match="not a pilot state"):
        sd._prepare_writable(seed, demo)


def test_prepare_writable_never_deletes_seed(tmp_path):
    seed = _make_seed(tmp_path)
    demo = tmp_path / "demo"
    sd._prepare_writable(seed, demo)
    # seed must be intact
    assert (seed / "pilot.json").is_file()
    assert (seed / "state.sqlite3").is_file()
