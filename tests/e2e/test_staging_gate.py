"""TASK-044 gate acceptance; all evidence envelopes here are synthetic test data.

These tests exercise real file/hash/CLI validation, never AWS or approval authority.
A fixture's synthetic=False field tests validation of a claimed external report;
it is not a real deployment attestation and must never be published as one.
"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "infra/cdk/staging_gate.py"
NOW = "2026-09-08T00:00:00Z"
ASSERTIONS = {
    "cdk_synth": ["template_synthesized"],
    "supply_chain": ["locks", "sbom", "vulnerabilities", "secrets", "licenses"],
    "preflight": ["runtime_binding", "consent", "account_permissions", "live_model_probe"],
    "public_pdf": [
        "rights_approved",
        "source_verified",
        "three_distinct_replicas",
        "python_grades",
    ],
    "tenant_isolation": ["foreign_read_denied", "foreign_write_denied"],
    "review_conflict": ["stale_if_match_rejected", "old_revision_unchanged"],
    "export_integrity": ["snapshot_hash_verified", "old_export_unchanged"],
    "rollback": [
        "previous_images",
        "previous_binding",
        "rule_pointer",
        "index_alias",
        "schema_compatible",
    ],
    "restore": [
        "separate_environment",
        "source_hash",
        "rulepack_hash",
        "export_hash",
        "tenant_isolation",
        "tombstones_reapplied",
        "pending_leases_reset",
    ],
    "slo": ["query_p95_measured", "acceptance_p95_measured", "rpo_measured", "rto_measured"],
}


def reference(root, name, value):
    data = value if isinstance(value, bytes) else json.dumps(value, sort_keys=True).encode()
    (root / name).write_bytes(data)
    return {"path": name, "sha256": hashlib.sha256(data).hexdigest()}


def bundle(root):
    release = {
        "release_id": "synthetic-test-release",
        "environment": "staging",
        "account_id": "000000000000",
        "region": "ap-northeast-2",
        "tenant_id": "synthetic-tenant",
        "images": {
            name: "test.invalid/image@sha256:" + "a" * 64 for name in ("api", "worker", "agent")
        },
        "runtime_binding_sha256": "b" * 64,
        "runtime_version": "test-version-2",
        "rulepack_sha256": "c" * 64,
        "index_alias": "test-index-2",
    }
    previous = {
        **release,
        "release_id": "previous-test-release",
        "runtime_version": "test-version-1",
    }
    release_sha = hashlib.sha256(
        json.dumps(release, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    reports = {}
    for name, assertions in ASSERTIONS.items():
        report = {
            "kind": name,
            "release_sha256": release_sha,
            "status": "pass",
            "environment": "staging",
            "synthetic": False,
            "checked_at": NOW,
            "command": "synthetic test envelope only",
            "exit_code": 0,
            "assertions": dict.fromkeys(assertions, True),
            "log": reference(root, name + ".log", b"synthetic test log, not AWS evidence"),
        }
        if name == "rollback":
            report["restored_release"] = previous
        if name == "restore":
            report["artifacts"] = {
                kind: {
                    "original": reference(root, kind + "-before", b"synthetic content"),
                    "restored": reference(root, kind + "-after", b"synthetic content"),
                }
                for kind in ("source", "rulepack", "export")
            }
        if name == "slo":
            report["measurements"] = {
                "query_p95_ms": 100,
                "acceptance_p95_ms": 200,
                "rpo_seconds": 10,
                "rto_seconds": 20,
            }
        reports[name] = reference(root, name + ".json", report)
    manifest = {
        "schema_version": 1,
        "release": release,
        "previous_release": previous,
        "reports": reports,
    }
    reference(root, "manifest.json", manifest)
    approval = {
        "status": "approved",
        "approved_by": "synthetic-test-reviewer",
        "approved_at": NOW,
        "manifest_sha256": hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest(),
    }
    reference(root, "approval.json", approval)
    return manifest


def run(root):
    assert CLI.exists(), "missing offline staging evidence gate"
    result = subprocess.run(
        [
            sys.executable,
            str(CLI),
            "--manifest",
            str(root / "manifest.json"),
            "--approval",
            str(root / "approval.json"),
        ],
        capture_output=True,
        text=True,
    )
    return result.returncode, json.loads(result.stdout)


def rewrite(root, manifest):
    reference(root, "manifest.json", manifest)
    approval = json.loads((root / "approval.json").read_text())
    approval["manifest_sha256"] = hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest()
    reference(root, "approval.json", approval)


def test_complete_envelope_verifies_only_supplied_evidence(tmp_path):
    bundle(tmp_path)
    code, result = run(tmp_path)
    assert code == 0 and result["evidence_complete"] is True
    assert result["aws_actions_performed"] is False
    assert result["mode"] == "offline_evidence_verification"


@pytest.mark.parametrize("kind", list(ASSERTIONS))
def test_missing_required_report_remains_not_run(tmp_path, kind):
    manifest = bundle(tmp_path)
    del manifest["reports"][kind]
    rewrite(tmp_path, manifest)
    code, result = run(tmp_path)
    assert code == 1 and result["evidence_complete"] is False
    assert {"name": kind, "status": "not_run", "reason": "evidence not supplied"} in result[
        "checks"
    ]


@pytest.mark.parametrize(
    "field,value",
    [
        ("synthetic", True),
        ("synthetic", "false"),
        ("environment", "local"),
        ("release_sha256", "0" * 64),
        ("status", "not_run"),
        ("exit_code", True),
        ("exit_code", 1),
        ("checked_at", "2099-01-01T00:00:00Z"),
        ("assertions", {"foreign_read_denied": True}),
    ],
)
def test_unverified_report_cannot_pass(tmp_path, field, value):
    manifest = bundle(tmp_path)
    report = json.loads((tmp_path / "tenant_isolation.json").read_text())
    report[field] = value
    manifest["reports"]["tenant_isolation"] = reference(tmp_path, "tenant_isolation.json", report)
    rewrite(tmp_path, manifest)
    assert run(tmp_path)[0] == 1


def test_hash_tampering_and_approval_replay_fail(tmp_path):
    manifest = bundle(tmp_path)
    (tmp_path / "restore.log").write_bytes(b"tampered")
    assert run(tmp_path)[0] == 1
    bundle(tmp_path)
    manifest["release"]["tenant_id"] = "other-tenant"
    reference(tmp_path, "manifest.json", manifest)  # keep the old approval
    assert run(tmp_path)[0] == 1


@pytest.mark.parametrize(
    "change", ["mutable_image", "wrong_rollback", "path_escape", "slo_failure", "missing_approval"]
)
def test_release_and_recovery_guards(tmp_path, change):
    manifest = bundle(tmp_path)
    if change == "mutable_image":
        manifest["release"]["images"]["worker"] = "test.invalid/image:latest"
    elif change == "missing_approval":
        (tmp_path / "approval.json").unlink()
        assert run(tmp_path)[0] == 1
        return
    else:
        name = "slo" if change == "slo_failure" else "rollback"
        report = json.loads((tmp_path / (name + ".json")).read_text())
        if change == "wrong_rollback":
            report["restored_release"]["images"]["worker"] = "test.invalid/wrong@sha256:" + "d" * 64
        elif change == "path_escape":
            report["log"]["path"] = "../outside.log"
        else:
            report["measurements"]["query_p95_ms"] = 1001
        manifest["reports"][name] = reference(tmp_path, name + ".json", report)
    rewrite(tmp_path, manifest)
    assert run(tmp_path)[0] == 1


def test_restore_requires_original_and_restored_bytes_not_just_boolean_claims(tmp_path):
    manifest = bundle(tmp_path)
    report = json.loads((tmp_path / "restore.json").read_text())
    report["artifacts"] = {
        kind: {
            "original": reference(tmp_path, kind + "-before", b"original"),
            "restored": reference(tmp_path, kind + "-after", b"wrong restored content"),
        }
        for kind in ("source", "rulepack", "export")
    }
    manifest["reports"]["restore"] = reference(tmp_path, "restore.json", report)
    rewrite(tmp_path, manifest)
    assert run(tmp_path)[0] == 1


def test_unbounded_numeric_input_fails_closed_with_json_result(tmp_path):
    manifest = bundle(tmp_path)
    report = json.loads((tmp_path / "slo.json").read_text())
    report["measurements"]["query_p95_ms"] = 10**400
    manifest["reports"]["slo"] = reference(tmp_path, "slo.json", report)
    rewrite(tmp_path, manifest)
    assert run(tmp_path)[0] == 1


def test_oversized_evidence_is_rejected_before_reading_entire_file(tmp_path):
    manifest = bundle(tmp_path)
    report = json.loads((tmp_path / "tenant_isolation.json").read_text())
    report["padding"] = "x" * (1024 * 1024)
    manifest["reports"]["tenant_isolation"] = reference(tmp_path, "tenant_isolation.json", report)
    rewrite(tmp_path, manifest)
    assert run(tmp_path)[0] == 1
