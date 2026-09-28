"""Offline deployment-readiness checker for the local C1-C4 reconciliation product.

This tool inspects; it never deploys. It performs no network call, no AWS API
call, no model invocation, no dependency installation and no write outside the
requested output directory and the private temporary directory it creates for
its database rehearsal. It never reads `.env`, `.env.*.local` or any other real
environment file, and it never prints a configuration value whose name looks
like a credential.

Findings are machine-readable. `passed` means an executed check confirmed the
condition. `blocked` means a real prerequisite is missing and must be supplied
from outside this repository. `failed` means the check ran and found a defect.
`not_run` means the check is out of this tool's authority (cloud probes, live
models, real approvals) and is deliberately not attempted.

The current product is a local API/SQLite/React application. Passing every
local gate does not make any cloud claim: `cloud_deployment` is reported
separately and never becomes ready from this tool's evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import urllib.parse
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]

SCHEMA_VERSION = "reconciliation-deployment-readiness-1"

PASSED = "passed"
BLOCKED = "blocked"
FAILED = "failed"
NOT_RUN = "not_run"
BLOCKING_STATUSES = frozenset({BLOCKED, FAILED})

LOCAL = "local_deployment"
VERIFICATION = "local_verification"
CLOUD = "cloud_deployment"
TARGETS = (LOCAL, VERIFICATION, CLOUD)

# Names whose value is never emitted, echoed or logged by this tool.
SECRET_NAME = re.compile(r"KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL|ARN", re.IGNORECASE)

# Real environment files: existence is checked, contents are never read.
UNREAD_ENV_FILES = (".env", ".env.local", ".env.dart.local", ".env.production")

ENV_EXAMPLE = ".env.example"
ENV_DOC = "docs/17_ENV_CONFIG.md"
CI_WORKFLOW = ".github/workflows/ci.yml"

REQUIRED_PATHS = (
    "packages/proofops/domain/reconciliation/engine.py",
    "packages/proofops/application/reconciliation/service.py",
    "packages/proofops/application/reconciliation/schema.py",
    "packages/proofops/adapters/reconciliation/files.py",
    "packages/proofops/adapters/local/reconciliation_store.py",
    "apps/api/src/proofops_api/routers/reconciliation.py",
    "apps/api/src/proofops_api/main.py",
    "apps/api/src/proofops_api/composition.py",
    "apps/web/src/features/reconciliation/ReconciliationWorkspace.tsx",
    "apps/web/package.json",
    "contracts/reconciliation/output.schema.json",
    "contracts/reconciliation/input.schema.json",
    "contracts/reconciliation/policy.schema.json",
    "contracts/reconciliation/product.openapi.yaml",
    "evaluation/reconciliation_import.py",
    "scripts/verify_reconciliation.py",
    "docs/RECONCILIATION_PRODUCT_CONTRACT.md",
    ENV_EXAMPLE,
    ENV_DOC,
    CI_WORKFLOW,
    "uv.lock",
    "pnpm-lock.yaml",
    "pyproject.toml",
)

# Deployment artifacts that would exist if this product shipped as a container
# or an executable cloud stack. Their absence is evidence, not a defect.
CLOUD_ARTIFACTS = (
    "Dockerfile",
    "apps/api/Dockerfile",
    "apps/worker/Dockerfile",
    "apps/agent/Dockerfile",
    "docker-compose.yml",
    "compose.yaml",
    "infra/cdk/cdk.json",
    "infra/cdk/bin/app.ts",
)

# Immutable reconciliation tables, mirrored from the adapter so a drift in
# either list is itself reported instead of silently skipping a check.
EXPECTED_IMMUTABLE_TABLES = (
    "reconciliation_case",
    "reconciliation_revision",
    "reconciliation_source",
    "reconciliation_idempotency",
)


@dataclass(frozen=True, slots=True)
class ReadOnlyAuth:
    """A viewer identity for the rehearsal. It grants nothing beyond reading."""

    tenant_id: str
    user_sub: str = "readiness-checker"
    role: str = "viewer"
    capabilities: frozenset[str] = frozenset({"viewer"})
    session_id: str = "readiness-checker"


@dataclass(frozen=True, slots=True)
class Finding:
    """One executed readiness observation."""

    check: str
    category: str
    target: str
    status: str
    summary: str
    detail: dict[str, Any] = field(default_factory=dict)
    external_input: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "category": self.category,
            "target": self.target,
            "status": self.status,
            "blocking": self.status in BLOCKING_STATUSES,
            "summary": self.summary,
            "detail": self.detail,
            "external_input": self.external_input,
        }


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def redact(name: str, value: str) -> str:
    """Describe a configuration value without ever reproducing a secret."""
    if SECRET_NAME.search(name):
        return "set" if value else "empty"
    return value


@contextmanager
def owned_workspace(prefix: str = "reconciliation-readiness-") -> Iterator[Path]:
    """Create a temporary workspace and remove only that exact directory.

    The path is resolved and re-checked before removal: it must be a real
    directory, not a symlink, and an immediate child of the system temporary
    directory carrying this prefix. Anything else is left in place rather than
    deleted, because a wrong `rmtree` target is unrecoverable.
    """
    root = Path(tempfile.mkdtemp(prefix=prefix)).resolve(strict=True)
    try:
        yield root
    finally:
        base = Path(tempfile.gettempdir()).resolve(strict=True)
        if (
            root.is_dir()
            and not root.is_symlink()
            and root.resolve(strict=True) == root
            and root.parent == base
            and root.name.startswith(prefix)
            and root != base
        ):
            shutil.rmtree(root, ignore_errors=True)
        else:
            print(f"refusing to remove unexpected workspace: {root.name}", file=sys.stderr)


def load_module(path: Path, name: str) -> Any:
    """Import a repository script by path so both CLI and pytest resolve it alike."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def digest_file(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            sha.update(chunk)
    return sha.hexdigest()


