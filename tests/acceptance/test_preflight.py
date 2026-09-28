"""AT-029: real preflight/dispatch guards; account evidence and SDK are synthetic.

No AWS/model call or human approval is performed by these fixtures. The
in-process transport records actual dispatch attempts instead of mocking guards.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from proofops.adapters.aws.bedrock import BedrockInvoker
from proofops.application.authorization import AuthContext, TenantNotFoundError
from proofops.application.preflight import PreflightBlocked, check_runtime_binding

ROOT = Path(__file__).resolve().parents[2]
NOW = "2026-09-09T00:00:00Z"
AUTH = AuthContext(
    "synthetic-admin",
    "11111111-1111-4111-8111-111111111111",
    "admin",
    frozenset({"admin"}),
    "session-a",
)
REGIONS = ("ap-northeast-2",)


def binding() -> dict[str, Any]:
    return {
        "runtime_binding_id": "00000000-0000-4000-8000-000000000001",
        "tenant_id": "11111111-1111-4111-8111-111111111111",
        "account_id": "000000000000",
        "status": "approved",
        "version": "1",
        "approved_by": "synthetic-reviewer",
        "approved_at": NOW,
        "checked_at": NOW,
        "role": "tagger",
        "model_id": "synthetic.model-v1",
        "inference_profile_arn": None,
        "endpoint_region": "ap-northeast-2",
        "allowed_processing_regions": ["ap-northeast-2"],
        "processing_regions_verified": True,
        "permissions_verified": True,
        "structured_output_strategy": "json_schema",
        "structured_output_verified": True,
        "accepts_images": True,
        "image_input_verified": True,
        "max_context_tokens": 10000,
        "max_output_tokens": 4096,
        "pricing_snapshot_id": "synthetic-price",
        "fallback_bindings": [],
    }


def consent() -> dict[str, Any]:
    return {
        "consent_profile_id": "00000000-0000-4000-8000-000000000002",
        "tenant_id": "11111111-1111-4111-8111-111111111111",
        "status": "approved",
        "version": "1",
        "approved_by": "synthetic-reviewer",
        "approved_at": NOW,
        "allowed_document_rights": ["synthetic-public"],
        "allowed_processing_regions": ["ap-northeast-2"],
        "provider_terms_approved": True,
        "allow_live_model_tests": False,
        "allow_cross_tenant_cache": False,
        "allow_agentcore_memory": False,
    }


def check(runtime=None, profile=None, **kwargs):
    return check_runtime_binding(
        binding=binding() if runtime is None else runtime,
        consent=consent() if profile is None else profile,
        auth=AUTH,
        allowed_regions=REGIONS,
        checked_at=NOW,
        **kwargs,
    )


class SyntheticBedrockClient:
    """Explicit local synthetic transport; never resolves or calls AWS."""

    def __init__(self, region="ap-northeast-2", *, fail=False):
        self.meta = SimpleNamespace(
            region_name=region, endpoint_url=f"https://bedrock-runtime.{region}.amazonaws.com"
        )
        self.calls: list[dict[str, Any]] = []
        self.fail = fail

    def invoke_model(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("synthetic provider outage; sensitive body must not escape")
        return {"synthetic": True, "body": b'{"tags":[]}'}


def dispatch(client, runtime=None, profile=None, **kwargs):
    adapter = BedrockInvoker(client, account_id="000000000000")
    return adapter.invoke(
        body=b'{"synthetic_document":"test only"}',
        auth=AUTH,
        binding=binding() if runtime is None else runtime,
        consent=consent() if profile is None else profile,
        allowed_regions=REGIONS,
        document_rights="synthetic-public",
        checked_at=NOW,
        **kwargs,
    )


def test_approved_snapshot_and_contract():
    result = check()
    assert result.ready
    schema = json.loads((ROOT / "contracts/jsonschema/api_models.schema.json").read_text())
    Draft202012Validator(schema["$defs"]["Preflight"], format_checker=FormatChecker()).validate(
        result.to_dict()
    )
    assert any(c.status == "not_run" and c.name == "live_model_probe" for c in result.checks)
    assert "synthetic.model" not in json.dumps(result.to_dict())


@pytest.mark.parametrize(
    "field,value",
    [
        ("model_id", ""),
        ("status", "requires_account_preflight"),
        ("permissions_verified", False),
        ("permissions_verified", "true"),
        ("processing_regions_verified", False),
        ("allowed_processing_regions", []),
        ("allowed_processing_regions", ["*"]),
        ("allowed_processing_regions", ["us-east-1"]),
        ("endpoint_region", "us-east-1"),
        ("structured_output_verified", False),
        ("structured_output_strategy", ""),
        ("max_context_tokens", 0),
        ("max_output_tokens", 10001),
        ("max_output_tokens", True),
        ("checked_at", ""),
        ("checked_at", "2027-01-01T00:00:00Z"),
        ("approved_by", ""),
        ("version", ""),
        ("account_id", ""),
        ("fallback_bindings", [{"endpoint_region": "us-east-1"}]),
    ],
)
def test_invalid_binding_never_transmits(field, value):
    runtime = binding()
    runtime[field] = value
    client = SyntheticBedrockClient()
    assert not check(runtime).ready
    with pytest.raises(PreflightBlocked):
        dispatch(client, runtime)
    assert client.calls == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "requires_approval"),
        ("allowed_processing_regions", []),
        ("allowed_processing_regions", ["*"]),
        ("allowed_document_rights", []),
        ("provider_terms_approved", False),
        ("provider_terms_approved", "true"),
        ("allow_cross_tenant_cache", True),
        ("allow_agentcore_memory", True),
    ],
)
def test_unapproved_consent_never_transmits(field, value):
    profile = consent()
    profile[field] = value
    client = SyntheticBedrockClient()
    with pytest.raises(PreflightBlocked):
        dispatch(client, profile=profile)
    assert client.calls == []


def test_profile_all_destinations_checked_not_just_endpoint():
    runtime = binding()
    runtime["inference_profile_arn"] = "synthetic-profile"
    runtime["allowed_processing_regions"].append("us-east-1")
    assert not check(runtime).ready


def test_vision_capability_and_no_live_probe_fabrication():
    runtime = binding()
    runtime["role"] = "vision"
    runtime["image_input_verified"] = False
    assert not check(runtime).ready
    assert not check(include_live_model_probe=True).ready


@pytest.mark.parametrize("which", ["binding", "consent"])
def test_cross_tenant_hides_binding_and_never_transmits(which):
    runtime, profile = binding(), consent()
    (runtime if which == "binding" else profile)["tenant_id"] = (
        "22222222-2222-4222-8222-222222222222"
    )
    client = SyntheticBedrockClient()
    with pytest.raises(TenantNotFoundError):
        dispatch(client, runtime, profile)
    assert client.calls == []


def test_sdk_endpoint_and_account_are_checked_before_transmission():
    client = SyntheticBedrockClient("us-east-1")
    with pytest.raises(PreflightBlocked):
        dispatch(client)
    assert not client.calls
    client = SyntheticBedrockClient()
    client.meta.endpoint_url = "https://unapproved.example"
    with pytest.raises(PreflightBlocked):
        dispatch(client)
    assert not client.calls
    runtime = binding()
    runtime["account_id"] = "111111111111"
    with pytest.raises(PreflightBlocked):
        dispatch(client, runtime)
    assert not client.calls


def test_dispatch_uses_only_exact_approved_target_and_no_failure_fallback():
    client = SyntheticBedrockClient()
    assert dispatch(client)["synthetic"] is True
    assert len(client.calls) == 1
    assert client.calls[0]["modelId"] == binding()["model_id"]
    assert client.calls[0]["contentType"] == "application/json"
    failed = SyntheticBedrockClient(fail=True)
    with pytest.raises(PreflightBlocked, match="MODEL_UNAVAILABLE"):
        dispatch(failed)
    assert len(failed.calls) == 1


def test_hash_frozen_input_not_overwritten_and_rechecked_after_consent_change():
    runtime, profile = binding(), consent()
    original = copy.deepcopy((runtime, profile))
    result = check(runtime, profile)
    assert (runtime, profile) == original
    assert check(runtime, profile).binding_sha256 == result.binding_sha256
    runtime["version"] = "2"
    assert check(runtime, profile).binding_sha256 != result.binding_sha256
    profile["allowed_processing_regions"] = ["us-east-1"]
    client = SyntheticBedrockClient()
    with pytest.raises(PreflightBlocked):
        dispatch(client, runtime, profile)
    assert not client.calls
    assert result.ready


def synthetic_build(root):
    """Build real lock/SBOM verification evidence for an explicit synthetic repo."""
    from proofops.application.supply_chain import generate_sbom, verify_supply_chain

    root.mkdir(exist_ok=True)
    (root / "uv.lock").write_text(
        'version = 1\n[[package]]\nname = "synthetic"\nversion = "0.0.0"\n'
    )
    (root / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\nimporters: {}\npackages: {}\n")
    (root / "sbom.json").write_text(json.dumps(generate_sbom(root)))
    result = verify_supply_chain(root_dir=root, env={})
    assert result.passed, result.errors
    return result


def test_cli_missing_and_synthetic_approved_inputs(tmp_path):
    synthetic_build(tmp_path / "build")
    command = [
        sys.executable,
        str(ROOT / "scripts/preflight.py"),
        "--build-root",
        str(tmp_path / "build"),
        "--tenant-id",
        "11111111-1111-4111-8111-111111111111",
        "--allowed-region",
        REGIONS[0],
    ]
    missing = subprocess.run(
        command
        + ["--binding", str(tmp_path / "missing"), "--consent", str(tmp_path / "missing-consent")],
        text=True,
        capture_output=True,
    )
    assert missing.returncode == 1
    assert json.loads(missing.stdout)["ready"] is False
    runtime_path, consent_path = tmp_path / "runtime.json", tmp_path / "consent.json"
    runtime_path.write_text(json.dumps(binding()))
    consent_path.write_text(json.dumps(consent()))
    result = subprocess.run(
        command
        + ["--binding", str(runtime_path), "--consent", str(consent_path), "--checked-at", NOW],
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["ready"] is True
    assert any(c["name"] == "supply_chain" for c in json.loads(result.stdout)["checks"])


def test_supply_chain_composition_uses_real_verifier(tmp_path):
    from proofops.application.preflight import combine_build_checks
    from proofops.application.supply_chain import verify_supply_chain

    blocked = combine_build_checks(check(), verify_supply_chain(root_dir=tmp_path, env={}))
    assert not blocked.ready
    assert any(c.name == "supply_chain" and c.status == "fail" for c in blocked.checks)
    assert not combine_build_checks(check(), None).ready


@pytest.mark.parametrize(
    "field,value", [("role", []), ("endpoint_region", []), ("account_id", 123456789012)]
)
def test_malformed_binding_values_fail_closed(field, value):
    runtime = binding()
    runtime[field] = value
    assert not check(runtime).ready


def http_client(*, role="admin", include_build_gate=True):
    from tempfile import TemporaryDirectory

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from proofops.adapters.local.auth_store import InMemoryMembershipStore, InMemorySessionStore
    from proofops.application.authorization import MembershipRecord, SessionRecord
    from proofops_api.auth import SESSION_COOKIE_NAME, AuthStore
    from proofops_api.preflight import build_preflight_router

    sessions, members = InMemorySessionStore(), InMemoryMembershipStore()
    sessions.put(
        SessionRecord(
            "http-session", AUTH.user_sub, AUTH.tenant_id, "seed", 2000000000.0, 2000000000.0, False
        )
    )
    members.put(MembershipRecord(AUTH.tenant_id, AUTH.user_sub, role, "active"))
    runtime, profile = binding(), consent()
    runtime_id, consent_id = (
        "00000000-0000-4000-8000-000000000001",
        "00000000-0000-4000-8000-000000000002",
    )
    runtime["runtime_binding_id"], profile["consent_profile_id"] = runtime_id, consent_id
    artifacts = {("runtime", runtime_id): runtime, ("consent", consent_id): profile}

    def resolve(auth, kind, profile_id):
        artifact = artifacts.get((kind, profile_id))
        if artifact is None or artifact["tenant_id"] != auth.tenant_id:
            raise TenantNotFoundError("profile not found")
        return copy.deepcopy(artifact)

    with TemporaryDirectory(prefix="task029-build-") as build_dir:
        build_evidence = synthetic_build(Path(build_dir)) if include_build_gate else None
    app = FastAPI()
    app.include_router(
        build_preflight_router(
            AuthStore(sessions, members),
            resolve_profile=resolve,
            allowed_regions=REGIONS,
            allowed_origin="https://testserver",
            build_result=build_evidence,
            clock=lambda: 1788912000.0,
        )
    )
    client = TestClient(app, base_url="https://testserver")
    client.cookies.set(SESSION_COOKIE_NAME, "http-session")
    client.headers.update(
        {
            "Origin": "https://testserver",
            "X-CSRF-Token": sessions.csrf_token_for("http-session"),
            "Idempotency-Key": "synthetic-key-00000001",
        }
    )
    payload = {
        "runtime_binding_id": runtime_id,
        "consent_profile_id": consent_id,
        "include_live_model_probe": False,
    }
    return client, payload, artifacts


def test_http_admin_preflight_and_idempotency_contract():
    client, payload, artifacts = http_client()
    first = client.post("/v1/preflight", json=payload)
    assert first.status_code == 200, first.text
    assert first.json()["ready"] is True
    assert client.post("/v1/preflight", json=payload).json() == first.json()
    schema = json.loads((ROOT / "contracts/jsonschema/api_models.schema.json").read_text())
    Draft202012Validator(schema["$defs"]["Preflight"], format_checker=FormatChecker()).validate(
        first.json()
    )
    conflict = client.post("/v1/preflight", json={**payload, "include_live_model_probe": True})
    assert conflict.status_code == 409
    assert "model_id" not in first.text
    assert "supply_chain" in first.text


@pytest.mark.parametrize(
    "change,status",
    [
        ("anonymous", 401),
        ("csrf", 403),
        ("origin", 403),
        ("idempotency", 400),
        ("extra", 422),
        ("uuid", 422),
        ("bool", 422),
    ],
)
def test_http_request_guards(change, status):
    client, payload, _ = http_client()
    if change == "anonymous":
        client.cookies.clear()
    elif change == "csrf":
        del client.headers["X-CSRF-Token"]
    elif change == "origin":
        client.headers["Origin"] = "https://unapproved.example"
    elif change == "idempotency":
        del client.headers["Idempotency-Key"]
    elif change == "extra":
        payload["approved"] = True
    elif change == "uuid":
        payload["runtime_binding_id"] = "secret-invalid-id"
    elif change == "bool":
        payload["include_live_model_probe"] = "true"
    result = client.post("/v1/preflight", json=payload)
    assert result.status_code == status, result.text
    assert "error" in result.json()
    assert "secret-invalid-id" not in result.text


def test_http_requires_admin_and_hides_foreign_profiles():
    client, payload, _ = http_client(role="editor")
    assert client.post("/v1/preflight", json=payload).status_code == 403
    client, payload, artifacts = http_client()
    artifacts[("runtime", payload["runtime_binding_id"])]["tenant_id"] = (
        "22222222-2222-4222-8222-222222222222"
    )
    foreign = client.post("/v1/preflight", json=payload)
    assert foreign.status_code == 404
    payload["runtime_binding_id"] = "00000000-0000-4000-8000-000000000099"
    missing = client.post("/v1/preflight", json=payload)
    assert missing.status_code == 404
    assert foreign.json()["error"]["message"] == missing.json()["error"]["message"]


def test_http_missing_build_evidence_and_live_probe_stay_blocked():
    client, payload, _ = http_client(include_build_gate=False)
    result = client.post("/v1/preflight", json=payload)
    assert result.status_code == 200
    assert result.json()["ready"] is False
    assert any(
        c["name"] == "supply_chain" and c["status"] == "not_run" for c in result.json()["checks"]
    )


def test_http_rate_limit_and_concurrent_idempotent_replay():
    from concurrent.futures import ThreadPoolExecutor

    client, payload, _ = http_client()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: client.post("/v1/preflight", json=payload), range(4)))
    assert all(r.status_code == 200 and r.json() == results[0].json() for r in results)
    for _ in range(6):
        assert client.post("/v1/preflight", json=payload).status_code == 200
    limited = client.post("/v1/preflight", json=payload)
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) > 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("runtime_binding_id", "synthetic-invalid-id"),
        (
            "inference_profile_arn",
            "arn:aws:bedrock:ap-northeast-2:111111111111:inference-profile/synthetic",
        ),
        ("inference_profile_arn", "arn:aws:bedrock:ap-northeast-2:000000000000:inference-profile/"),
        ("inference_profile_arn", "arn:aws:bedrock:ap-northeast-2::inference-profile/synthetic"),
        ("model_id", "arn:aws:bedrock:us-east-1::foundation-model/synthetic.model"),
        ("model_id", "arn:aws:bedrock:ap-northeast-2:111111111111:inference-profile/synthetic"),
        ("model_id", "https://unapproved.example/model"),
    ],
)
def test_identifiers_and_full_arn_binding(field, value):
    runtime = binding()
    runtime[field] = value
    assert not check(runtime).ready


def test_invalid_consent_identifier_is_not_approval():
    profile = consent()
    profile["consent_profile_id"] = "invalid"
    assert not check(profile=profile).ready


def test_exact_synthetic_profile_dispatch_and_all_region_gate():
    # Fabricated ARN-shaped *synthetic fixture*, never a claimed AWS binding.
    runtime = binding()
    target = "arn:aws:bedrock:ap-northeast-2:000000000000:inference-profile/synthetic"
    runtime["inference_profile_arn"] = target
    client = SyntheticBedrockClient()
    dispatch(client, runtime)
    assert client.calls[0]["modelId"] == target
    runtime["allowed_processing_regions"].append("us-east-1")
    denied = SyntheticBedrockClient()
    with pytest.raises(PreflightBlocked):
        dispatch(denied, runtime)
    assert not denied.calls


def test_http_host_header_cannot_choose_allowed_origin():
    client, payload, _ = http_client()
    response = client.post(
        "/v1/preflight",
        json=payload,
        headers={"Host": "unapproved.example", "Origin": "https://unapproved.example"},
    )
    assert response.status_code == 403


def test_registry_artifact_hash_and_identity_are_verified(tmp_path):
    from proofops.application.registry import Registry, RegistryNotFound, artifact_sha256

    runtime = binding()
    registry = Registry.sqlite(tmp_path / "registry.sqlite3")
    options = dict(
        status="approved",
        version="1",
        artifact=runtime,
        sha256=artifact_sha256(runtime),
        approved_by="synthetic-reviewer",
        approved_at=NOW,
        local_synthetic=True,
    )
    registry.with_option(
        AUTH.tenant_id,
        "runtime",
        runtime["runtime_binding_id"],
        "Synthetic approved model",
        **options,
    )
    resolved = registry.resolve_profile(AUTH, "runtime", runtime["runtime_binding_id"])
    assert check(resolved).ready
    runtime["allowed_processing_regions"].append("us-east-1")
    assert check(registry.resolve_profile(AUTH, "runtime", runtime["runtime_binding_id"])).ready
    with pytest.raises(TypeError):
        resolved["version"] = "overwritten"
    with pytest.raises(RegistryNotFound):
        registry.resolve_profile(AUTH, "consent", consent()["consent_profile_id"])
    with pytest.raises(ValueError):
        Registry().with_option(
            AUTH.tenant_id,
            "runtime",
            runtime["runtime_binding_id"],
            "Synthetic mismatched artifact",
            **options,
        )
    fresh = binding()
    with pytest.raises(ValueError):
        Registry().with_option(
            AUTH.tenant_id,
            "runtime",
            fresh["runtime_binding_id"],
            "Synthetic stale version",
            **{**options, "artifact": fresh, "version": "2"},
        )
