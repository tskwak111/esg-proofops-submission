"""AT-042 SEC-006: 비밀·라이선스·공급망.

Acceptance: PyMuPDF 사용 승인이 없는 공개 배포에 해당 adapter가 포함되지 않는다.
Operation: POST /v1/preflight (license/supply gate part)
Files under test: scripts/check_licenses.py, infra/cdk/lib/iam.ts, .github/workflows/ci.yml
Interface: verify_supply_chain :: lockfiles + SBOM + license decisions -> deployment gate
"""

from __future__ import annotations

import json
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SUPPLY_MODULE = ROOT / "packages" / "proofops" / "application" / "supply_chain.py"
CHECK_SCRIPT = ROOT / "scripts" / "check_licenses.py"
IAM_FILE = ROOT / "infra" / "cdk" / "lib" / "iam.ts"
CI_FILE = ROOT / ".github" / "workflows" / "ci.yml"

# ---------------------------------------------------------------------------
# helpers for synthetic valid artifacts
# ---------------------------------------------------------------------------


def _valid_uv_lock_with_pymupdf() -> str:
    return """version = 1
[[package]]
name = "pymupdf"
version = "1.26.0"
source = { registry = "https://pypi.org/simple" }
[[package]]
name = "fastapi"
version = "0.119.1"
source = { registry = "https://pypi.org/simple" }
"""


def _valid_uv_lock_without_pymupdf() -> str:
    return """version = 1
[[package]]
name = "fastapi"
version = "0.119.1"
source = { registry = "https://pypi.org/simple" }
"""


def _valid_pnpm_lock() -> str:
    return """lockfileVersion: '9.0'
settings:
  autoInstallPeers: true
importers:
  .:
    dependencies:
      react:
        specifier: ^19.0.0
        version: 19.2.8
packages:
  react@19.2.8:
    resolution: {integrity: sha512-xxx}
"""


def _valid_sbom_for(names: list[str]) -> str:
    versions = {"fastapi": "0.119.1", "pymupdf": "1.26.0", "react": "19.2.8"}
    comps = [
        {
            "type": "library",
            "name": name,
            "version": versions[name],
            "purl": f"pkg:{'npm' if name == 'react' else 'pypi'}/{name}@{versions[name]}",
        }
        for name in [*names, "react"]
    ]
    return json.dumps(
        {
            "bomFormat": "CycloneDX",
            "specVersion": "1.6",
            "serialNumber": "urn:uuid:00000000-0000-4000-8000-000000000002",
            "version": 1,
            "metadata": {
                "component": {"type": "application", "name": "proofops", "version": "0.0.0"},
                "properties": [
                    {
                        "name": "proofops:uv.lock:sha256",
                        "value": sha256(
                            (
                                _valid_uv_lock_with_pymupdf()
                                if "pymupdf" in names
                                else _valid_uv_lock_without_pymupdf()
                            ).encode()
                        ).hexdigest(),
                    },
                    {
                        "name": "proofops:pnpm-lock.yaml:sha256",
                        "value": sha256(_valid_pnpm_lock().encode()).hexdigest(),
                    },
                ],
            },
            "components": comps,
        }
    )


def _approved_license_yaml() -> str:
    return """decisions:
  - name: pymupdf
    decision: approved
    license: commercial
    approved_by: legal-approver
    approved_at: '2026-09-08'
    source: https://pymupdf.readthedocs.io/en/latest/about.html
"""


# ---------------------------------------------------------------------------
# 1. Module / script existence and importability
# ---------------------------------------------------------------------------


def test_supply_chain_module_exists():
    assert SUPPLY_MODULE.is_file(), "packages/proofops/application/supply_chain.py must exist"


def test_verify_supply_chain_importable():
    from proofops.application.supply_chain import verify_supply_chain

    assert callable(verify_supply_chain)