def tree_digest(root: Path) -> dict[str, str]:
    """Map every file under `root` to its SHA-256, keyed by relative POSIX path."""
    return {
        path.relative_to(root).as_posix(): digest_file(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def probe_version(command: Sequence[str], timeout: float = 20.0) -> dict[str, Any]:
    """Run one `--version`-style probe. Never installs, never reaches a network."""
    executable = shutil.which(command[0])
    if executable is None:
        return {"resolved": False, "error": "not_on_path", "output": ""}
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [executable, *command[1:]],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"resolved": True, "error": "timeout", "output": ""}
    except OSError as exc:
        return {"resolved": True, "error": f"launch_failed:{type(exc).__name__}", "output": ""}
    output = f"{result.stdout}{result.stderr}".strip()
    return {
        "resolved": True,
        "error": None if result.returncode == 0 else f"exit_{result.returncode}",
        "output": output[:400],
    }


def first_version(text: str) -> tuple[int, ...] | None:
    match = re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", text)
    if match is None:
        return None
    return tuple(int(part) for part in match.groups() if part is not None)


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a KEY=VALUE example file. Only ever called on the committed example."""
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        values[name.strip()] = value.strip()
    return values


@dataclass(frozen=True, slots=True)
class DocumentedVariable:
    name: str
    default: str
    requirement: str
    environments: str


def parse_env_doc(path: Path) -> dict[str, DocumentedVariable]:
    """Read the variable contract table out of the environment specification."""
    documented: dict[str, DocumentedVariable] = {}
    row = re.compile(r"^\|\s*`([A-Z0-9_]+)`\s*\|(.*)$")
    for line in path.read_text(encoding="utf-8").splitlines():
        match = row.match(line.strip())
        if match is None:
            continue
        cells = [cell.strip().strip("`") for cell in match.group(2).split("|")]
        if len(cells) < 3:
            continue
        documented[match.group(1)] = DocumentedVariable(
            name=match.group(1),
            default="" if cells[0] in ("빈 값", "") else cells[0],
            requirement=cells[1],
            environments=cells[2],
        )
    return documented


def parse_ci_pins(path: Path) -> dict[str, str]:
    """Extract the toolchain versions CI already pins; nothing here is invented."""
    import yaml  # type: ignore[import-untyped]

    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    wanted = {
        "actions/setup-python": ("python-version", "python"),
        "actions/setup-node": ("node-version", "node"),
        "actions/setup-java": ("java-version", "java"),
        "pnpm/action-setup": ("version", "pnpm"),
    }
    pins: dict[str, str] = {}
    jobs = document.get("jobs", {}) if isinstance(document, dict) else {}
    for job in jobs.values():
        for step in job.get("steps", []) if isinstance(job, dict) else []:
            uses = step.get("uses", "") if isinstance(step, dict) else ""
            options = step.get("with", {}) if isinstance(step, dict) else {}
            for prefix, (key, tool) in wanted.items():
                if uses.startswith(prefix) and isinstance(options, dict) and key in options:
                    pins.setdefault(tool, str(options[key]))
    return pins


def resolve_java(environ: dict[str, str]) -> tuple[str, Path | None]:
    """Resolve the Java the parser regression would actually use."""
    explicit = environ.get("PROOFOPS_TEST_JAVA", "").strip()
    if explicit:
        candidate = Path(explicit)
        return ("PROOFOPS_TEST_JAVA", candidate if candidate.is_file() else None)
    home = environ.get("JAVA_HOME", "").strip()
    if home:
        name = "java.exe" if sys.platform == "win32" else "java"
        candidate = Path(home) / "bin" / name
        return ("JAVA_HOME", candidate if candidate.is_file() else None)
    found = shutil.which("java")
    return ("PATH", Path(found) if found else None)


def port_of(url: str, fallback: int) -> int:
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return fallback
    if parsed.port is not None:
        return parsed.port
    return 443 if parsed.scheme == "https" else fallback


def loopback_port_free(port: int) -> bool:
    """Bind-test a loopback port. This opens no outbound connection."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def reconciliation_imports(root: Path) -> Iterator[Any]:
    """Import the product packages from the repository, without installing them."""
    for part in ("packages", "apps/api/src", "apps/worker/src", "apps/agent/src"):
        entry = str(root / part)
        if entry not in sys.path:
            sys.path.insert(0, entry)
    from proofops.adapters.local import reconciliation_store
    from proofops.adapters.local.run_store import LocalSQLiteRunStore

    yield reconciliation_store
    yield LocalSQLiteRunStore


# --------------------------------------------------------------------------- #
# checks
# --------------------------------------------------------------------------- #


class ReadinessChecker:
    """Collects findings. Every check must record one, including its own failure."""

    def __init__(self, root: Path, environ: dict[str, str] | None = None) -> None:
        self.root = root
        self.environ = dict(os.environ if environ is None else environ)
        self.findings: list[Finding] = []
        self.tenant = str(uuid4())
        self.auth = ReadOnlyAuth(self.tenant)

    # ---------------------------------------------------------------- #

    def add(self, finding: Finding) -> Finding:
        self.findings.append(finding)
        print(f"{finding.status.upper():8} {finding.check}", flush=True)
        return finding

    def guarded(self, check: str, category: str, target: str, body: Callable[[], Finding]) -> None:
        """Run one check; a checker defect becomes a failed finding, never a crash."""
        try:
            self.add(body())
        except Exception as exc:  # A silent checker defect would be worse than a report.
            self.add(
                Finding(
                    check=check,
                    category=category,
                    target=target,
                    status=FAILED,
                    summary="readiness check raised an unexpected error",
                    # Only the exception type: a message could quote a value.
                    detail={"error": type(exc).__name__},
                )
            )

    # ---------------------------------------------------------------- #
    # platform and toolchain
    # ---------------------------------------------------------------- #

    def check_platform(self) -> Finding:
        system, machine = sys.platform, platform.machine().lower()
        detail = {"sys_platform": system, "machine": machine, "python": platform.python_version()}
        if system == "win32" or system.startswith("linux"):
            return Finding(
                "platform.supported",
                "platform",
                LOCAL,
                PASSED,
                "host platform is inside the supported set",
                detail,
            )
        if system == "darwin" and machine in ("arm64", "aarch64"):
            return Finding(
                "platform.supported",
                "platform",
                LOCAL,
                PASSED,
                "Apple Silicon macOS is inside the supported set",
                detail,
            )
        if system == "darwin":
            return Finding(
                "platform.supported",
                "platform",
                LOCAL,
                BLOCKED,
                "Intel macOS is excluded from the delivery scope",
                detail,
                external_input="An Apple Silicon macOS, Windows or Linux host.",
            )
        return Finding(
            "platform.supported",
            "platform",
            LOCAL,
            BLOCKED,
            "host platform is outside the verified set",
            detail,
            external_input="A Windows, Linux or Apple Silicon macOS host.",
        )

    def check_python(self) -> Finding:
        text = (self.root / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r'requires-python\s*=\s*"[>=~^]*\s*([0-9.]+)"', text)
        detail: dict[str, Any] = {
            "running": platform.python_version(),
            "declared_minimum": match.group(1) if match else None,
        }
        if match is None:
            return Finding(
                "toolchain.python",
                "toolchain",
                LOCAL,
                FAILED,
                "pyproject.toml declares no requires-python floor",
                detail,
            )
        minimum = tuple(int(part) for part in match.group(1).split("."))
        if sys.version_info[: len(minimum)] >= minimum:
            return Finding(
                "toolchain.python",
                "toolchain",
                LOCAL,
                PASSED,
                "running interpreter satisfies the declared minimum",
                detail,
            )
        return Finding(
            "toolchain.python",
            "toolchain",
            LOCAL,
            BLOCKED,
            "running interpreter is older than the declared minimum",
            detail,
            external_input=f"Python {match.group(1)} or newer on the deployment host.",
        )

    def check_pinned_tool(
        self, tool: str, command: Sequence[str], pins: dict[str, str], target: str
    ) -> Finding:
        pinned = pins.get(tool)
        probe = probe_version(command)
        detail: dict[str, Any] = {"ci_pin": pinned, **probe}
        if not probe["resolved"]:
            return Finding(
                f"toolchain.{tool}",
                "toolchain",
                target,
                BLOCKED,
                f"{tool} is not available on this host",
                detail,
                external_input=(
                    f"Install {tool}"
                    + (f" {pinned}" if pinned else "")
                    + " on the deployment host; this tool never installs dependencies."
                ),
            )
        if probe["error"]:
            return Finding(
                f"toolchain.{tool}",
                "toolchain",
                target,
                BLOCKED,
                f"{tool} is present but did not report a version",
                detail,
                external_input=f"A working {tool} installation on the deployment host.",
            )
        found = first_version(probe["output"])
        expected = first_version(pinned or "")
        detail["detected_version"] = ".".join(str(part) for part in found) if found else None
        if pinned is None or expected is None or found is None:
            return Finding(
                f"toolchain.{tool}",
                "toolchain",
                target,
                PASSED,
                f"{tool} is available; no comparable CI pin was found",
                detail,
            )
        if found[0] == expected[0]:
            return Finding(
                f"toolchain.{tool}",
                "toolchain",
                target,
                PASSED,
                f"{tool} major version matches the CI pin",
                detail,
            )
        return Finding(
            f"toolchain.{tool}",
            "toolchain",
            target,
            BLOCKED,
            f"{tool} major version differs from the CI pin",
            detail,
            external_input=f"{tool} {pinned} on the deployment host.",
        )

    def check_java(self, pins: dict[str, str]) -> Finding:
        source, java = resolve_java(self.environ)
        pinned = pins.get("java")
        detail: dict[str, Any] = {"resolved_from": source, "ci_pin": pinned}
        if java is None:
            return Finding(
                "toolchain.java",
                "toolchain",
                VERIFICATION,
                BLOCKED,
                "no Java runtime resolved for the real parser regression",
                detail,
                external_input=(
                    f"Java {pinned or '21'} installed, with PROOFOPS_TEST_JAVA or JAVA_HOME set."
                ),
            )
        probe = probe_version([str(java), "-version"])
        detail.update({key: probe[key] for key in ("error", "output")})
        found = first_version(re.sub(r'.*version\s+"', "", probe["output"], count=1))
        expected = first_version(pinned or "")
        detail["detected_version"] = ".".join(str(part) for part in found) if found else None
        if found and expected and found[0] == expected[0]:
            return Finding(
                "toolchain.java",
                "toolchain",
                VERIFICATION,
                PASSED,
                "resolved Java major version matches the CI pin",
                detail,
            )
        return Finding(
            "toolchain.java",
            "toolchain",
            VERIFICATION,
            BLOCKED,
            "resolved Java major version does not match the CI pin",
            detail,
            external_input=(
                f"Java {pinned or '21'} selected through PROOFOPS_TEST_JAVA for the parser tests."
            ),
        )

    # ---------------------------------------------------------------- #
    # repository
    # ---------------------------------------------------------------- #

    def check_required_paths(self) -> Finding:
        missing = [name for name in REQUIRED_PATHS if not (self.root / name).exists()]
        empty = [
            name
            for name in REQUIRED_PATHS
            if (self.root / name).is_file() and (self.root / name).stat().st_size == 0
        ]
        detail = {"required": len(REQUIRED_PATHS), "missing": missing, "empty": empty}
        if missing or empty:
            return Finding(
                "repository.required_paths",
                "repository",
                LOCAL,
                FAILED,
                "the deployable tree is missing required files",
                detail,
            )
        return Finding(
            "repository.required_paths",
            "repository",
            LOCAL,
            PASSED,
            "every required product, contract and configuration file is present",
            detail,
        )

    def check_deployment_artifacts(self) -> Finding:
        present = [name for name in CLOUD_ARTIFACTS if (self.root / name).exists()]
        detail = {"searched": list(CLOUD_ARTIFACTS), "present": present}
        if present:
            return Finding(
                "repository.cloud_artifacts",
                "repository",
                CLOUD,
                NOT_RUN,
                "container or stack entrypoints exist but were not built or deployed here",
                detail,
                external_input="A reviewed build and deploy pipeline outside this tool.",
            )
        return Finding(
            "repository.cloud_artifacts",
            "repository",
            CLOUD,
            BLOCKED,
            "no container image or deployable stack entrypoint exists in this repository",
            detail,
            external_input=(
                "A container definition and an executable IaC entrypoint; the current "
                "product ships as a local process only."
            ),
        )

    # ---------------------------------------------------------------- #
    # configuration
    # ---------------------------------------------------------------- #

    def check_env_contract(self, documented: dict[str, DocumentedVariable]) -> Finding:
        example = parse_env_file(self.root / ENV_EXAMPLE)
        undocumented = sorted(set(example) - set(documented))
        unexampled = sorted(set(documented) - set(example))
        mismatched = sorted(
            name
            for name, variable in documented.items()
            if name in example and variable.default and example[name] != variable.default
        )
        detail = {
            "documented": len(documented),
            "in_example": len(example),
            "undocumented_in_spec": undocumented,
            "missing_from_example": unexampled,
            "default_mismatch": mismatched,
        }
        if unexampled or mismatched:
            return Finding(
                "configuration.env_contract",
                "configuration",
                LOCAL,
                FAILED,
                "the example environment disagrees with the environment specification",
                detail,
            )
        summary = "every documented variable appears in the example with the documented default"
        if undocumented:
            summary += (
                f"; {len(undocumented)} example variable(s) are absent from the specification "
                "table and need a documentation follow-up"
            )
        return Finding(
            "configuration.env_contract",
            "configuration",
            LOCAL,
            PASSED,
            summary,
            detail,
        )

    def check_local_profile(self) -> Finding:
        example = parse_env_file(self.root / ENV_EXAMPLE)
        expected = {"APP_ENV": "local", "MODEL_ADAPTER": "synthetic"}
        wrong = {
            name: example.get(name)
            for name, value in expected.items()
            if example.get(name) != value
        }
        origins = {
            name: redact(name, example.get(name, ""))
            for name in ("APP_ORIGIN", "API_PUBLIC_BASE_URL", "VITE_API_BASE_URL")
        }
        remote = sorted(
            name
            for name, value in origins.items()
            if value and urllib.parse.urlsplit(value).hostname not in ("localhost", "127.0.0.1")
        )
        detail = {"expected": expected, "unexpected": wrong, "origins": origins, "remote": remote}
        if wrong or remote:
            return Finding(
                "configuration.local_profile",
                "configuration",
                LOCAL,
                FAILED,
                "the example profile is not the local loopback profile it documents",
                detail,
            )
        return Finding(
            "configuration.local_profile",
            "configuration",
            LOCAL,
            PASSED,
            "the example profile is local, synthetic and loopback-only",
            detail,
        )

    def check_cloud_variables(self, documented: dict[str, DocumentedVariable]) -> Finding:
        example = parse_env_file(self.root / ENV_EXAMPLE)
        cloud = sorted(
            name for name, variable in documented.items() if variable.requirement == "cloud"
        )
        supplied = sorted(
            name
            for name in cloud
            if example.get(name, "").strip() or self.environ.get(name, "").strip()
        )
        absent = [name for name in cloud if name not in supplied]
        detail = {
            "cloud_variables": cloud,
            "supplied": supplied,
            "absent": absent,
            "values_emitted": False,
        }
        if not absent:
            return Finding(
                "configuration.cloud_variables",
                "configuration",
                CLOUD,
                NOT_RUN,
                "every cloud variable carries a value; this tool never validates one "
                "against a real account",
                detail,
                external_input="An account-owner preflight run against the real account.",
            )
        return Finding(
            "configuration.cloud_variables",
            "configuration",
            CLOUD,
            BLOCKED,
            f"{len(absent)} of {len(cloud)} cloud-required variables are unset, "
            "so no cloud target exists",
            detail,
            external_input=(
                "Real account values for: " + ", ".join(absent) + ". None may be invented here."
            ),
        )

    def check_live_model_artifacts(self) -> Finding:
        pairs = {
            "config/model_bindings.json": "config/model_bindings.example.json",
            "config/consent_profile.json": "config/consent_profile.example.json",
        }
        state = {
            real: {
                "approved_artifact_present": (self.root / real).exists(),
                "example_present": (self.root / example).exists(),
            }
            for real, example in pairs.items()
        }
        present = [name for name, value in state.items() if value["approved_artifact_present"]]
        detail = {"artifacts": state, "present": present}
        if present:
            return Finding(
                "configuration.live_model_artifacts",
                "configuration",
                CLOUD,
                NOT_RUN,
                "binding and consent artifacts exist; no model was called to validate them",
                detail,
                external_input="An approved preflight run; this tool never invokes a model.",
            )
        return Finding(
            "configuration.live_model_artifacts",
            "configuration",
            CLOUD,
            BLOCKED,
            "no approved model binding or consent profile exists, only examples",
            detail,
            external_input=(
                "An approved config/model_bindings.json and config/consent_profile.json "
                "produced by the account owner."
            ),
        )

    def check_unread_env_files(self) -> Finding:
        state = {
            name: {"present": (self.root / name).exists(), "read_by_this_tool": False}
            for name in UNREAD_ENV_FILES
        }
        return Finding(
            "configuration.real_env_files",
            "configuration",
            LOCAL,
            PASSED,
            "real environment files were detected by existence only and never opened",
            {"files": state},
        )

    def check_example_secrets(self) -> Finding:
        example = parse_env_file(self.root / ENV_EXAMPLE)
        populated = sorted(
            name for name, value in example.items() if SECRET_NAME.search(name) and value.strip()
        )
        detail = {
            "secret_named_variables": sorted(n for n in example if SECRET_NAME.search(n)),
            "populated": populated,
            "values_emitted": False,
        }
        if populated:
            return Finding(
                "configuration.example_secrets",
                "configuration",
                LOCAL,
                FAILED,
                "the committed example file carries values for credential-named variables",
                detail,
            )
        return Finding(
            "configuration.example_secrets",
            "configuration",
            LOCAL,
            PASSED,
            "no credential-named variable carries a value in the committed example",
            detail,
        )

    def check_loopback_ports(self) -> Finding:
        example = parse_env_file(self.root / ENV_EXAMPLE)
        ports = {
            "api": port_of(example.get("API_PUBLIC_BASE_URL", ""), 8000),
            "web": port_of(example.get("APP_ORIGIN", ""), 5173),
        }
        occupied = sorted(name for name, port in ports.items() if not loopback_port_free(port))
        detail = {"ports": ports, "occupied": occupied, "outbound_connections": 0}
        if occupied:
            return Finding(
                "configuration.loopback_ports",
                "configuration",
                LOCAL,
                BLOCKED,
                "a documented loopback port is already in use on this host",
                detail,
                external_input="A free loopback port, or an explicitly reconfigured origin.",
            )
        return Finding(
            "configuration.loopback_ports",
            "configuration",
            LOCAL,
            PASSED,
            "both documented loopback ports can be bound on this host",
            detail,
        )

    # ---------------------------------------------------------------- #
    # database, backup and restore
    # ---------------------------------------------------------------- #

    def rehearse_database(self, workspace: Path) -> str:
        """Create, populate, back up and restore a real reconciliation database.

        Returns the imported case id so a caller can re-read the same case.
        """
        store_module, run_store_class = tuple(reconciliation_imports(self.root))
        rejected = store_module.ReconciliationRejected
        live = workspace / "live"
        database = live / "state.sqlite3"
        artifacts = live / "artifacts"
        live.mkdir(parents=True)

        runs = run_store_class(database)
        with sqlite3.connect(database) as probe:
            before = self._schema_objects(probe)
        store = store_module.LocalReconciliationStore(
            database, artifacts, run_store=runs, claims=None
        )
        with sqlite3.connect(database) as probe:
            after = self._schema_objects(probe)
            versions = [
                row[0] for row in probe.execute("SELECT version FROM reconciliation_schema")
            ]

        self.add(self._schema_finding(store_module, store, versions, before, after))
        self.add(self._additive_finding(before, after))
        self.add(self._immutability_finding(database, store_module))
        self.add(self._unknown_version_finding(workspace, store_module, run_store_class, rejected))

        case_id = self._populate_case(store, store_module, workspace)
        self.add(self._backup_finding(workspace, database, artifacts))
        self.add(self._restore_finding(workspace, store_module, run_store_class, case_id))
        return case_id

    def _populate_case(self, store: Any, module: Any, workspace: Path) -> str:
        """Import one published fixture bundle through the store's own write path.

        `register_case` additionally anchors a draft to a verified run produced
        by the upload/parse/extract/tag pipeline; public registration is
        exercised with synthetic adapters by the test suite, not here. Bundle
        validation, artifact import with locator and hash proof, the revision
        append and the head row are the real product code.
        """
        entry = str(self.root)
        if entry not in sys.path:
            sys.path.insert(0, entry)
        from evaluation.reconciliation_fixtures import build_case

        bundle = build_case("c1-difference-no-explanation", workspace / "fixture")
        packet, policy, documents, artifacts, coverage = store._validate_bundle(bundle)
        tenant = self.tenant
        case_id = str(uuid4())
        created_at, actor = "2026-09-21T00:00:00Z", "readiness-checker"
        imported = store._import_artifacts(
            tenant, case_id, artifacts, packet, workspace / "fixture"
        )
        provenance = {
            "tenant_id": tenant,
            "imported_by": actor,
            "imported_at": created_at,
            "packet_sha256": module.canonical_sha256(packet),
            "policy_sha256": module.canonical_sha256(policy),
            "documents_sha256": module.canonical_sha256(documents),
            "artifacts_sha256": module.canonical_sha256(artifacts),
        }
        snapshot = {
            "case_id": case_id,
            "run_id": str(uuid4()),
            "claim_id": packet["identity"]["claim_id"],
            "item": packet["item"],
            "synthetic": True,
            "packet": packet,
            "policy": policy,
            "documents": documents,
            "artifacts": artifacts,
            "coverage": coverage,
            "provenance": provenance,
        }
        with store.jobs._transaction() as db:
            db.execute(
                "INSERT INTO reconciliation_case VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    tenant,
                    case_id,
                    snapshot["run_id"],
                    snapshot["claim_id"],
                    packet["item"],
                    1,
                    created_at,
                    actor,
                    module._encode(snapshot),
                ),
            )
            for source in imported:
                db.execute(
                    "INSERT INTO reconciliation_source VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        tenant,
                        case_id,
                        source["source_id"],
                        source["document_id"],
                        source["artifact_sha256"],
                        source["locator"],
                        source["quote"],
                        source["format"],
                        source["stored_path"],
                        source["byte_size"],
                    ),
                )
            store._append(
                db,
                tenant,
                case_id,
                1,
                "registration",
                actor,
                created_at,
                {"provenance": provenance, "event": {"kind": "registration", "actor": actor}},
            )
            db.execute(
                "INSERT INTO reconciliation_head VALUES (?,?,?,?,?,?,?,?,?)",
                (tenant, case_id, 1, "pending", 0, 0, None, None, created_at),
            )
        return case_id

    def read_case(self, store: Any, case_id: str) -> dict[str, Any]:
        """Read one case back through the product's own authorised read paths."""
        auth = self.auth
        detail = store.get_case(auth, case_id)
        revision = store.revision(auth, case_id, 1)
        contents = {}
        for source in detail.get("sources", []):
            payload, filename, digest = store.source_content(auth, case_id, source["source_id"])
            contents[source["source_id"]] = {
                "filename": filename,
                "sha256": digest,
                "byte_size": len(payload),
                "payload_sha256": hashlib.sha256(payload).hexdigest(),
            }
        return {"detail": detail, "revision": revision, "sources": contents}

    @staticmethod
    def _schema_objects(db: sqlite3.Connection) -> dict[str, str]:
        return {
            str(name): str(sql or "")
            for name, sql in db.execute(
                "SELECT name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            )
        }

    def _schema_finding(
        self,
        module: Any,
        store: Any,
        versions: list[int],
        before: dict[str, str],
        after: dict[str, str],
    ) -> Finding:
        declared = tuple(module.IMMUTABLE_TABLES)
        detail = {
            "schema_version_rows": versions,
            "module_schema_version": module.SCHEMA_VERSION,
            "store_kind": store.kind,
            "objects_before": len(before),
            "objects_after": len(after),
            "immutable_tables_declared": list(declared),
        }
        if versions != [module.SCHEMA_VERSION]:
            return Finding(
                "database.schema_initializes",
                "database",
                LOCAL,
                FAILED,
                "the reconciliation schema table does not hold exactly the declared version",
                detail,
            )
        if declared != EXPECTED_IMMUTABLE_TABLES:
            return Finding(
                "database.schema_initializes",
                "database",
                LOCAL,
                FAILED,
                "the immutable table list drifted from the list this checker verifies",
                detail,
            )
        return Finding(
            "database.schema_initializes",
            "database",
            LOCAL,
            PASSED,
            "a real store created the reconciliation schema at the declared version",
            detail,
        )

    def _additive_finding(self, before: dict[str, str], after: dict[str, str]) -> Finding:
        added = sorted(set(after) - set(before))
        removed = sorted(set(before) - set(after))
        changed = sorted(name for name in set(before) & set(after) if before[name] != after[name])
        foreign = sorted(name for name in added if not name.startswith("reconciliation_"))
        detail = {
            "added": added,
            "removed": removed,
            "changed_existing": changed,
            "added_outside_prefix": foreign,
        }
        if removed or changed or foreign or not added:
            return Finding(
                "database.additive_only",
                "database",
                LOCAL,
                FAILED,
                "enabling reconciliation did not leave the existing schema untouched",
                detail,
            )
        return Finding(
            "database.additive_only",
            "database",
            LOCAL,
            PASSED,
            "reconciliation adds only reconciliation_-prefixed objects and changes none",
            detail,
        )

    def _immutability_finding(self, database: Path, module: Any) -> Finding:
        results: dict[str, dict[str, bool]] = {}
        # A fresh marker keeps repeated probes of one database independent.
        marker = f"probe-{uuid4().hex[:8]}"
        with sqlite3.connect(database) as db:
            db.execute(
                "INSERT INTO reconciliation_case VALUES (?,?,?,?,?,?,?,?,?)",
                (marker, "c", "r", "cl", "C1", 1, "2026-09-21T00:00:00Z", "checker", b"{}"),
            )
            db.execute(
                "INSERT INTO reconciliation_revision VALUES (?,?,?,?,?,?,?,?)",
                (marker, "c", 1, "import", "checker", "2026-09-21T00:00:00Z", b"{}", "0" * 64),
            )
            db.execute(
                "INSERT INTO reconciliation_source VALUES (?,?,?,?,?,?,?,?,?,?)",
                (marker, "c", "s", "d", "0" * 64, "p1", "q", "pdf", "case/original.bin", 1),
            )
            db.execute(
                "INSERT INTO reconciliation_idempotency VALUES (?,?,?,?,?,?)",
                (marker, "c", "evaluate", "k", "0" * 64, b"{}"),
            )
            db.commit()
            for table in module.IMMUTABLE_TABLES:
                blocked: dict[str, bool] = {}
                for action in ("UPDATE", "DELETE"):
                    statement = (
                        f"UPDATE {table} SET tenant_id = 'other' WHERE tenant_id = '{marker}'"
                        if action == "UPDATE"
                        else f"DELETE FROM {table} WHERE tenant_id = '{marker}'"
                    )
                    try:
                        db.execute(statement)
                    except sqlite3.IntegrityError:
                        blocked[action] = True
                    except sqlite3.DatabaseError:
                        blocked[action] = True
                    else:
                        blocked[action] = False
                        db.rollback()
                results[table] = blocked
            db.rollback()
        unprotected = sorted(
            f"{table}.{action}"
            for table, actions in results.items()
            for action, refused in actions.items()
            if not refused
        )
        detail = {"tables": results, "unprotected": unprotected}
        if unprotected:
            return Finding(
                "database.immutability",
                "database",
                LOCAL,
                FAILED,
                "an immutable reconciliation table accepted an update or delete",
                detail,
            )
        return Finding(
            "database.immutability",
            "database",
            LOCAL,
            PASSED,
            "every immutable table refused a real update and delete attempt",
            detail,
        )

    def _unknown_version_finding(
        self, workspace: Path, module: Any, run_store_class: Any, rejected: type[Exception]
    ) -> Finding:
        future = workspace / "future"
        future.mkdir(parents=True)
        database = future / "state.sqlite3"
        runs = run_store_class(database)
        with sqlite3.connect(database) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS reconciliation_schema (version INTEGER PRIMARY KEY)"
            )
            db.execute("DELETE FROM reconciliation_schema")
            db.execute("INSERT INTO reconciliation_schema VALUES (?)", (module.SCHEMA_VERSION + 1,))
            db.commit()
        detail: dict[str, Any] = {"planted_version": module.SCHEMA_VERSION + 1}
        try:
            module.LocalReconciliationStore(
                database, future / "artifacts", run_store=runs, claims=None
            )
        except rejected as exc:
            detail["rejection_code"] = getattr(exc, "code", None)
            detail["http_status"] = getattr(exc, "status", None)
            return Finding(
                "database.unknown_version_rejected",
                "database",
                LOCAL,
                PASSED,
                "an unknown reconciliation schema version is refused instead of migrated",
                detail,
            )
        return Finding(
            "database.unknown_version_rejected",
            "database",
            LOCAL,
            FAILED,
            "an unknown reconciliation schema version was accepted",
            detail,
        )

    def _backup_finding(self, workspace: Path, database: Path, artifacts: Path) -> Finding:
        backup_root = workspace / "backup"
        backup_root.mkdir(parents=True)
        backup_database = backup_root / "state.sqlite3"
        backup_artifacts = backup_root / "artifacts"
        # An open writer connection stays live to prove the documented online
        # backup is safe while the application still holds the database.
        with sqlite3.connect(database) as live, sqlite3.connect(backup_database) as copy:
            live.execute("SELECT count(*) FROM reconciliation_case")
            live.backup(copy)
        shutil.copytree(artifacts, backup_artifacts)
        counts = {}
        for name in EXPECTED_IMMUTABLE_TABLES:
            with sqlite3.connect(database) as source, sqlite3.connect(backup_database) as copy:
                counts[name] = {
                    "source": source.execute(f"SELECT count(*) FROM {name}").fetchone()[0],
                    "backup": copy.execute(f"SELECT count(*) FROM {name}").fetchone()[0],
                }
        with sqlite3.connect(backup_database) as copy:
            integrity = copy.execute("PRAGMA integrity_check").fetchone()[0]
            schema = [row[0] for row in copy.execute("SELECT version FROM reconciliation_schema")]
        source_tree, backup_tree = tree_digest(artifacts), tree_digest(backup_artifacts)
        detail = {
            "method": "sqlite3.Connection.backup + shutil.copytree",
            "row_counts": counts,
            "backup_integrity_check": integrity,
            "backup_schema_version_rows": schema,
            "artifact_files": len(source_tree),
            "artifact_digests_match": source_tree == backup_tree,
        }
        mismatched = [name for name, value in counts.items() if value["source"] != value["backup"]]
        detail["row_count_mismatch"] = mismatched
        if mismatched or integrity != "ok" or source_tree != backup_tree or not source_tree:
            return Finding(
                "backup.online_copy",
                "backup_restore",
                LOCAL,
                FAILED,
                "the documented backup procedure did not reproduce the live state",
                detail,
            )
        return Finding(
            "backup.online_copy",
            "backup_restore",
            LOCAL,
            PASSED,
            "an online database backup and artifact copy reproduced every row and byte",
            detail,
        )

    def _restore_finding(
        self, workspace: Path, module: Any, run_store_class: Any, case_id: str
    ) -> Finding:
        """Restore the backup to a new location and compare it with the live original.

        The comparison reads both copies through the product's own `get_case`,
        `revision` and `source_content`, and compares the restored copy against
        `live`, never against the backup it came from, so a corrupted backup
        cannot verify itself.
        """
        live_database = workspace / "live" / "state.sqlite3"
        live_artifacts = workspace / "live" / "artifacts"
        restored = workspace / "restored"
        restored.mkdir(parents=True)
        database = restored / "state.sqlite3"
        artifacts = restored / "artifacts"
        shutil.copy2(workspace / "backup" / "state.sqlite3", database)
        shutil.copytree(workspace / "backup" / "artifacts", artifacts)

        live_store = module.LocalReconciliationStore(
            live_database,
            live_artifacts,
            run_store=run_store_class(live_database),
            claims=None,
        )
        restored_store = module.LocalReconciliationStore(
            database, artifacts, run_store=run_store_class(database), claims=None
        )
        original = self.read_case(live_store, case_id)
        try:
            copy = self.read_case(restored_store, case_id)
        except module.ReconciliationRejected as rejection:
            # The product refuses to serve a source whose bytes no longer hash
            # to the registered digest; that is a failed restore, not a pass.
            return Finding(
                "restore.rehearsal",
                "backup_restore",
                LOCAL,
                FAILED,
                "the restored copy was refused by the product's own read path",
                {
                    "compared_against": "live",
                    "restored_read_rejection": rejection.code,
                    "http_status": rejection.status,
                    "source_bytes_match": False,
                },
            )

        differing = sorted(
            key for key in ("detail", "revision", "sources") if original[key] != copy[key]
        )
        live_rows = {
            name: self._table_rows(live_database, name) for name in EXPECTED_IMMUTABLE_TABLES
        }
        restored_rows = {
            name: self._table_rows(database, name) for name in EXPECTED_IMMUTABLE_TABLES
        }
        tables = sorted(
            name for name in EXPECTED_IMMUTABLE_TABLES if live_rows[name] != restored_rows[name]
        )
        live_tree, restored_tree = tree_digest(live_artifacts), tree_digest(artifacts)
        shared = set(live_tree) & set(restored_tree)
        changed = sorted(
            (set(live_tree) ^ set(restored_tree))
            | {name for name in shared if live_tree[name] != restored_tree[name]}
        )
        detail = {
            "compared_against": "live",
            "read_through": ["get_case", "revision", "source_content"],
            "case_id_known": bool(case_id),
            "restored_revision": copy["revision"].get("revision"),
            "restored_revision_sha256": copy["revision"].get("snapshot_sha256"),
            "restored_sources": sorted(copy["sources"]),
            "product_reads_differing_from_live": differing,
            "tables_differing_from_live": tables,
            "artifact_files": len(restored_tree),
            "artifact_paths_differing_from_live": changed,
            "source_bytes_match": not changed and bool(restored_tree),
            "reopened_by_real_store": True,
        }
        if differing or tables or changed or not copy["sources"]:
            return Finding(
                "restore.rehearsal",
                "backup_restore",
                LOCAL,
                FAILED,
                "a restored copy did not match the live original it claims to restore",
                detail,
            )
        return Finding(
            "restore.rehearsal",
            "backup_restore",
            LOCAL,
            PASSED,
            "a restored case reread through get_case, revision and source_content "
            "matched the live original byte for byte",
            detail,
        )

    @staticmethod
    def _table_rows(database: Path, table: str) -> list[tuple[Any, ...]]:
        with sqlite3.connect(database) as db:
            return sorted(db.execute(f"SELECT * FROM {table}").fetchall())

    # ---------------------------------------------------------------- #
    # rollback
    # ---------------------------------------------------------------- #

    def check_rollback_surface(self) -> Finding:
        api = (self.root / "apps/api/src/proofops_api/main.py").read_text(encoding="utf-8")
        composition = (self.root / "apps/api/src/proofops_api/composition.py").read_text(
            encoding="utf-8"
        )
        web = (self.root / "apps/web/src/App.tsx").read_text(encoding="utf-8")
        detail = {
            "api_router_mounts": api.count("build_reconciliation_router("),
            "api_router_imports": api.count("from proofops_api.routers.reconciliation import"),
            "composition_store_references": composition.count("LocalReconciliationStore"),
            "web_feature_references": web.count("features/reconciliation"),
            "web_feature_directory": "apps/web/src/features/reconciliation",
        }
        if api.count("build_reconciliation_router(") != 1 or (
            composition.count("LocalReconciliationStore") < 1
        ):
            return Finding(
                "rollback.surface_is_bounded",
                "rollback",
                LOCAL,
                FAILED,
                "the reconciliation surface is not a single removable mount point",
                detail,
            )
        return Finding(
            "rollback.surface_is_bounded",
            "rollback",
            LOCAL,
            PASSED,
            "the surface is one API router mount, one store wiring and one web feature",
            detail,
        )

    def check_rollback_data_independence(self, workspace: Path) -> Finding:
        database = workspace / "live" / "state.sqlite3"
        with sqlite3.connect(database) as db:
            objects = self._schema_objects(db)
        prefix = "reconciliation_"
        reconciliation = {n: sql for n, sql in objects.items() if n.startswith(prefix)}
        existing = {n: sql for n, sql in objects.items() if not n.startswith(prefix)}
        inbound = sorted(
            name
            for name, sql in existing.items()
            if re.search(r"REFERENCES\s+reconciliation_", sql, re.IGNORECASE)
        )
        outbound = sorted(
            name
            for name, sql in reconciliation.items()
            if re.search(r"REFERENCES\s+(?!reconciliation_)", sql, re.IGNORECASE)
        )
        detail = {
            "reconciliation_objects": len(reconciliation),
            "existing_objects": len(existing),
            "existing_objects_referencing_reconciliation": inbound,
            "reconciliation_objects_referencing_existing": outbound,
        }
        if inbound:
            return Finding(
                "rollback.data_independence",
                "rollback",
                LOCAL,
                FAILED,
                "existing tables reference reconciliation tables, so disabling would break them",
                detail,
            )
        return Finding(
            "rollback.data_independence",
            "rollback",
            LOCAL,
            PASSED,
            "no existing table depends on a reconciliation table; disabling leaves data readable",
            detail,
        )

    # ---------------------------------------------------------------- #
    # contract
    # ---------------------------------------------------------------- #

    def check_route_parity(self) -> Finding:
        import yaml  # type: ignore[import-untyped]

        for part in ("packages", "apps/api/src"):
            entry = str(self.root / part)
            if entry not in sys.path:
                sys.path.insert(0, entry)
        from proofops_api.routers.reconciliation import build_reconciliation_router

        router = build_reconciliation_router(None, None, allowed_origin="http://localhost:5173")
        served = {
            (method, route.path)
            for route in router.routes
            for method in sorted(getattr(route, "methods", ()) or ())
        }
        document = yaml.safe_load(
            (self.root / "contracts/reconciliation/product.openapi.yaml").read_text(
                encoding="utf-8"
            )
        )
        declared = {
            (method.upper(), path)
            for path, operations in (document.get("paths") or {}).items()
            for method in operations
            if method.lower() in ("get", "post", "put", "patch", "delete")
        }
        detail = {
            "served": sorted(f"{method} {path}" for method, path in served),
            "undocumented": sorted(f"{method} {path}" for method, path in served - declared),
            "unimplemented": sorted(f"{method} {path}" for method, path in declared - served),
        }
        if served != declared or not served:
            return Finding(
                "contract.route_parity",
                "contract",
                LOCAL,
                FAILED,
                "the served routes and the published contract fragment disagree",
                detail,
            )
        return Finding(
            "contract.route_parity",
            "contract",
            LOCAL,
            PASSED,
            "every served route is declared in the contract fragment and vice versa",
            detail,
        )

    def check_contract_examples(self) -> Finding:
        entry = str(self.root / "packages")
        if entry not in sys.path:
            sys.path.insert(0, entry)
        from proofops.application.reconciliation.schema import validate_schema

        examples = sorted((self.root / "contracts/reconciliation/examples").glob("*.json"))
        invalid: list[str] = []
        outcomes: dict[str, str] = {}
        for example in examples:
            payload = json.loads(example.read_text(encoding="utf-8"))["expected"]
            outcomes[example.stem] = f"{payload['execution_state']}/{payload['status']}"
            try:
                validate_schema("output", payload)
            except Exception as exc:
                invalid.append(f"{example.stem}:{type(exc).__name__}")
        detail = {"examples": len(examples), "outcomes": outcomes, "invalid": invalid}
        if not examples or invalid:
            return Finding(
                "contract.examples_validate",
                "contract",
                LOCAL,
                FAILED,
                "a published example does not validate against the strict output schema",
                detail,
            )
        return Finding(
            "contract.examples_validate",
            "contract",
            LOCAL,
            PASSED,
            "every published example validates against the strict output schema",
            detail,
        )

    # ---------------------------------------------------------------- #
    # frontend
    # ---------------------------------------------------------------- #

    def check_frontend_inputs(self, pins: dict[str, str]) -> Finding:
        package = json.loads((self.root / "apps/web/package.json").read_text(encoding="utf-8"))
        scripts = package.get("scripts", {})
        dist = self.root / "apps/web/dist"
        detail = {
            "scripts": sorted(scripts),
            "has_build": "build" in scripts,
            "has_typecheck": "typecheck" in scripts,
            "lockfile": (self.root / "pnpm-lock.yaml").is_file(),
            "node_modules_present": (self.root / "apps/web/node_modules").is_dir(),
            "previous_build_present": (dist / "index.html").is_file(),
            "pnpm_pin": pins.get("pnpm"),
        }
        missing = [name for name in ("build", "typecheck") if name not in scripts]
        detail["missing_scripts"] = missing
        if missing or not detail["lockfile"]:
            return Finding(
                "frontend.build_inputs",
                "frontend",
                LOCAL,
                FAILED,
                "the web workspace is missing a build script or its lockfile",
                detail,
            )
        if not detail["node_modules_present"]:
            return Finding(
                "frontend.build_inputs",
                "frontend",
                LOCAL,
                BLOCKED,
                "web dependencies are not installed and this tool never installs them",
                detail,
                external_input=(
                    f"Run pnpm {pins.get('pnpm', '')} install --frozen-lockfile on the host."
                ),
            )
        return Finding(
            "frontend.build_inputs",
            "frontend",
            LOCAL,
            PASSED,
            "the web workspace has its scripts, lockfile and installed dependencies",
            detail,
        )

    # ---------------------------------------------------------------- #
    # application wiring
    # ---------------------------------------------------------------- #

    def check_application_wiring(self, workspace: Path) -> Finding:
        """Build the real FastAPI application against a throwaway local database.

        The database path is redirected before the module is imported, so the
        repository's own `.local` state is never opened, created or modified.
        """
        for part in ("packages", "apps/api/src"):
            entry = str(self.root / part)
            if entry not in sys.path:
                sys.path.insert(0, entry)
        sandbox = workspace / "app"
        sandbox.mkdir(parents=True, exist_ok=True)
        overrides = {
            "APP_ENV": "local",
            "APP_ORIGIN": "http://localhost:5173",
            "API_PUBLIC_BASE_URL": "http://localhost:8000",
            "MODEL_ADAPTER": "synthetic",
            "LOCAL_DATABASE_PATH": str(sandbox / "state.sqlite3"),
            "LOCAL_ARTIFACT_DIR": str(sandbox / "artifacts"),
        }
        previous = {name: os.environ.get(name) for name in overrides}
        os.environ.update(overrides)
        try:
            from proofops_api.main import create_app

            application = create_app()
            # FastAPI wraps included routers lazily, so the served OpenAPI
            # document is the authoritative list of routes, not `app.routes`.
            document = application.openapi()
            paths = {
                f"{method.upper()} {path}"
                for path, operations in (document.get("paths") or {}).items()
                for method in operations
                if method.lower() in ("get", "post", "put", "patch", "delete")
            }
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
        required = {
            "GET /v1/health/live",
            "GET /v1/health/ready",
            "GET /v1/reconciliation/cases/{case_id}",
            "POST /v1/reconciliation/cases/{case_id}/evaluate",
        }
        missing = sorted(required - paths)
        created = sorted(path.name for path in sandbox.iterdir())
        detail = {
            "total_routes": len(paths),
            "required_routes_missing": missing,
            "sandbox_files_created": created,
            "repository_state_touched": False,
        }
        if missing:
            return Finding(
                "application.wiring",
                "application",
                LOCAL,
                FAILED,
                "the assembled application does not expose the required local routes",
                detail,
            )
        return Finding(
            "application.wiring",
            "application",
            LOCAL,
            PASSED,
            "the real application assembles against a throwaway database and exposes its routes",
            detail,
        )

    # ---------------------------------------------------------------- #
    # verification inputs
    # ---------------------------------------------------------------- #

    def check_release_verifier_inputs(self) -> Finding:
        """The release verifier is the gate; confirm every path it names still exists."""
        module = load_module(
            self.root / "scripts/verify_reconciliation.py", "verify_reconciliation"
        )
        declared = (
            *module.RECONCILIATION_PATHS,
            *module.PRODUCT_PATHS,
            *module.NEW_SOURCE_PATHS,
        )
        missing = [name for name in declared if not (self.root / name).exists()]
        examples = sorted((self.root / "contracts/reconciliation/examples").glob("*.json"))
        detail = {
            "declared_paths": len(declared),
            "missing_paths": missing,
            "synthetic_cases": len(examples),
            "expected_synthetic_cases": 8,
            "verifier_executed_here": False,
        }
        if missing or len(examples) != 8:
            return Finding(
                "verification.release_verifier_inputs",
                "verification",
                VERIFICATION,
                FAILED,
                "the release verifier names paths or cases that no longer exist",
                detail,
            )
        return Finding(
            "verification.release_verifier_inputs",
            "verification",
            VERIFICATION,
            PASSED,
            "every path and synthetic case the release verifier gates on is present",
            detail,
        )

    def check_test_inventory(self) -> Finding:
        """Confirm the suites the release gate runs are present and non-empty."""
        suites = {
            "tests/reconciliation": sorted(
                path.name for path in (self.root / "tests/reconciliation").glob("test_*.py")
            ),
            "tests/acceptance/test_rules.py": (
                self.root / "tests/acceptance/test_rules.py"
            ).is_file(),
            "tests/acceptance/test_upload.py": (
                self.root / "tests/acceptance/test_upload.py"
            ).is_file(),
            "tests/acceptance/test_upload_security.py": (
                self.root / "tests/acceptance/test_upload_security.py"
            ).is_file(),
            "tests/acceptance/test_parsing.py": (
                self.root / "tests/acceptance/test_parsing.py"
            ).is_file(),
        }
        modules = sorted(
            path.name for path in (self.root / "tests/reconciliation").glob("test_*.py")
        )
        missing = [name for name, present in suites.items() if present is False]
        detail = {
            "reconciliation_test_modules": len(modules),
            "modules": modules,
            "missing_gate_suites": missing,
            "tests_executed_here": False,
        }
        if missing or not modules:
            return Finding(
                "verification.test_inventory",
                "verification",
                VERIFICATION,
                FAILED,
                "a suite the release gate runs is missing from the tree",
                detail,
            )
        return Finding(
            "verification.test_inventory",
            "verification",
            VERIFICATION,
            PASSED,
            "every suite the release gate runs is present; execution is a separate command",
            detail,
        )

    # ---------------------------------------------------------------- #
    # cloud boundary
    # ---------------------------------------------------------------- #

    def check_non_local_refused(self) -> Finding:
        entry = str(self.root / "packages")
        if entry not in sys.path:
            sys.path.insert(0, entry)
        from proofops.composition import AdapterRejectedError, build_composition

        outcomes: dict[str, str] = {}
        for app_env in ("local", "staging", "production"):
            try:
                build_composition(app_env=app_env, model_adapter="synthetic")
            except AdapterRejectedError:
                outcomes[app_env] = "refused"
            except Exception as exc:
                outcomes[app_env] = f"error:{type(exc).__name__}"
            else:
                outcomes[app_env] = "accepted"
        detail = {"composition": outcomes}
        if outcomes.get("local") != "accepted":
            return Finding(
                "cloud.local_only_boundary",
                "cloud",
                LOCAL,
                FAILED,
                "the local composition no longer builds",
                detail,
            )
        if outcomes.get("staging") != "refused" or outcomes.get("production") != "refused":
            return Finding(
                "cloud.local_only_boundary",
                "cloud",
                CLOUD,
                FAILED,
                "a non-local environment was accepted without real adapters",
                detail,
            )
        return Finding(
            "cloud.local_only_boundary",
            "cloud",
            CLOUD,
            BLOCKED,
            "the product refuses staging and production by design; it is local-only today",
            detail,
            external_input=(
                "Real cloud adapters, an account, approved bindings and a deployment "
                "decision. No part of that exists in this repository."
            ),
        )

    def check_staging_gate_inputs(self) -> Finding:
        manifest = self.root / "evidence/staging/manifest.json"
        approval = self.root / "evidence/staging/approval.json"
        detail = {
            "gate_script": "infra/cdk/staging_gate.py",
            "gate_script_present": (self.root / "infra/cdk/staging_gate.py").is_file(),
            "manifest_present": manifest.exists(),
            "approval_present": approval.exists(),
            "gate_executed_here": False,
        }
        return Finding(
            "cloud.staging_gate_inputs",
            "cloud",
            CLOUD,
            NOT_RUN,
            "the staging evidence gate has no bundle to verify and was not executed",
            detail,
            external_input=(
                "A staging evidence bundle and a detached approval from the protected "
                "review channel, produced by whoever holds the account."
            ),
        )

    def check_live_operations_not_attempted(self) -> Finding:
        return Finding(
            "cloud.live_operations",
            "cloud",
            CLOUD,
            NOT_RUN,
            "no AWS call, model invocation, deployment or DART collection was attempted",
            {
                "aws_api_calls": 0,
                "model_invocations": 0,
                "outbound_network_calls": 0,
                "dependency_installations": 0,
                "files_written_outside_output_and_temp": 0,
            },
            external_input="An authorized operator performs any live operation, not this tool.",
        )

    # ---------------------------------------------------------------- #

    def run(self) -> dict[str, Any]:
        pins: dict[str, str] = {}
        documented: dict[str, DocumentedVariable] = {}
        try:
            pins = parse_ci_pins(self.root / CI_WORKFLOW)
        except (OSError, ValueError, ImportError) as exc:
            self.add(
                Finding(
                    "toolchain.ci_pins",
                    "toolchain",
                    LOCAL,
                    FAILED,
                    "the CI workflow toolchain pins could not be read",
                    {"error": type(exc).__name__},
                )
            )
        else:
            self.add(
                Finding(
                    "toolchain.ci_pins",
                    "toolchain",
                    LOCAL,
                    PASSED if pins else FAILED,
                    "toolchain versions were taken from the committed CI workflow",
                    {"pins": pins, "source": CI_WORKFLOW},
                )
            )
        try:
            documented = parse_env_doc(self.root / ENV_DOC)
        except OSError as exc:
            self.add(
                Finding(
                    "configuration.env_specification",
                    "configuration",
                    LOCAL,
                    FAILED,
                    "the environment specification could not be read",
                    {"error": type(exc).__name__},
                )
            )

        self.guarded("platform.supported", "platform", LOCAL, self.check_platform)
        self.guarded("toolchain.python", "toolchain", LOCAL, self.check_python)
        self.guarded(
            "toolchain.uv",
            "toolchain",
            LOCAL,
            lambda: self.check_pinned_tool("uv", ["uv", "--version"], pins, LOCAL),
        )
        self.guarded(
            "toolchain.node",
            "toolchain",
            LOCAL,
            lambda: self.check_pinned_tool("node", ["node", "--version"], pins, LOCAL),
        )
        self.guarded(
            "toolchain.pnpm",
            "toolchain",
            LOCAL,
            lambda: self.check_pinned_tool("pnpm", ["pnpm", "--version"], pins, LOCAL),
        )
        self.guarded("toolchain.java", "toolchain", VERIFICATION, lambda: self.check_java(pins))

        self.guarded("repository.required_paths", "repository", LOCAL, self.check_required_paths)
        self.guarded(
            "repository.cloud_artifacts", "repository", CLOUD, self.check_deployment_artifacts
        )

        self.guarded(
            "configuration.env_contract",
            "configuration",
            LOCAL,
            lambda: self.check_env_contract(documented),
        )
        self.guarded(
            "configuration.local_profile", "configuration", LOCAL, self.check_local_profile
        )
        self.guarded(
            "configuration.cloud_variables",
            "configuration",
            CLOUD,
            lambda: self.check_cloud_variables(documented),
        )
        self.guarded(
            "configuration.live_model_artifacts",
            "configuration",
            CLOUD,
            self.check_live_model_artifacts,
        )
        self.guarded(
            "configuration.real_env_files", "configuration", LOCAL, self.check_unread_env_files
        )
        self.guarded(
            "configuration.example_secrets", "configuration", LOCAL, self.check_example_secrets
        )
        self.guarded(
            "configuration.loopback_ports", "configuration", LOCAL, self.check_loopback_ports
        )

        with owned_workspace() as workspace:
            try:
                self.rehearse_database(workspace)
            except Exception as exc:
                self.add(
                    Finding(
                        "database.rehearsal",
                        "database",
                        LOCAL,
                        FAILED,
                        "the offline database, backup and restore rehearsal could not complete",
                        {"error": type(exc).__name__},
                    )
                )
            self.guarded(
                "rollback.data_independence",
                "rollback",
                LOCAL,
                lambda: self.check_rollback_data_independence(workspace),
            )
            self.guarded(
                "application.wiring",
                "application",
                LOCAL,
                lambda: self.check_application_wiring(workspace),
            )

        self.guarded("rollback.surface_is_bounded", "rollback", LOCAL, self.check_rollback_surface)
        self.guarded(
            "verification.release_verifier_inputs",
            "verification",
            VERIFICATION,
            self.check_release_verifier_inputs,
        )
        self.guarded(
            "verification.test_inventory", "verification", VERIFICATION, self.check_test_inventory
        )
        self.guarded("contract.route_parity", "contract", LOCAL, self.check_route_parity)
        self.guarded("contract.examples_validate", "contract", LOCAL, self.check_contract_examples)
        self.guarded(
            "frontend.build_inputs", "frontend", LOCAL, lambda: self.check_frontend_inputs(pins)
        )
        self.guarded("cloud.local_only_boundary", "cloud", CLOUD, self.check_non_local_refused)
        self.guarded("cloud.staging_gate_inputs", "cloud", CLOUD, self.check_staging_gate_inputs)
        self.guarded(
            "cloud.live_operations", "cloud", CLOUD, self.check_live_operations_not_attempted
        )
        return build_report(self.findings, self.root)


