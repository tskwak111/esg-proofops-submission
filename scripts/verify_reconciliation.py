"""Run the offline reconciliation release checks into a new evidence directory."""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RECONCILIATION_PATHS = (
    "packages/proofops/domain/reconciliation",
    "packages/proofops/application/reconciliation",
    "packages/proofops/adapters/reconciliation",
    "packages/proofops/adapters/dart",
    "evaluation/reconciliation_cli.py",
    "evaluation/reconciliation_fixtures.py",
    "evaluation/reconciliation_collect.py",
    "evaluation/reconciliation_prepare.py",
    "evaluation/reconciliation_source_audit.py",
    "evaluation/reconciliation_benchmark.py",
)
PRODUCT_PATHS = (
    "packages/proofops/application/uploads_security.py",
    "packages/proofops/adapters/local/reconciliation_store.py",
    "apps/api/src/proofops_api/routers/reconciliation.py",
    "evaluation/reconciliation_import.py",
)
NEW_SOURCE_PATHS = (
    "scripts/verify_reconciliation.py",
    "scripts/check_reconciliation_readiness.py",
    "tests/reconciliation/test_release_verifier.py",
)


def build_environment(root: Path = ROOT, base: dict[str, str] | None = None) -> dict[str, str]:
    """Build a cross-platform environment without assuming a path separator."""
    environment = dict(os.environ if base is None else base)
    parts = (
        root / "packages",
        root / "apps/api/src",
        root / "apps/worker/src",
        root / "apps/agent/src",
    )
    environment.update(
        PYTHONPATH=os.pathsep.join(str(path) for path in parts),
        PYTHONUTF8="1",
        PYTHONDONTWRITEBYTECODE="1",
    )
    return environment


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def run_command(
    name: str,
    args: Sequence[str],
    *,
    root: Path = ROOT,
    env: dict[str, str] | None = None,
    timeout: float = 180,
    executable: str | None = None,
) -> dict[str, Any]:
    """Run one Python command and convert every launch failure into evidence."""
    command = [sys.executable if executable is None else executable, *args]
    started = time.monotonic()
    log: dict[str, Any] = {
        "check": name,
        "command": command,
        "cwd": str(root),
        "exit_code": None,
        "stdout": "",
        "stderr": "",
        "timed_out": False,
        "error": None,
        "passed": False,
    }
    try:
        result = subprocess.run(
            command,
            cwd=root,
            env=build_environment(root) if env is None else env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        log.update(
            exit_code=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            passed=result.returncode == 0,
        )
    except subprocess.TimeoutExpired as exc:
        log.update(
            stdout=_text(exc.stdout),
            stderr=_text(exc.stderr),
            timed_out=True,
            error="timeout",
        )
    except FileNotFoundError as exc:
        log.update(error="executable_not_found", stderr=str(exc))
    except OSError as exc:
        log.update(error="process_launch_failed", stderr=str(exc))
    log["duration_seconds"] = round(time.monotonic() - started, 3)
    return log


def build_summary(checks: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Create the authoritative aggregate result; any failed check fails the release."""
    passed_count = sum(check.get("passed") is True for check in checks)
    passed = bool(checks) and passed_count == len(checks)
    return {
        "schema_version": "reconciliation-release-verifier-1",
        "generated_at": datetime.now(UTC).isoformat(),
        "root": str(ROOT),
        "python": sys.executable,
        "status": "passed" if passed else "failed",
        "passed": passed,
        "summary": {
            "total": len(checks),
            "passed": passed_count,
            "failed": len(checks) - passed_count,
        },
        "checks": [
            {
                "check": check["check"],
                "passed": check.get("passed") is True,
                "exit_code": check.get("exit_code"),
                "timed_out": check.get("timed_out", False),
                "error": check.get("error"),
                "evidence": check.get("evidence"),
            }
            for check in checks
        ],
    }


def _write_json_new(path: Path, value: Any) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


class ReleaseVerifier:
    def __init__(self, output: Path, timeout: float) -> None:
        self.output = output
        self.timeout = timeout
        self.environment = build_environment()
        self.checks: list[dict[str, Any]] = []

    def record(self, log: dict[str, Any]) -> dict[str, Any]:
        evidence = f"{len(self.checks) + 1:02d}_{log['check']}.json"
        log["evidence"] = evidence
        _write_json_new(self.output / evidence, log)
        self.checks.append(log)
        state = "PASS" if log.get("passed") else "FAIL"
        print(f"{state} {log['check']}", flush=True)
        return log

    def command(self, name: str, args: Sequence[str]) -> dict[str, Any]:
        return self.record(
            run_command(
                name,
                args,
                root=ROOT,
                env=self.environment,
                timeout=self.timeout,
            )
        )

    def internal_failure(self, name: str, error: str) -> dict[str, Any]:
        return self.record(
            {
                "check": name,
                "command": None,
                "cwd": str(ROOT),
                "exit_code": None,
                "stdout": "",
                "stderr": "",
                "timed_out": False,
                "error": error,
                "passed": False,
                "duration_seconds": 0.0,
            }
        )

    def static_checks(self) -> None:
        self.command(
            "pytest",
            [
                "-m",
                "pytest",
                "tests/reconciliation",
                "tests/acceptance/test_rules.py",
                "tests/acceptance/test_upload_security.py",
                "tests/acceptance/test_upload.py",
                "-q",
                "-p",
                "no:cacheprovider",
            ],
        )
        lint_paths = [
            *RECONCILIATION_PATHS,
            *PRODUCT_PATHS,
            "scripts/verify_reconciliation.py",
            "scripts/check_reconciliation_readiness.py",
            "tests/reconciliation",
        ]
        self.command("ruff_check", ["-m", "ruff", "check", *lint_paths])
        self.command("ruff_format", ["-m", "ruff", "format", "--check", *lint_paths])
        self.command(
            "mypy",
            [
                "-m",
                "mypy",
                *RECONCILIATION_PATHS,
                "scripts/verify_reconciliation.py",
                "scripts/check_reconciliation_readiness.py",
            ],
        )
        self.command(
            "product_mypy",
            ["-m", "mypy", "--follow-imports", "silent", *PRODUCT_PATHS],
        )
        self.command("architecture", ["scripts/verify_architecture.py"])

    def synthetic_cases(self) -> None:
        fixtures = self.output / "fixtures"
        generated = self.command(
            "fixture_generation",
            ["-m", "evaluation.reconciliation_fixtures", "--output", str(fixtures)],
        )
        examples = ROOT / "contracts/reconciliation/examples"
        case_names = sorted(path.stem for path in examples.glob("*.json"))
        if len(case_names) != 8:
            self.internal_failure("case_catalog", f"expected_8_cases_found_{len(case_names)}")
        if not generated["passed"]:
            for name in case_names:
                self.internal_failure(f"cli_{name}", "fixture_generation_failed")
            return
        for name in case_names:
            self.synthetic_case(name, fixtures / name, examples / f"{name}.json")

    def synthetic_case(self, name: str, case: Path, example: Path) -> None:
        result_path = case / "result.json"
        projection_path = case / "presentation.json"
        log = run_command(
            f"cli_{name}",
            [
                "-m",
                "evaluation.reconciliation_cli",
                "--packet",
                str(case / "packet.json"),
                "--policy",
                str(case / "policy.json"),
                "--artifacts",
                str(case / "artifacts"),
                "--artifact-index",
                str(case / "artifacts.json"),
                "--documents",
                str(case / "documents.json"),
                "--policy-registry",
                str(case / "policies.json"),
                "--coverage-registry",
                str(case / "coverage.json"),
                "--output",
                str(result_path),
                "--projection",
                str(projection_path),
            ],
            root=ROOT,
            env=self.environment,
            timeout=self.timeout,
        )
        if log["passed"]:
            try:
                sys.path.insert(0, str(ROOT / "packages"))
                from proofops.application.reconciliation.schema import validate_schema

                result = json.loads(result_path.read_text(encoding="utf-8"))
                expected = json.loads(example.read_text(encoding="utf-8"))["expected"]
                validate_schema("output", result)
                expected_outcome = {key: expected[key] for key in ("execution_state", "status")}
                observed_outcome = {key: result[key] for key in ("execution_state", "status")}
                log["validation"] = {
                    "schema": "passed",
                    "expected": expected_outcome,
                    "observed": observed_outcome,
                }
                if observed_outcome != expected_outcome:
                    log.update(passed=False, error="unexpected_outcome")
            except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
                log.update(passed=False, error=f"result_validation_failed:{type(exc).__name__}")
        self.record(log)

    def run(self) -> dict[str, Any]:
        self.static_checks()
        self.synthetic_cases()
        summary = build_summary(self.checks)
        _write_json_new(self.output / "checks.json", summary)
        return summary


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument(
        "--output",
        required=True,
        type=Path,
        help="New evidence directory; an existing path is rejected",
    )
    cli.add_argument(
        "--timeout-seconds",
        type=float,
        default=180,
        help="Per-command timeout (default: 180)",
    )
    return cli


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if not math.isfinite(args.timeout_seconds) or args.timeout_seconds <= 0:
        print("--timeout-seconds must be finite and positive", file=sys.stderr)
        return 2
    try:
        args.output.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        print(f"output directory already exists: {args.output}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"cannot create output directory: {exc}", file=sys.stderr)
        return 2
    verifier = ReleaseVerifier(args.output.resolve(), args.timeout_seconds)
    try:
        summary = verifier.run()
    except Exception as exc:  # Preserve a truthful aggregate even for verifier defects.
        verifier.internal_failure("verifier_internal", type(exc).__name__)
        summary = build_summary(verifier.checks)
        checks_path = verifier.output / "checks.json"
        if not checks_path.exists():
            _write_json_new(checks_path, summary)
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