def test_check_licenses_script_exists_and_executable():
    assert CHECK_SCRIPT.is_file(), "scripts/check_licenses.py must exist"
    result = subprocess.run(
        [sys.executable, str(CHECK_SCRIPT), "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_iam_file_exists_and_least_privilege():
    assert IAM_FILE.is_file(), "infra/cdk/lib/iam.ts must exist"
    text = IAM_FILE.read_text(encoding="utf-8")
    assert "OIDC" in text or "oidc" in text.lower(), "IAM must use GitHub OIDC"
    assert "secretsmanager" in text.lower() or "kms" in text.lower() or "deploy" in text.lower()
    assert "assertNoWildcard" in text  # Executable policy checks below cover the ECR exception.
    lowered = text.lower()
    assert "iam:*" not in lowered, "IAM must not contain iam:* wildcard"
    assert "s3:*" not in lowered, "IAM must not contain s3:* wildcard"
    # Verify explicit model/profile ARNs, not wildcard foundation-model/*
    # Our IAM should reference explicit ARNs passed via props, not wildcard
    assert "foundation-model/*" not in text, "IAM must not grant wildcard foundation-model/*"
    assert "inference-profile/*" not in text, "IAM must not grant wildcard inference-profile/*"
    assert "sts:AssumeRoleWithWebIdentity" in text or "AssumeRoleWithWebIdentity" in text


def test_ci_workflow_supply_chain_gate():
    assert CI_FILE.is_file(), ".github/workflows/ci.yml must exist"
    text = CI_FILE.read_text(encoding="utf-8")
    assert (
        "supply-chain" in text.lower() or "supply_chain" in text.lower()
    ), "CI must have supply-chain job"
    assert "uv.lock" in text or "pnpm-lock" in text or "lock" in text.lower()
    assert "sbom" in text.lower(), "CI must generate/check SBOM"
    assert "secret" in text.lower(), "CI must run secret scan"
    assert "license" in text.lower(), "CI must run license check"
    import re

    pinned = re.findall(r"uses:\s+[^\n]+@[0-9a-f]{40}", text)
    assert len(pinned) >= 4, f"Actions must be pinned to full SHA, found {pinned}"
    assert "aws-actions/configure-aws-credentials" not in text.lower()
    # Verification CI must NOT use live deploy credentials or global id-token
    # Verification must not assume deployment roles.
    assert "123456789012" not in text, "CI must not contain placeholder AWS account"
    # Check no fallback that hides failures
    assert "|| true" not in text, "CI must not use || true to hide failures"
    lowered = text.lower()
    assert "aws_access_key_id" not in lowered, "CI must not use long-lived AWS keys"
    assert "aws_secret_access_key" not in lowered
    # Must not have global permissions id-token write for verification
    # If it has, ensure it's job-scoped not global
    # Simple check: if global permissions exists, it should not be id-token: write at top level
    assert (
        text.count("id-token: write") <= 1
    ), "Global id-token permission should not be present for verification CI"


# ---------------------------------------------------------------------------
# 2. verify_supply_chain core logic
# ---------------------------------------------------------------------------


def test_verify_supply_chain_lockfiles_missing_blocks(tmp_path: Path):
    from proofops.application.supply_chain import verify_supply_chain

    result = verify_supply_chain(
        root_dir=tmp_path,
        env={"ENABLE_LEGACY_PYMUPDF": "false", "APP_ENV": "production"},
        check_secrets=False,
    )
    assert result.passed is False
    assert any("lock" in e.lower() for e in result.errors)


def test_verify_supply_chain_lockfiles_present_passes():
    from proofops.application.supply_chain import verify_supply_chain

    result = verify_supply_chain(
        root_dir=ROOT,
        env={"ENABLE_LEGACY_PYMUPDF": "false", "APP_ENV": "production"},
        check_secrets=False,
    )
    assert not any("uv.lock" in e and "missing" in e.lower() for e in result.errors)
    assert not any("pnpm-lock" in e and "missing" in e.lower() for e in result.errors)


def test_malformed_uv_lock_blocks(tmp_path: Path):
    from proofops.application.supply_chain import verify_supply_chain

    (tmp_path / "uv.lock").write_text("truncated", encoding="utf-8")
    (tmp_path / "pnpm-lock.yaml").write_text(_valid_pnpm_lock(), encoding="utf-8")
    (tmp_path / "sbom.json").write_text(_valid_sbom_for(["fastapi"]), encoding="utf-8")
    result = verify_supply_chain(
        root_dir=tmp_path, env={"ENABLE_LEGACY_PYMUPDF": "false"}, check_secrets=False
    )
    assert result.passed is False
    assert any("uv.lock" in e.lower() and "invalid" in e.lower() for e in result.errors)


def test_malformed_pnpm_lock_blocks(tmp_path: Path):
    from proofops.application.supply_chain import verify_supply_chain

    (tmp_path / "uv.lock").write_text(_valid_uv_lock_without_pymupdf(), encoding="utf-8")
    (tmp_path / "pnpm-lock.yaml").write_text("not: yaml: : :", encoding="utf-8")
    (tmp_path / "sbom.json").write_text(_valid_sbom_for(["fastapi"]), encoding="utf-8")
    result = verify_supply_chain(
        root_dir=tmp_path, env={"ENABLE_LEGACY_PYMUPDF": "false"}, check_secrets=False
    )
    assert result.passed is False
    assert any("pnpm-lock" in e.lower() for e in result.errors)


def test_pymupdf_gate_blocks_without_approval(tmp_path: Path):
    """PyMuPDF adapter present + ENABLE_LEGACY_PYMUPDF=false => deployment blocked."""
    from proofops.application.supply_chain import verify_supply_chain

    fake_lock = tmp_path / "uv.lock"
    fake_lock.write_text(_valid_uv_lock_with_pymupdf(), encoding="utf-8")
    (tmp_path / "pnpm-lock.yaml").write_text(_valid_pnpm_lock(), encoding="utf-8")
    adapter_dir = tmp_path / "packages" / "proofops" / "adapters" / "parsing"
    adapter_dir.mkdir(parents=True)
    (adapter_dir / "legacy_pymupdf.py").write_text(
        "# legacy pymupdf adapter\nimport fitz\n", encoding="utf-8"
    )
    (tmp_path / "sbom.json").write_text(_valid_sbom_for(["pymupdf", "fastapi"]), encoding="utf-8")

    result = verify_supply_chain(
        root_dir=tmp_path,
        env={"ENABLE_LEGACY_PYMUPDF": "false", "APP_ENV": "production"},
        check_secrets=False,
    )
    assert result.passed is False
    combined = " ".join(result.errors).lower()
    assert "pymupdf" in combined or "legacy_pymupdf" in combined or "license" in combined


def test_pymupdf_gate_blocks_without_verified_approval_even_when_enabled(tmp_path: Path):
    """ENABLE true alone without verified approval metadata must still FAIL (fail-closed)."""
    from proofops.application.supply_chain import verify_supply_chain

    (tmp_path / "uv.lock").write_text(_valid_uv_lock_with_pymupdf(), encoding="utf-8")
    (tmp_path / "pnpm-lock.yaml").write_text(_valid_pnpm_lock(), encoding="utf-8")
    (tmp_path / "sbom.json").write_text(_valid_sbom_for(["pymupdf", "fastapi"]), encoding="utf-8")
    # Provide incomplete approval (missing approved_by)
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "license_decisions.yaml").write_text(
        "decisions:\n  - name: pymupdf\n    decision: approved\n    license: commercial\n",
        encoding="utf-8",
    )
    result = verify_supply_chain(
        root_dir=tmp_path,
        env={"ENABLE_LEGACY_PYMUPDF": "true", "APP_ENV": "production"},
        check_secrets=False,
    )
    assert result.passed is False
    combined = " ".join(result.errors).lower()
    assert "pymupdf" in combined or "approval" in combined


def test_enable_flag_without_pymupdf_but_no_approval_fails(tmp_path: Path):
    """ENABLE true without any pymupdf usage but still no verified approval must FAIL."""
    from proofops.application.supply_chain import verify_supply_chain

    (tmp_path / "uv.lock").write_text(_valid_uv_lock_without_pymupdf(), encoding="utf-8")
    (tmp_path / "pnpm-lock.yaml").write_text(_valid_pnpm_lock(), encoding="utf-8")
    (tmp_path / "sbom.json").write_text(_valid_sbom_for(["fastapi"]), encoding="utf-8")
    # No config file
    result = verify_supply_chain(
        root_dir=tmp_path,
        env={"ENABLE_LEGACY_PYMUPDF": "true", "APP_ENV": "production"},
        check_secrets=False,
    )
    assert result.passed is False
    assert any("approval" in e.lower() or "pymupdf" in e.lower() for e in result.errors)


def test_pymupdf_gate_allows_with_verified_approval(tmp_path: Path):
    """With verified approval and ENABLE true, gate allows pymupdf."""
    from proofops.application.supply_chain import verify_supply_chain

    fake_lock = tmp_path / "uv.lock"
    fake_lock.write_text(_valid_uv_lock_with_pymupdf(), encoding="utf-8")
    (tmp_path / "pnpm-lock.yaml").write_text(_valid_pnpm_lock(), encoding="utf-8")
    adapter_dir = tmp_path / "packages" / "proofops" / "adapters" / "parsing"
    adapter_dir.mkdir(parents=True)
    (adapter_dir / "legacy_pymupdf.py").write_text(
        "# legacy pymupdf adapter\nimport fitz\n", encoding="utf-8"
    )
    (tmp_path / "sbom.json").write_text(_valid_sbom_for(["pymupdf", "fastapi"]), encoding="utf-8")
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "license_decisions.yaml").write_text(
        _approved_license_yaml(), encoding="utf-8"
    )

    result = verify_supply_chain(
        root_dir=tmp_path,
        env={"ENABLE_LEGACY_PYMUPDF": "true", "APP_ENV": "production"},
        check_secrets=False,
    )
    combined = " ".join(result.errors).lower()
    assert (
        "pymupdf" not in combined and "legacy_pymupdf" not in combined
    ), f"Should allow with verified approval, got {result.errors}"
    assert result.passed is True, f"Should pass with verified approval, errors: {result.errors}"


