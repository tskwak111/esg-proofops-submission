#!/usr/bin/env python3
"""Offline TASK-044 evidence verifier; performs no deploy, restore or approval.

The bundle and detached approval must come from a trusted, protected review/CI
channel. Hashes establish integrity, not reviewer identity or truth of a report.
Exit 0 means supplied evidence is complete; it never grants AWS authorization.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MAX_JSON_BYTES = 1024 * 1024
MAX_ARTIFACT_BYTES = 100 * 1024 * 1024

REQUIRED = {
    "cdk_synth": ("template_synthesized",),
    "supply_chain": ("locks", "sbom", "vulnerabilities", "secrets", "licenses"),
    "preflight": ("runtime_binding", "consent", "account_permissions", "live_model_probe"),
    "public_pdf": (
        "rights_approved",
        "source_verified",
        "three_distinct_replicas",
        "python_grades",
    ),
    "tenant_isolation": ("foreign_read_denied", "foreign_write_denied"),
    "review_conflict": ("stale_if_match_rejected", "old_revision_unchanged"),
    "export_integrity": ("snapshot_hash_verified", "old_export_unchanged"),
    "rollback": (
        "previous_images",
        "previous_binding",
        "rule_pointer",
        "index_alias",
        "schema_compatible",
    ),
    "restore": (
        "separate_environment",
        "source_hash",
        "rulepack_hash",
        "export_hash",
        "tenant_isolation",
        "tombstones_reapplied",
        "pending_leases_reset",
    ),
    "slo": ("query_p95_measured", "acceptance_p95_measured", "rpo_measured", "rto_measured"),
}
TARGETS = {
    "query_p95_ms": 1000,
    "acceptance_p95_ms": 2000,
    "rpo_seconds": 86400,
    "rto_seconds": 14400,
}


def require(condition: bool) -> None:
    if not condition:
        raise ValueError("invalid evidence")


def text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value == value.strip()


def digest(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def timestamp(value: Any, now: datetime) -> datetime:
    require(text(value))
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(parsed.tzinfo is not None and parsed <= now)
    return parsed


def object_from_bytes(data: bytes) -> dict[str, Any]:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result)
            result[key] = value
        return result

    value = json.loads(data, object_pairs_hook=unique)
    require(isinstance(value, dict))
    return value


def bounded_read(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    require(0 < len(data) <= limit)
    return data


def referenced(root: Path, ref: Any, limit: int = MAX_ARTIFACT_BYTES) -> bytes:
    require(isinstance(ref, dict) and text(ref.get("path")) and digest(ref.get("sha256")))
    relative = Path(ref["path"])
    require(not relative.is_absolute() and ".." not in relative.parts)
    target = (root / relative).resolve()
    require(target.is_relative_to(root.resolve()) and target.is_file())
    data = bounded_read(target, limit)
    require(bool(data) and hashlib.sha256(data).hexdigest() == ref["sha256"])
    return data


def release_valid(release: Any) -> None:
    require(isinstance(release, dict))
    require(release.get("environment") == "staging")
    require(
        all(
            text(release.get(key))
            for key in (
                "release_id",
                "tenant_id",
                "runtime_version",
                "index_alias",
            )
        )
    )
    require(
        isinstance(release.get("account_id"), str)
        and re.fullmatch(r"[0-9]{12}", release["account_id"]) is not None
    )
    require(
        isinstance(release.get("region"), str)
        and re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d+", release["region"]) is not None
    )
    require(all(digest(release.get(key)) for key in ("runtime_binding_sha256", "rulepack_sha256")))
    images = release.get("images")
    require(isinstance(images, dict) and set(images) == {"api", "worker", "agent"})
    require(
        all(
            isinstance(image, str)
            and re.fullmatch(r"[A-Za-z0-9./_-]+@sha256:[0-9a-f]{64}", image) is not None
            for image in images.values()
        )
    )


def verify(manifest_path: Path, approval_path: Path) -> dict[str, Any]:
    now = datetime.now(UTC)
    checks: list[dict[str, str]] = []

    def add(name: str, status: str, reason: str) -> None:
        checks.append({"name": name, "status": status, "reason": reason})

    manifest: dict[str, Any] = {}
    release_sha = None
    try:
        raw = bounded_read(manifest_path, MAX_JSON_BYTES)
        manifest = object_from_bytes(raw)
        require(type(manifest.get("schema_version")) is int and manifest["schema_version"] == 1)
        release, previous = manifest["release"], manifest["previous_release"]
        release_valid(release)
        release_valid(previous)
        require(release["release_id"] != previous["release_id"])
        require(all(release[k] == previous[k] for k in ("tenant_id", "account_id", "region")))
        require(isinstance(manifest.get("reports"), dict))
        release_sha = hashlib.sha256(
            json.dumps(
                release,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
        ).hexdigest()
        add("release", "pass", "immutable release and rollback scope verified")
        approval = object_from_bytes(bounded_read(approval_path, MAX_JSON_BYTES))
        require(approval.get("status") == "approved" and text(approval.get("approved_by")))
        require(approval.get("manifest_sha256") == hashlib.sha256(raw).hexdigest())
        timestamp(approval.get("approved_at"), now)
        add("approval_snapshot", "pass", "supplied detached approval matches exact bundle")
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        add("release_or_approval", "fail", "missing or invalid release/approval snapshot")

    reports = manifest.get("reports", {})
    if not isinstance(reports, dict):
        reports = {}
    for name, assertions in REQUIRED.items():
        if name not in reports:
            add(name, "not_run", "evidence not supplied")
            continue
        try:
            report = object_from_bytes(
                referenced(manifest_path.parent, reports[name], MAX_JSON_BYTES)
            )
            require(report.get("kind") == name and report.get("environment") == "staging")
            require(report.get("synthetic") is False)
            require(release_sha is not None and report.get("release_sha256") == release_sha)
            if report.get("status") in ("not_run", "blocked", "fail"):
                add(name, report["status"], "supplied evidence reports an incomplete gate")
                continue
            require(report.get("status") == "pass" and text(report.get("command")))
            require(type(report.get("exit_code")) is int and report["exit_code"] == 0)
            timestamp(report.get("checked_at"), now)
            facts = report.get("assertions")
            require(isinstance(facts, dict) and all(facts.get(key) is True for key in assertions))
            referenced(manifest_path.parent, report.get("log"))
            if name == "rollback":
                require(report.get("restored_release") == manifest.get("previous_release"))
            if name == "restore":
                artifacts = report.get("artifacts")
                if not isinstance(artifacts, dict):
                    raise ValueError("missing restore artifacts")
                for kind in ("source", "rulepack", "export"):
                    pair = artifacts[kind]
                    require(isinstance(pair, dict))
                    require(
                        referenced(manifest_path.parent, pair.get("original"))
                        == referenced(manifest_path.parent, pair.get("restored"))
                    )
            if name == "slo":
                measurements = report.get("measurements")
                if not isinstance(measurements, dict):
                    raise ValueError("missing measurements")
                for metric, target in TARGETS.items():
                    value = measurements.get(metric)
                    if not isinstance(value, int | float) or isinstance(value, bool):
                        raise ValueError("invalid measurement")
                    require(0 <= value <= target and math.isfinite(value))
            add(name, "pass", "supplied report, scope and log hashes verified")
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            add(name, "fail", "invalid, synthetic, mismatched or tampered evidence")
    return {
        "mode": "offline_evidence_verification",
        "aws_actions_performed": False,
        "evidence_complete": all(check["status"] == "pass" for check in checks),
        "checked_at": now.isoformat(),
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("evidence/staging/manifest.json"))
    parser.add_argument("--approval", type=Path, default=Path("evidence/staging/approval.json"))
    args = parser.parse_args()
    result = verify(args.manifest, args.approval)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result["evidence_complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