def build_report(findings: Sequence[Finding], root: Path) -> dict[str, Any]:
    """Aggregate findings per target. A target is ready only if nothing blocks it."""
    records = [finding.to_dict() for finding in findings]
    gates: dict[str, Any] = {}
    for target in TARGETS:
        scoped = [record for record in records if record["target"] == target]
        blocking = [record["check"] for record in scoped if record["blocking"]]
        not_run = [record["check"] for record in scoped if record["status"] == NOT_RUN]
        passed = sum(record["status"] == PASSED for record in scoped)
        gates[target] = {
            "checks": len(scoped),
            "passed": passed,
            "blocked": sum(record["status"] == BLOCKED for record in scoped),
            "failed": sum(record["status"] == FAILED for record in scoped),
            "not_run": len(not_run),
            # Ready means every scoped check ran and passed. An unattempted
            # check is not evidence, so a not_run-only target is never ready.
            "ready": bool(scoped) and not blocking and not not_run and passed == len(scoped),
            "blocking_checks": blocking,
            "unattempted_checks": not_run,
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "repository": root.name,
        "product_scope": "local API + SQLite + React; no cloud deployment exists",
        "tool_authority": {
            "deploys": False,
            "calls_aws": False,
            "calls_models": False,
            "network_egress": False,
            "installs_dependencies": False,
            "reads_real_env_files": False,
            "prints_credentials": False,
        },
        "host": {
            "sys_platform": sys.platform,
            "machine": platform.machine().lower(),
            "python": platform.python_version(),
        },
        "gates": gates,
        "summary": {
            "total": len(records),
            "passed": sum(record["status"] == PASSED for record in records),
            "blocked": sum(record["status"] == BLOCKED for record in records),
            "failed": sum(record["status"] == FAILED for record in records),
            "not_run": sum(record["status"] == NOT_RUN for record in records),
        },
        "external_inputs_required": sorted(
            {
                record["external_input"]
                for record in records
                if record["external_input"] and record["blocking"]
            }
        ),
        "findings": records,
    }