def test_pymupdf_import_scan_blocks_without_flag(tmp_path: Path):
    """Even if adapter file not named legacy_pymupdf, import of fitz/pymupdf should be detected."""
    from proofops.application.supply_chain import verify_supply_chain

    (tmp_path / "uv.lock").write_text(_valid_uv_lock_without_pymupdf(), encoding="utf-8")
    (tmp_path / "pnpm-lock.yaml").write_text(_valid_pnpm_lock(), encoding="utf-8")
    (tmp_path / "sbom.json").write_text(_valid_sbom_for(["fastapi"]), encoding="utf-8")
    some_file = tmp_path / "packages" / "proofops" / "adapters" / "parsing" / "some_parser.py"
    some_file.parent.mkdir(parents=True)
    some_file.write_text("import pymupdf\n", encoding="utf-8")

    result = verify_supply_chain(
        root_dir=tmp_path,
        env={"ENABLE_LEGACY_PYMUPDF": "false", "APP_ENV": "production"},
        check_secrets=False,
    )
    assert result.passed is False
    assert any("pymupdf" in e.lower() or "fitz" in e.lower() for e in result.errors)


def test_secret_scan_detects_fake_secret(tmp_path: Path):
    from proofops.application.supply_chain import verify_supply_chain

    (tmp_path / "uv.lock").write_text(_valid_uv_lock_without_pymupdf(), encoding="utf-8")
    (tmp_path / "pnpm-lock.yaml").write_text(_valid_pnpm_lock(), encoding="utf-8")
    (tmp_path / "sbom.json").write_text(_valid_sbom_for(["fastapi"]), encoding="utf-8")
    secret_file = tmp_path / "some.py"
    secret_file.write_text('aws_secret = "' + "AKIA" + "Z" * 16 + '"\n', encoding="utf-8")

    result = verify_supply_chain(
        root_dir=tmp_path,
        env={"ENABLE_LEGACY_PYMUPDF": "false", "APP_ENV": "production"},
        check_secrets=True,
    )
    assert result.passed is False
    assert any("secret" in e.lower() for e in result.errors)


