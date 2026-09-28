"""Build-time supply-chain gate; no cloud/model calls.

Consumes trusted repository artifacts, never request-supplied approval records.
SBOM is a lock inventory, not legal approval or deployed-image attestation.
PyYAML is build tooling: missing tooling blocks verification, never bypasses it.
"""

from __future__ import annotations

import ast
import json
import os
import re
import tomllib
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from pathlib import Path
from urllib.parse import quote, urlparse

_SBOM_CANDIDATES = ("sbom.json", "bom.json", "sbom.cdx.json", "evidence/sbom.json")
_IGNORE_DIRS = {".git", ".venv", "node_modules", ".local", "__pycache__"}
# ponytail: common credential signatures only; expand scanning if coverage requires it.
_SECRET_PATTERNS = (
    re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"aws_secret_access_key\s*=\s*['\"]?[A-Za-z0-9/+=]{20,}", re.I),
    re.compile(r"ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,}"),
)


@dataclass(frozen=True, slots=True)
class SupplyChainResult:
    passed: bool
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


def _yaml(path: Path) -> object:
    import yaml  # type: ignore[import-untyped]

    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _inventory(root: Path) -> set[tuple[str, str, str]]:
    """Read exact (ecosystem, name, version) identities from both lockfiles."""
    inventory: set[tuple[str, str, str]] = set()
    for name in ("uv.lock", "pnpm-lock.yaml"):
        path = root / name
        if not path.is_file():
            raise ValueError(f"lockfile missing: {name}")
        try:
            if name == "uv.lock":
                data = tomllib.loads(path.read_text(encoding="utf-8"))
                packages = data.get("package")
                if (
                    type(data.get("version")) is not int
                    or data.get("version") != 1
                    or not isinstance(packages, list)
                    or not packages
                ):
                    raise ValueError
                for package in packages:
                    package_name, version = package["name"], package["version"]
                    if not all(isinstance(v, str) and v.strip() for v in (package_name, version)):
                        raise ValueError
                    inventory.add(("pypi", re.sub(r"[-_.]+", "-", package_name.lower()), version))
            else:
                data = _yaml(path)  # type: ignore[assignment]
                if not isinstance(data, dict) or str(data.get("lockfileVersion")) != "9.0":
                    raise ValueError
                if not isinstance(data.get("importers"), dict):
                    raise ValueError
                packages = data.get("packages", {})
                if not isinstance(packages, dict):
                    raise ValueError
                for key in packages:
                    package_name, separator, version = key.rpartition("@")
                    if (
                        not separator
                        or not package_name
                        or not re.fullmatch(r"\d+\.\d+\.\d+[^\s]*", version)
                    ):
                        raise ValueError
                    inventory.add(("npm", package_name, version))
        except Exception as exc:
            # Parser errors can echo credentials from input: report only artifact + error type.
            raise ValueError(f"lockfile invalid: {name} ({type(exc).__name__})") from None
    return inventory


def generate_sbom(root: Path) -> dict:
    """Produce CycloneDX lock inventory with exact versions and lock hashes."""
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "name": "proofops-lock-inventory",
                "description": (
                    "Dependency lock inventory; not a license approval " "or image attestation."
                ),
            },
            "properties": [
                {
                    "name": f"proofops:{name}:sha256",
                    "value": sha256((root / name).read_bytes()).hexdigest(),
                }
                for name in ("uv.lock", "pnpm-lock.yaml")
            ],
        },
        "components": [
            {
                "type": "library",
                "name": name,
                "version": version,
                "purl": f"pkg:{ecosystem}/{quote(name, safe='/')}@{quote(version, safe='')}",
            }
            for ecosystem, name, version in sorted(_inventory(root))
        ],
    }