def exit_code(report: dict[str, Any]) -> int:
    """0 only when the local product gates hold. Cloud is never a success signal."""
    if report["summary"]["failed"]:
        return 1
    local_gates = (report["gates"][LOCAL], report["gates"][VERIFICATION])
    return 0 if all(gate["ready"] for gate in local_gates) else 1


def write_json_new(path: Path, value: Any) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def render_markdown(report: dict[str, Any]) -> str:
    """Render the same findings as a short operator-readable summary."""
    lines = [
        "# Reconciliation deployment readiness",
        "",
        f"Generated {report['generated_at']} on "
        f"{report['host']['sys_platform']}/{report['host']['machine']}, "
        f"Python {report['host']['python']}.",
        "",
        "This report is an inspection. Nothing was deployed, no AWS API or model was",
        "called, no dependency was installed and no credential was read or printed.",
        "",
        "## Gates",
        "",
        "| Target | Ready | Passed | Blocked | Failed | Not run |",
        "|---|---|---|---|---|---|",
    ]
    for target in TARGETS:
        gate = report["gates"][target]
        lines.append(
            f"| `{target}` | {'yes' if gate['ready'] else 'no'} | {gate['passed']} | "
            f"{gate['blocked']} | {gate['failed']} | {gate['not_run']} |"
        )
    lines += ["", "## Findings", "", "| Check | Target | Status | Summary |", "|---|---|---|---|"]
    for record in report["findings"]:
        lines.append(
            f"| `{record['check']}` | `{record['target']}` | "
            f"**{record['status']}** | {record['summary']} |"
        )
    inputs: Iterable[str] = report["external_inputs_required"]
    lines += ["", "## External inputs still required", ""]
    lines += [f"- {item}" for item in inputs] or ["- None."]
    lines.append("")
    return "\n".join(lines)


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument(
        "--output",
        required=True,
        type=Path,
        help="New evidence directory; an existing path is rejected",
    )
    cli.add_argument(
        "--root",
        type=Path,
        default=ROOT,
        help="Repository root to inspect (default: this script's repository)",
    )
    return cli


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        args.output.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        print(f"output directory already exists: {args.output}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"cannot create output directory: {exc}", file=sys.stderr)
        return 2
    checker = ReadinessChecker(args.root.resolve())
    report = checker.run()
    output = args.output.resolve()
    write_json_new(output / "readiness.json", report)
    with (output / "readiness.md").open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(render_markdown(report))
    print(
        json.dumps(
            {key: report[key] for key in ("summary", "gates", "external_inputs_required")},
            ensure_ascii=False,
        ),
        flush=True,
    )
    return exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())