def test_sbom_missing_blocks_when_required(tmp_path: Path):
    from proofops.application.supply_chain import verify_supply_chain

    (tmp_path / "uv.lock").write_text(_valid_uv_lock_without_pymupdf(), encoding="utf-8")
    (tmp_path / "pnpm-lock.yaml").write_text(_valid_pnpm_lock(), encoding="utf-8")
    result = verify_supply_chain(
        root_dir=tmp_path,
        env={"ENABLE_LEGACY_PYMUPDF": "false", "APP_ENV": "production"},
        check_secrets=False,
    )
    assert result.passed is False
    assert any("sbom" in e.lower() for e in result.errors)


def test_empty_sbom_blocks(tmp_path: Path):
    from proofops.application.supply_chain import verify_supply_chain

    (tmp_path / "uv.lock").write_text(_valid_uv_lock_without_pymupdf(), encoding="utf-8")
    (tmp_path / "pnpm-lock.yaml").write_text(_valid_pnpm_lock(), encoding="utf-8")
    (tmp_path / "sbom.json").write_text(
        '{"bomFormat":"CycloneDX","specVersion":"1.6","components":[]}', encoding="utf-8"
    )
    result = verify_supply_chain(
        root_dir=tmp_path, env={"ENABLE_LEGACY_PYMUPDF": "false"}, check_secrets=False
    )
    assert result.passed is False
    assert any("sbom" in e.lower() and "empty" in e.lower() for e in result.errors)


