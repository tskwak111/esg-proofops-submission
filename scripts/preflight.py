#!/usr/bin/env python3
"""Offline TASK-029 CLI for a single approved RuntimeBinding artifact.

Reads metadata only; never grants consent, resolves credentials, or invokes a
model. Role registries/example templates are not approved binding artifacts.
Exit 0: supplied snapshots pass; exit 1: blocked. No live test is claimed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from proofops.application.authorization import AuthContext, TenantNotFoundError
from proofops.application.preflight import (
    Check,
    Preflight,
    check_runtime_binding,
    combine_build_checks,
)
from proofops.application.supply_chain import verify_supply_chain


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--binding", default=os.getenv("MODEL_BINDINGS_PATH", "config/model_bindings.json")
    )
    parser.add_argument(
        "--consent", default=os.getenv("CONSENT_PROFILE_PATH", "config/consent_profile.json")
    )
    parser.add_argument("--build-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--tenant-id", default="")
    parser.add_argument("--allowed-region", action="append", default=[])
    parser.add_argument("--checked-at", default=None)
    parser.add_argument("--include-live-model-probe", action="store_true")
    args = parser.parse_args(argv)
    checked_at = args.checked_at or datetime.now(UTC).isoformat()
    try:
        runtime = json.loads(Path(args.binding).read_text(encoding="utf-8"))
        consent = json.loads(Path(args.consent).read_text(encoding="utf-8"))
        if not isinstance(runtime, dict) or not isinstance(consent, dict):
            raise ValueError("profile must be an object")
        result = check_runtime_binding(
            binding=runtime,
            consent=consent,
            auth=AuthContext(
                "offline-admin", args.tenant_id, "admin", frozenset({"admin"}), "offline-cli"
            ),
            allowed_regions=args.allowed_region,
            checked_at=checked_at,
            include_live_model_probe=args.include_live_model_probe,
        )
    except (OSError, ValueError, TenantNotFoundError):
        result = Preflight(
            False,
            (
                Check(
                    "configuration", "fail", "missing, invalid or inaccessible approval artifacts"
                ),
                Check("live_model_probe", "not_run", "offline CLI never calls a model"),
            ),
            None,
            datetime.now(UTC).isoformat(),
        )
    result = combine_build_checks(
        result,
        verify_supply_chain(
            root_dir=Path(args.build_root),
            env={"ENABLE_LEGACY_PYMUPDF": os.getenv("ENABLE_LEGACY_PYMUPDF", "false")},
        ),
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False))
    return 0 if result.ready else 1


if __name__ == "__main__":
    sys.exit(main())