def _check_sbom(root: Path, inventory: set[tuple[str, str, str]]) -> list[str]:
    path = next((root / p for p in _SBOM_CANDIDATES if (root / p).is_file()), None)
    if path is None:
        return ["SBOM missing: generate sbom.json from lockfiles"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("bomFormat") != "CycloneDX":
            raise ValueError
        components = data.get("components")
        if not isinstance(components, list):
            raise ValueError
        if not components:
            return ["SBOM invalid: empty components"]
        if (
            data.get("specVersion") != "1.6"
            or type(data.get("version")) is not int
            or data.get("version") != 1
        ):
            raise ValueError
        observed = set()
        for component in components:
            if not isinstance(component, dict):
                raise ValueError
            name, version = component.get("name"), component.get("version")
            if not isinstance(name, str) or not name.strip():
                raise ValueError
            if not isinstance(version, str) or not version.strip():
                raise ValueError
            purl = component.get("purl", "")
            # Require ecosystem identity: same package name can exist on PyPI and npm.
            ecosystem = (
                "pypi"
                if purl.startswith("pkg:pypi/")
                else "npm"
                if purl.startswith("pkg:npm/")
                else None
            )
            if ecosystem is None:
                raise ValueError
            expected = f"pkg:{ecosystem}/{quote(name, safe='/')}@{quote(version, safe='')}"
            if purl != expected:
                raise ValueError
            observed.add((ecosystem, name, version))
        if observed != inventory:
            return [
                f"SBOM inventory gap: {len(inventory - observed)} missing, "
                f"{len(observed - inventory)} unexpected name/version identities"
            ]
        properties = data.get("metadata", {}).get("properties", [])
        recorded = {p["name"]: p["value"] for p in properties}
        for name in ("uv.lock", "pnpm-lock.yaml"):
            if (
                recorded.get(f"proofops:{name}:sha256")
                != sha256((root / name).read_bytes()).hexdigest()
            ):
                return [f"SBOM lock hash mismatch: {name}; regenerate inventory"]
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        return ["SBOM invalid: malformed CycloneDX inventory"]
    return []


def _approval(root: Path) -> bool:
    """Validate approval evidence shape; no automated legal decision is made."""
    try:
        data = _yaml(root / "config/license_decisions.yaml")
        if not isinstance(data, dict) or not isinstance(data.get("decisions"), list):
            return False
        entries = [
            e for e in data["decisions"] if isinstance(e, dict) and e.get("name") == "pymupdf"
        ]
        if len(entries) != 1:
            return False
        entry = entries[0]
        if entry.get("decision") != "approved":
            return False
        if not all(
            isinstance(entry.get(k), str) and entry[k].strip()
            for k in ("approved_by", "approved_at", "source", "license")
        ):
            return False
        date.fromisoformat(entry["approved_at"])
        source = urlparse(entry["source"])
        return (
            entry["license"] in {"AGPL-3.0-only", "AGPL-3.0-or-later", "commercial"}
            and source.scheme == "https"
            and bool(source.netloc)
        )
    except Exception:
        return False


def _files(root: Path):
    for directory, names, files in os.walk(root, followlinks=False):
        names[:] = [
            name
            for name in names
            if name not in _IGNORE_DIRS
            and not (Path(directory) == root and name == "legacy_reference")
        ]
        for name in names:
            path = Path(directory) / name
            if path.is_symlink():
                yield path
        for name in files:
            yield Path(directory) / name


def _check_sources(root: Path, check_secrets: bool) -> tuple[list[str], bool]:
    errors: list[str] = []
    pymupdf = False
    for path in _files(root):
        relative = path.relative_to(root)
        if path.is_symlink():
            errors.append(f"source scan: uninspected symlink {relative}")
            continue
        try:
            contents = path.read_bytes()
        except OSError:
            errors.append(f"source scan: unreadable file {relative}")
            continue
        text = contents.decode("utf-8", errors="replace")
        if check_secrets and any(pattern.search(text) for pattern in _SECRET_PATTERNS):
            errors.append(f"secret scan: potential secret in {relative} (redacted)")
        # tests may contain synthetic import fixtures; they are still scanned for secrets.
        if path.suffix != ".py" or relative.parts[0] == "tests":
            continue
        if path.name == "legacy_pymupdf.py":
            pymupdf = True
        try:
            tree = ast.parse(text)
        except SyntaxError:
            errors.append(f"source scan: invalid Python in {relative}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                pymupdf |= any(a.name.split(".")[0] in {"fitz", "pymupdf"} for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                pymupdf |= (node.module or "").split(".")[0] in {"fitz", "pymupdf"}
            elif isinstance(node, ast.Call) and node.args:
                function = node.func
                method = (
                    function.id
                    if isinstance(function, ast.Name)
                    else function.attr
                    if isinstance(function, ast.Attribute)
                    else ""
                )
                if method in {"__import__", "import_module"} and isinstance(
                    node.args[0], ast.Constant
                ):
                    pymupdf |= str(node.args[0].value).split(".")[0] in {"fitz", "pymupdf"}
    return errors, pymupdf


def verify_supply_chain(
    *,
    root_dir: Path | str,
    env: dict[str, str] | None = None,
    check_secrets: bool = True,
) -> SupplyChainResult:
    """Validate build artifacts; success alone never authorizes public deployment."""
    root = Path(root_dir)
    env = env or {}
    errors: list[str] = []
    inventory: set[tuple[str, str, str]] = set()
    try:
        inventory = _inventory(root)
    except ValueError as exc:
        errors.append(str(exc))
    errors.extend(_check_sbom(root, inventory))
    source_errors, pymupdf = _check_sources(root, check_secrets)
    errors.extend(source_errors)
    pymupdf |= any(
        ecosystem == "pypi" and name in {"pymupdf", "pymupdfb", "fitz"}
        for ecosystem, name, _ in inventory
    )
    flag = env.get("ENABLE_LEGACY_PYMUPDF", "false")
    if flag not in {"true", "false"}:
        errors.append("license gate: ENABLE_LEGACY_PYMUPDF must be true or false")
    if pymupdf or flag == "true":
        if not _approval(root):
            errors.append(
                "license gate: PyMuPDF requires verified approval evidence "
                "in config/license_decisions.yaml"
            )
        if flag != "true":
            errors.append("license gate: PyMuPDF present while ENABLE_LEGACY_PYMUPDF is disabled")
    return SupplyChainResult(
        not errors,
        tuple(errors),
        (
            "Human license/data-rights approvals and deployed-image verification "
            "remain separate release gates.",
        ),
    )