def test_sbom_inventory_gap_blocks(tmp_path: Path):
    from proofops.application.supply_chain import verify_supply_chain

    (tmp_path / "uv.lock").write_text(_valid_uv_lock_with_pymupdf(), encoding="utf-8")
    (tmp_path / "pnpm-lock.yaml").write_text(_valid_pnpm_lock(), encoding="utf-8")
    # SBOM only has fastapi, missing pymupdf
    (tmp_path / "sbom.json").write_text(_valid_sbom_for(["fastapi"]), encoding="utf-8")
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "license_decisions.yaml").write_text(
        _approved_license_yaml(), encoding="utf-8"
    )
    result = verify_supply_chain(
        root_dir=tmp_path, env={"ENABLE_LEGACY_PYMUPDF": "true"}, check_secrets=False
    )
    assert result.passed is False
    assert any("inventory gap" in e.lower() or "sbom" in e.lower() for e in result.errors)


def test_real_repo_supply_chain_reports_license_gate():
    """Real repo without ENABLE_LEGACY_PYMUPDF must not include pymupdf in lockfiles nor adapter."""
    from proofops.application.supply_chain import verify_supply_chain

    result = verify_supply_chain(
        root_dir=ROOT,
        env={"ENABLE_LEGACY_PYMUPDF": "false", "APP_ENV": "production"},
        check_secrets=False,
    )
    combined = " ".join(result.errors).lower()
    assert "pymupdf" not in combined, f"Real repo should not be blocked on pymupdf: {result.errors}"
    legacy = ROOT / "packages" / "proofops" / "adapters" / "parsing" / "legacy_pymupdf.py"
    assert not legacy.exists(), "legacy_pymupdf.py must not be shipped without license approval"


