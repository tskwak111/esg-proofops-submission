"""Tests for the portable offline reconciliation release verifier."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scripts import verify_reconciliation as verifier

ROOT = Path(__file__).resolve().parents[2]


def test_process_failures_make_the_aggregate_fail(tmp_path: Path) -> None:
    missing = verifier.run_command(
        "missing",
        [],
        executable=str(tmp_path / "missing-python"),
        timeout=1,
    )
    nonzero = verifier.run_command("nonzero", ["-c", "raise SystemExit(7)"], timeout=5)
    timeout = verifier.run_command("timeout", ["-c", "import time; time.sleep(2)"], timeout=0.01)

    summary = verifier.build_summary([missing, nonzero, timeout])

    assert missing["error"] == "executable_not_found"
    assert nonzero["exit_code"] == 7
    assert timeout["timed_out"] is True
    assert summary["status"] == "failed"
    assert summary["summary"] == {"total": 3, "passed": 0, "failed": 3}


def test_existing_output_is_rejected_without_overwrite(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "existing"
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_text("original", encoding="utf-8")

    assert verifier.main(["--output", str(output)]) == 2
    assert marker.read_text(encoding="utf-8") == "original"
    assert list(output.iterdir()) == [marker]
    assert "already exists" in capsys.readouterr().err


def test_paths_are_derived_from_source_and_platform_separator() -> None:
    assert verifier.ROOT == Path(verifier.__file__).resolve().parents[1]
    environment = verifier.build_environment(ROOT, {})
    assert environment["PYTHONPATH"].split(os.pathsep) == [
        str(ROOT / "packages"),
        str(ROOT / "apps/api/src"),
        str(ROOT / "apps/worker/src"),
        str(ROOT / "apps/agent/src"),
    ]
    assert environment["PYTHONUTF8"] == "1"


@pytest.mark.parametrize("timeout", ["nan", "inf", "0", "-1"])
def test_invalid_timeout_is_rejected_before_creating_output(tmp_path: Path, timeout: str) -> None:
    output = tmp_path / "invalid"
    assert verifier.main(["--output", str(output), "--timeout-seconds", timeout]) == 2
    assert not output.exists()


def test_real_release_run_executes_and_validates_all_eight_cli_cases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "release-evidence"
    output.mkdir()
    monkeypatch.chdir(tmp_path)
    runner = verifier.ReleaseVerifier(output, timeout=180)
    # Exercise real fixture/CLI subprocesses without recursively invoking pytest.
    runner.synthetic_cases()
    summary = verifier.build_summary(runner.checks)
    cli_checks = [check for check in summary["checks"] if check["check"].startswith("cli_")]

    assert summary["status"] == "passed"
    assert len(cli_checks) == 8
    assert all(check["passed"] for check in cli_checks)
    for check in cli_checks:
        detail = json.loads((output / check["evidence"]).read_text(encoding="utf-8"))
        assert detail["exit_code"] == 0
        assert detail["command"][1:3] == ["-m", "evaluation.reconciliation_cli"]
        assert detail["validation"]["schema"] == "passed"
        assert detail["validation"]["observed"] == detail["validation"]["expected"]
