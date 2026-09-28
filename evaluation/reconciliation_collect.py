"""Collect pinned DART originals into an immutable store and collection-1 manifest.

No policy approval or reconciliation verdict is inferred during collection.
Use DART_API_KEY in the environment; never pass credentials on the command line.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from proofops.adapters.dart import (
    ArtifactStore,
    DartAuthError,
    DartClient,
    DartError,
    DartNotFoundError,
    DartRateLimitError,
    build_collection_manifest,
    create_artifact_entry,
)

from evaluation.reconciliation_cli import write_json


def collect(
    client: DartClient,
    store: ArtifactStore,
    *,
    corp_code: str,
    fy: int,
    rcept_no: str,
    package_id: str,
    manifest_id: str,
    document_version_id: str,
    kinds: list[str],
    reprt_code: str = "11011",
    fs_div: str = "CFS",
    synthetic: bool = False,
) -> dict[str, Any]:
    if (
        not re.fullmatch(r"[0-9]{8}", corp_code)
        or not re.fullmatch(r"[0-9]{14}", rcept_no)
        or type(fy) is not int
        or not 1900 <= fy <= 9999
        or reprt_code not in {"11011", "11012", "11013", "11014"}
        or fs_div not in {"CFS", "OFS"}
        or not all(
            isinstance(v, str) and v.strip() for v in (package_id, manifest_id, document_version_id)
        )
        or type(synthetic) is not bool
        or not kinds
        or len(set(kinds)) != len(kinds)
        or any(k not in {"statements", "document", "xbrl"} for k in kinds)
    ):
        raise ValueError("invalid_collection_identity")
    entries = []
    for kind in kinds:
        status, error, digest, locator = "retrieved", None, None, None
        try:
            if kind == "statements":
                response = client.get_financial_statements(
                    corp_code, fy, reprt_code, fs_div, expected_rcept_no=rcept_no
                )
                if response.get("status") == "013":
                    raise DartNotFoundError("no_data")
                if response.get("status") != "000" or not response.get("list"):
                    raise DartError("invalid_statement_response")
                raw, ext = response.raw_bytes, "json"
            elif kind == "document":
                raw, ext = client.download_document(rcept_no), "zip"
            else:
                raw, ext = client.download_xbrl(rcept_no, reprt_code), "zip"
            digest = store.store(raw, ext=ext)
            locator = store.path_for(digest, ext=ext).relative_to(store.root).as_posix()
        except DartNotFoundError:
            status, error = "not_available", "no_data"
        except DartAuthError:
            status, error = "failed", "authentication_failed"
        except DartRateLimitError:
            status, error = "failed", "rate_limited"
        except (DartError, ValueError):
            status, error = "failed", "collection_or_identity_failed"
        except OSError:
            status, error = "failed", "artifact_storage_failed"
        entries.append(
            create_artifact_entry(
                source_id=f"{document_version_id}:{kind}",
                document_version_id=document_version_id,
                corp_code=corp_code,
                fy=fy,
                rcept_no=rcept_no,
                consolidation="consolidated" if fs_div == "CFS" else "separate",
                artifact_sha256=digest if status == "retrieved" else None,
                locator=locator if status == "retrieved" else None,
                status=status,
                error_code=error,
            )
        )
    return build_collection_manifest(manifest_id, package_id, entries, synthetic=synthetic)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corp-code", required=True)
    parser.add_argument("--fy", required=True, type=int)
    parser.add_argument("--rcept-no", required=True, help="Explicit pinned receipt, never latest")
    parser.add_argument("--package-id", required=True)
    parser.add_argument("--manifest-id", required=True)
    parser.add_argument("--document-version-id", required=True)
    parser.add_argument("--store", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--reprt-code", default="11011", choices=["11011", "11012", "11013", "11014"]
    )
    parser.add_argument("--fs-div", default="CFS", choices=["CFS", "OFS"])
    parser.add_argument(
        "--kinds",
        nargs="+",
        default=["statements", "document", "xbrl"],
        choices=["statements", "document", "xbrl"],
    )
    args = parser.parse_args(argv)
    try:
        if args.output.exists():
            raise ValueError("output_exists")
        if not os.environ.get("DART_API_KEY", "").strip():
            raise ValueError("api_key_missing")
        result = collect(
            DartClient(),
            ArtifactStore(args.store),
            corp_code=args.corp_code,
            fy=args.fy,
            rcept_no=args.rcept_no,
            package_id=args.package_id,
            manifest_id=args.manifest_id,
            document_version_id=args.document_version_id,
            kinds=args.kinds,
            reprt_code=args.reprt_code,
            fs_div=args.fs_div,
        )
        write_json(args.output, result)
        return 3 if any(e["status"] == "failed" for e in result["artifacts"]) else 0
    except (ValueError, FileExistsError, PermissionError):
        print(json.dumps({"error": "collection_input_rejected"}), file=sys.stderr)
        return 2
    except Exception:
        print(json.dumps({"error": "collection_failed"}), file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
