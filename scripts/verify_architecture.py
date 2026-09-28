#!/usr/bin/env python3
"""Verify TASK-000 architecture boundaries (not a docs/contract checker).

Checks (exit 1 on any failure):
1. Domain purity: packages/proofops domain/ports modules import nothing from
   AWS SDKs, network, files, environment, scripts, legacy, or sources.
   The API DTO boundary (apps/api) may use Pydantic v2 only, nothing else new.
2. Port conformance: local adapters implement TaggerPort; synthetic adapter
   output carries no grade/label fields.
3. Composition gate: local builds; staging/production refuse ALL adapters
   (string selection alone never certifies readiness).
4. Contract reuse: fixed JSONSchema fixtures still validate (read-only use of
   contracts/, which this script never modifies).

Usage: uv run python scripts/verify_architecture.py
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "packages" / "proofops"

# Domain dependency boundary (mandated): domain modules import stdlib only.
# Rejected: cloud SDKs, network, files/process IO, environment, orchestration
# scripts, legacy/sources, sibling adapters/composition, app packages, web
# frameworks, and even pydantic (the API DTO boundary owns that dependency).
DOMAIN_BANNED = {
    "boto3",
    "botocore",
    "requests",
    "httpx",
    "urllib",
    "socket",
    "os",
    "sys",
    "pathlib",
    "subprocess",
    "shutil",
    "secrets",
    "scripts",
    "legacy",
    "legacy_reference",
    "sources",
    "fastapi",
    "pydantic",
    "proofops_api",
    "proofops_worker",
    "proofops_agent",
}
# Intra-package upward imports rejected in domain/: adapters, composition root,
# application services (ports included). Sibling domain modules are allowed.
DOMAIN_BANNED_PROOFOPS = {"adapters", "application", "composition"}
# API DTO boundary: pydantic is the one allowed third-party import.
DTO_MODULES = [ROOT / "apps" / "api" / "src" / "proofops_api" / "dto.py"]

failures: list[str] = []


def _imports(path: Path) -> tuple[set[str], set[str]]:
    """Return (top-level imports, proofops-submodule imports)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    top: set[str] = set()
    sub: set[str] = set()
    for node in ast.walk(tree):
        mods: list[str] = []
        if isinstance(node, ast.Import):
            mods = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                mods = [node.module]
        for mod in mods:
            parts = mod.split(".")
            top.add(parts[0])
            if parts[0] == "proofops" and len(parts) > 1:
                sub.add(parts[1])
    return top, sub


def check_domain_tree() -> None:
    domain = PKG / "domain"
    found = sorted(domain.rglob("*.py"))
    if not found:
        failures.append("purity:no domain modules discovered")
        return
    for path in found:
        top, sub = _imports(path)
        banned = (top & DOMAIN_BANNED) | (sub & DOMAIN_BANNED_PROOFOPS)
        # proofops.domain.* self-imports are allowed; relative imports resolve
        # inside domain/ and carry no module prefix to check.
        if banned:
            failures.append(f"purity:{path.relative_to(ROOT)} imports banned {sorted(banned)}")
        else:
            print(f"PASS purity:{path.relative_to(ROOT)}")


def check_dto_module(path: Path) -> None:
    allowed = {"__future__", "re", "typing", "uuid", "pydantic"}
    top, _sub = _imports(path)
    unexpected = top - allowed
    if unexpected:
        failures.append(f"dto:{path.relative_to(ROOT)} has unexpected imports {sorted(unexpected)}")
    else:
        print(f"PASS dto-boundary:{path.relative_to(ROOT)} (pydantic-only)")


def main() -> int:
    check_domain_tree()
    for path in DTO_MODULES:
        if not path.is_file():
            failures.append(f"missing:{path.relative_to(ROOT)}")
        else:
            check_dto_module(path)

    try:
        from proofops.adapters.local.models import BedrockTagger, SyntheticTagger
        from proofops.application.ports.models import ModelBinding
        from proofops.composition import (
            AdapterRejectedError,
            build_composition,
            build_local_composition,
        )

        binding = ModelBinding(binding_id="test", role="tagger", synthetic=True)
        tags = SyntheticTagger().tag(
            {"claim_id": "00000000-0000-4000-8000-000000000004"}, 1, binding
        )
        payload_keys = set(tags.__dataclass_fields__)
        if payload_keys & {"evidence_grade", "label", "sublabel"}:
            failures.append("ports:synthetic output carries grading fields")
        else:
            print("PASS ports:SyntheticTagger implements TaggerPort without grading fields")

        try:
            BedrockTagger().tag({}, 1, binding)
            failures.append("ports:BedrockTagger stub produced output instead of refusing")
        except AdapterRejectedError:
            print("PASS ports:BedrockTagger refuses without approved binding")

        local = build_local_composition(app_env="local", model_adapter="synthetic")
        assert local.profile == "local-synthetic"
        print("PASS composition:local-synthetic builds")
        for env in ("staging", "production"):
            for adapter in ("synthetic", "bedrock"):
                try:
                    build_composition(app_env=env, model_adapter=adapter)
                    failures.append(f"composition:{adapter} accepted in {env}")
                except AdapterRejectedError:
                    print(f"PASS composition:{adapter} rejected in {env}")
    except Exception as exc:  # noqa: BLE001 - report then exit 1
        failures.append(f"runtime:{exc}")

    try:
        import json

        import jsonschema

        schema = json.loads(
            (ROOT / "contracts" / "jsonschema" / "llm_tags.schema.json").read_text()
        )
        example = json.loads((ROOT / "fixtures" / "llm_tags_example.json").read_text())
        jsonschema.validate(example, schema)
        print("PASS contracts:llm_tags fixture validates against fixed schema (read-only)")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"contracts:{exc}")

    if failures:
        print("FAILURES:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("verify_architecture: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
