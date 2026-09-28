"""Import a prepared draft into an existing local run as a trusted local operator.

This command requires filesystem access to the server database. It records an
operator identity but does not grant review or policy approval. Never expose it
as an HTTP upload handler or accept its arguments from an untrusted client.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from uuid import UUID

from evaluation.reconciliation_cli import InputRejected, load_json


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--artifacts", required=True, type=Path)
    parser.add_argument("--tenant-id", required=True, type=UUID)
    parser.add_argument("--run-id", required=True, type=UUID)
    parser.add_argument("--claim-id", required=True, type=UUID)
    parser.add_argument("--actor", required=True, help="Local operator's auditable identity")
    args = parser.parse_args(argv)
    if not args.actor.strip() or not args.database.is_file():
        parser.error("an existing local database and nonempty operator identity are required")
    if os.environ.get("APP_ENV", "local") != "local":
        parser.error("draft import supports only the local composition")

    from proofops.application.authorization import AuthContext
    from proofops_api.composition import build_composition

    os.environ["LOCAL_DATABASE_PATH"] = str(args.database.resolve())
    composition = build_composition()
    try:
        bundle = {
            name: load_json(args.bundle / f"{name}.json")
            for name in ("packet", "policy", "documents", "artifacts", "coverage")
        }
        # Approval assertions from an imported file cannot authorize this case.
        bundle["policies"] = {}
        auth = AuthContext(
            args.actor.strip(),
            str(args.tenant_id),
            "admin",
            frozenset({"viewer", "editor", "reviewer", "admin"}),
            "local-operator-draft-import",
        )
        detail = composition.reconciliation.register_case(
            auth, str(args.run_id), str(args.claim_id), bundle, args.artifacts
        )
        print(
            json.dumps(
                {
                    "case_id": detail["case_id"],
                    "revision": detail["revision"],
                    "review_state": detail["review_state"],
                    "policy_approved": detail["policy_approved"],
                    "workspace_path": (
                        f"/runs/{args.run_id}/claims/{args.claim_id}/reconciliation"
                    ),
                }
            )
        )
        return 0
    except (InputRejected, ValueError, OSError) as exc:
        # Do not print arbitrary input contents, paths, or embedded credentials.
        print(json.dumps({"error": getattr(exc, "code", "draft_import_rejected")}), file=sys.stderr)
        return 2
    finally:
        composition.uploads.close()
        composition.registry.close()


if __name__ == "__main__":
    raise SystemExit(main())