def test_check_licenses_cli_blocks_without_approval(tmp_path: Path):
    """scripts/check_licenses.py must exit non-zero when gate blocked."""
    (tmp_path / "uv.lock").write_text(_valid_uv_lock_with_pymupdf(), encoding="utf-8")
    (tmp_path / "pnpm-lock.yaml").write_text(_valid_pnpm_lock(), encoding="utf-8")
    (tmp_path / "sbom.json").write_text(_valid_sbom_for(["pymupdf", "fastapi"]), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(CHECK_SCRIPT),
            "--root",
            str(tmp_path),
            "--env",
            "ENABLE_LEGACY_PYMUPDF=false",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0, f"Should block, stdout={result.stdout} stderr={result.stderr}"
    assert "pymupdf" in (result.stdout + result.stderr).lower()


def test_check_licenses_cli_allows_with_verified_approval(tmp_path: Path):
    (tmp_path / "uv.lock").write_text(_valid_uv_lock_with_pymupdf(), encoding="utf-8")
    (tmp_path / "pnpm-lock.yaml").write_text(_valid_pnpm_lock(), encoding="utf-8")
    (tmp_path / "sbom.json").write_text(_valid_sbom_for(["pymupdf", "fastapi"]), encoding="utf-8")
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "license_decisions.yaml").write_text(_approved_license_yaml(), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(CHECK_SCRIPT),
            "--root",
            str(tmp_path),
            "--env",
            "ENABLE_LEGACY_PYMUPDF=true",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert (
        result.returncode == 0
    ), f"With verified approval should pass: stdout={result.stdout} stderr={result.stderr}"


@pytest.fixture
def clean_inventory(tmp_path):
    """Synthetic dependency inventory only; never a product/legal approval."""
    (tmp_path / "uv.lock").write_text(_valid_uv_lock_without_pymupdf())
    (tmp_path / "pnpm-lock.yaml").write_text(_valid_pnpm_lock())
    (tmp_path / "sbom.json").write_text(_valid_sbom_for(["fastapi"]))
    return tmp_path


@pytest.mark.parametrize(
    "contents", ["[]", "null", '{"bomFormat":"CycloneDX","components":[null]}']
)
def test_sbom_invalid_shape_blocks_without_crashing(clean_inventory, contents):
    from proofops.application.supply_chain import verify_supply_chain

    (clean_inventory / "sbom.json").write_text(contents)
    result = verify_supply_chain(root_dir=clean_inventory, check_secrets=False)
    assert not result.passed
    assert any("sbom" in error.lower() for error in result.errors)


def test_toml_syntax_error_with_package_header_blocks(clean_inventory):
    from proofops.application.supply_chain import verify_supply_chain

    with (clean_inventory / "uv.lock").open("a") as stream:
        stream.write("\nbroken = [\n")
    result = verify_supply_chain(root_dir=clean_inventory, check_secrets=False)
    assert any("lockfile invalid: uv.lock" in error for error in result.errors)


@pytest.mark.parametrize("change", ["version", "npm"])
def test_sbom_requires_exact_python_and_node_inventory(clean_inventory, change):
    from proofops.application.supply_chain import verify_supply_chain

    bom = json.loads((clean_inventory / "sbom.json").read_text())
    if change == "version":
        bom["components"][0]["version"] = "0.0.0"
        bom["components"][0]["purl"] = "pkg:pypi/fastapi@0.0.0"
    else:
        bom["components"] = [c for c in bom["components"] if c["name"] != "react"]
    (clean_inventory / "sbom.json").write_text(json.dumps(bom))
    result = verify_supply_chain(root_dir=clean_inventory, check_secrets=False)
    assert not result.passed
    assert any("inventory gap" in error for error in result.errors)


@pytest.mark.parametrize(
    "statement", ["import os, fitz", 'import importlib\npdf = importlib.import_module("pymupdf")']
)
def test_alternative_import_forms_require_approval(clean_inventory, statement):
    from proofops.application.supply_chain import verify_supply_chain

    (clean_inventory / "parser.py").write_text(statement)
    result = verify_supply_chain(root_dir=clean_inventory, check_secrets=False)
    assert any("license gate" in error for error in result.errors)


@pytest.mark.parametrize("removed", ["approved_at", "source"])
def test_incomplete_approval_record_blocks(clean_inventory, removed):
    from proofops.application.supply_chain import verify_supply_chain

    text = "\n".join(
        line for line in _approved_license_yaml().splitlines() if removed + ":" not in line
    )
    (clean_inventory / "config").mkdir()
    (clean_inventory / "config/license_decisions.yaml").write_text(text)
    result = verify_supply_chain(
        root_dir=clean_inventory, env={"ENABLE_LEGACY_PYMUPDF": "true"}, check_secrets=False
    )
    assert any("license gate" in error for error in result.errors)


def test_secret_scan_includes_test_directories_and_dotenv(clean_inventory):
    from proofops.application.supply_chain import verify_supply_chain

    directory = clean_inventory / "tests"
    directory.mkdir()
    secret = "AKIA" + "Z" * 16
    (directory / ".env").write_text("AWS_ACCESS_KEY_ID=" + secret)
    result = verify_supply_chain(root_dir=clean_inventory)
    assert any("secret scan" in error for error in result.errors)
    assert secret not in repr(result)


def test_ci_has_no_fail_open_typecheck_or_unlocked_tool_install():
    text = CI_FILE.read_text()
    assert "||" not in text
    assert "uv run pip audit" not in text
    assert "pip install cyclonedx-bom pip-audit pyyaml" not in text


def test_iam_builders_execute_and_reject_unbounded_inputs(tmp_path):
    compiled = subprocess.run(
        [
            "pnpm",
            "--dir",
            str(ROOT / "apps/web"),
            "exec",
            "tsc",
            str(IAM_FILE),
            "--outDir",
            str(tmp_path),
            "--target",
            "ES2022",
            "--module",
            "commonjs",
            "--skipLibCheck",
            "--strict",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    program = r"""
const assert = require('node:assert/strict');
const iam = require(process.argv[1]);
const props = {
 githubRepository: 'synthetic/repo',
 githubOidcProviderArn:
   'arn:aws:iam::111111111111:oidc-provider/token.actions.githubusercontent.com',
 artifactBucketArn: 'arn:aws:s3:::synthetic-artifacts',
 quarantineBucketArn: 'arn:aws:s3:::synthetic-quarantine',
 coreTableArn: 'arn:aws:dynamodb:ap-northeast-2:111111111111:table/synthetic-core',
 auditTableArn: 'arn:aws:dynamodb:ap-northeast-2:111111111111:table/synthetic-audit',
 kmsKeyArn: 'arn:aws:kms:ap-northeast-2:111111111111:key/synthetic-key',
 secretsArns: [], approvedModelArns: [], approvedInferenceProfileArns: [],
 deploymentStackArns: ['arn:aws:cloudformation:ap-northeast-2:111111111111:stack/synthetic/id'],
 ecrRepositoryArns: ['arn:aws:ecr:ap-northeast-2:111111111111:repository/synthetic'],
};
const deployment = JSON.stringify(iam.buildDeployRolePolicies(props));
assert(!deployment.includes('123456789012'), 'no invented AWS account');
assert(deployment.includes(props.deploymentStackArns[0]));
const withModel = {...props,
 approvedModelArns: ['arn:aws:bedrock:ap-northeast-2::foundation-model/synthetic-model']};
assert(!JSON.stringify(iam.buildApiTaskPolicies(withModel)).includes('bedrock:InvokeModel'));
const worker = iam.buildWorkerTaskPolicies(withModel);
assert.deepEqual(worker.find(s => s.Action.includes('bedrock:InvokeModel')).Resource,
 withModel.approvedModelArns);
assert(!JSON.stringify(worker).includes('secretsmanager:GetSecretValue'));
assert.deepEqual(iam.buildDeployRolePolicies(props).filter(s => s.Resource.includes('*')),
 [{Effect:'Allow', Action:['ecr:GetAuthorizationToken'], Resource:['*']}]);
assert.throws(() => iam.assertNoWildcard([
 {Effect:'Allow', Action:['ecr:GetAuthorizationToken','ecr:PutImage'], Resource:['*']} ]));
const trust = JSON.stringify(iam.buildGithubDeployTrustPolicy(props));
assert(!trust.includes('refs/heads/main'),
 'protected environment must not be bypassed by branch trust');
assert.throws(() => iam.buildApiTaskPolicies({...props, approvedModelArns: ['*']}));
assert.throws(() => iam.buildGithubDeployTrustPolicy({...props, githubRepository: '*/*'}));
assert.throws(() => iam.assertNoWildcard([{Effect: 'Allow', Action: ['*'], Resource: ['*']}]));
"""
    executed = subprocess.run(
        ["node", "-e", program, str(tmp_path / "iam.js")],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert executed.returncode == 0, executed.stdout + executed.stderr


def test_cli_generates_exact_lock_inventory(clean_inventory):
    generated = subprocess.run(
        [
            sys.executable,
            str(CHECK_SCRIPT),
            "--root",
            str(clean_inventory),
            "--generate-sbom",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert generated.returncode == 0, generated.stdout + generated.stderr
    data = json.loads((clean_inventory / "sbom.json").read_text())
    assert {(c["name"], c["version"]) for c in data["components"]} == {
        ("fastapi", "0.119.1"),
        ("react", "19.2.8"),
    }
    assert not any("licenses" in c for c in data["components"])
    assert "not a license approval" in data["metadata"]["component"]["description"]


@pytest.mark.parametrize("directory", ["packages/legacy_reference", "packages/tests"])
def test_runtime_directories_cannot_hide_imports(clean_inventory, directory):
    from proofops.application.supply_chain import verify_supply_chain

    path = clean_inventory / directory
    path.mkdir(parents=True)
    (path / "hidden.py").write_text("import fitz\n")
    result = verify_supply_chain(root_dir=clean_inventory, check_secrets=False)
    assert any("license gate" in error for error in result.errors)


def test_symlink_directory_blocks_uninspected_sources(clean_inventory, tmp_path):
    from proofops.application.supply_chain import verify_supply_chain

    external = tmp_path / "outside"
    external.mkdir()
    (clean_inventory / "linked").symlink_to(external, target_is_directory=True)
    result = verify_supply_chain(root_dir=clean_inventory, check_secrets=False)
    assert not result.passed
    assert any("symlink" in error for error in result.errors)


def test_invalid_license_kind_blocks(clean_inventory):
    from proofops.application.supply_chain import verify_supply_chain

    (clean_inventory / "config").mkdir()
    (clean_inventory / "config/license_decisions.yaml").write_text(
        _approved_license_yaml().replace("license: commercial", "license: MIT")
    )
    result = verify_supply_chain(
        root_dir=clean_inventory, env={"ENABLE_LEGACY_PYMUPDF": "true"}, check_secrets=False
    )
    assert any("license gate" in error for error in result.errors)


def test_lock_resolution_change_invalidates_generated_sbom(clean_inventory):
    from proofops.application.supply_chain import generate_sbom, verify_supply_chain

    (clean_inventory / "sbom.json").write_text(json.dumps(generate_sbom(clean_inventory)))
    with (clean_inventory / "uv.lock").open("a") as stream:
        stream.write("\n# lock source/hash revision changed\n")
    result = verify_supply_chain(root_dir=clean_inventory, check_secrets=False)
    assert not result.passed
    assert any("lock hash" in error for error in result.errors)


@pytest.mark.parametrize("artifact", ["uv.lock", "sbom.json"])
def test_boolean_version_is_not_a_format_version(clean_inventory, artifact):
    from proofops.application.supply_chain import verify_supply_chain

    path = clean_inventory / artifact
    if artifact == "uv.lock":
        path.write_text(path.read_text().replace("version = 1", "version = true", 1))
    else:
        data = json.loads(path.read_text())
        data["version"] = True
        path.write_text(json.dumps(data))
    result = verify_supply_chain(root_dir=clean_inventory, check_secrets=False)
    assert any("invalid" in error.lower() for error in result.errors)
