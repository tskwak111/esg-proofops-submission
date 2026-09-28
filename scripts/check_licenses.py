#!/usr/bin/env python3
"""License and supply-chain gate (TASK-042 SEC-006).

Verifies:
- lockfiles present (uv.lock, pnpm-lock.yaml)
- SBOM present (sbom.json / evidence/sbom.json etc.)
- license decisions / PyMuPDF gate (ENABLE_LEGACY_PYMUPDF)
- optional secret scan

Usage:
    python scripts/check_licenses.py --root . --env ENABLE_LEGACY_PYMUPDF=false
    python scripts/check_licenses.py --help

Exits 0 when deployment gate passes, 1 when blocked.
Used by CI supply-chain job and locally.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure package is importable when running via uv run / python scripts/...
ROOT_DEFAULT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DEFAULT / "packages"))

from proofops.application.supply_chain import (  # noqa: E402
    generate_sbom,
    verify_supply_chain,
)


def parse_env_assignments(items: list[str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(f"--env expects KEY=VALUE, got: {item}")
        k, v = item.split("=", 1)
        env[k] = v
    return env


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ProofOps supply-chain / license gate (TASK-042)")
    parser.add_argument(
        "--root", default=str(ROOT_DEFAULT), help="Repository root to inspect (default: repo root)"
    )
    parser.add_argument(
        "--env",
        action="append",
        default=[],
        help="Environment flag override KEY=VALUE (repeatable), e.g. ENABLE_LEGACY_PYMUPDF=true",
    )
    parser.add_argument(
        "--skip-secrets",
        action="store_true",
        help="Skip secret scan (useful for hermetic tests)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON result to stdout instead of human text",
    )
    parser.add_argument(
        "--generate-sbom",
        action="store_true",
        help="Write exact lock inventory to sbom.json before verifying",
    )
    args = parser.parse_args(argv)

    root = Path(args.root)
    # Build env from explicit flags; also allow reading from real environment for convenience
    # Explicit flags win. Only pass ENABLE_LEGACY_PYMUPDF and APP_ENV if set either way.
    env: dict[str, str] = {}
    # Pull from real env if not overridden and relevant
    import os

    for key in ("ENABLE_LEGACY_PYMUPDF", "APP_ENV"):
        if key in os.environ:
            env[key] = os.environ[key]
    explicit = parse_env_assignments(args.env)
    env.update(explicit)

    if args.generate_sbom:
        import json

        try:
            content = generate_sbom(root)
        except (OSError, ValueError) as exc:
            print(f"SBOM generation blocked: {exc}", file=sys.stderr)
            return 1
        (root / "sbom.json").write_text(json.dumps(content, indent=2) + "\n", encoding="utf-8")

    result = verify_supply_chain(root_dir=root, env=env, check_secrets=not args.skip_secrets)

    if args.json:
        import json

        payload = {
            "passed": result.passed,
            "errors": list(result.errors),
            "warnings": list(result.warnings),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        if result.passed:
            print("supply-chain gate: PASSED")
            if result.warnings:
                for w in result.warnings:
                    print(f"WARN: {w}")
        else:
            print("supply-chain gate: BLOCKED")
            for e in result.errors:
                print(f"ERROR: {e}")
            if result.warnings:
                for w in result.warnings:
                    print(f"WARN: {w}")

    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
