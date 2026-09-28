"""Local real-model authorization; no network or production approval."""

from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

import pytest
from proofops.application.preflight import check_local_upstage_binding
from proofops.domain.provenance import canonical_hash

from tests.acceptance.test_preflight import AUTH, NOW


def _model_hash(model_id: str) -> str:
    return canonical_hash({"model": model_id, "provider": "upstage", "transport": "UpstageProbe"})


def _profile_for(model_id: str):
    from proofops_agent.upstage_extraction import _profile

    if model_id in ("solar-pro3", "solar-pro4"):
        return _profile(model_id)
    return replace(_profile(), model_sha256=_model_hash(model_id))


def profiles():
    common = dict(
        tenant_id=AUTH.tenant_id,
        status="approved",
        version="1",
        approved_by="test-user",
        approved_at=NOW,
        purpose="local_test",
        provider="upstage",
        expires_at="2026-09-16T00:00:00Z",
    )
    runtime = dict(
        common,
        runtime_binding_id="00000000-0000-4000-8000-000000000001",
        role="extractor",
        model_id="solar-pro3",
        endpoint="https://api.upstage.ai/v1/chat/completions",
        budget_limit_usd="10.00",
    )
    consent = dict(
        common,
        consent_profile_id="00000000-0000-4000-8000-000000000002",
        allowed_source_sha256=["a" * 64],
        allowed_document_rights=["report-test"],
        allow_cross_tenant_cache=False,
        allow_agentcore_memory=False,
    )
    return runtime, consent


def test_local_authorization_does_not_claim_region_verification():
    runtime, consent = profiles()
    result = check_local_upstage_binding(
        binding=runtime, consent=consent, auth=AUTH, checked_at=NOW, source_sha256="a" * 64
    )
    assert result.ready
    assert next(c for c in result.checks if c.name == "processing_regions").status == "not_run"


def test_local_model_hash_binding_requires_exact_match():
    runtime, consent = profiles()
    valid = _model_hash("solar-pro3")
    assert check_local_upstage_binding(
        binding=runtime,
        consent=consent,
        auth=AUTH,
        checked_at=NOW,
        source_sha256="a" * 64,
        model_sha256=valid,
    ).ready
    assert not check_local_upstage_binding(
        binding=runtime,
        consent=consent,
        auth=AUTH,
        checked_at=NOW,
        source_sha256="a" * 64,
        model_sha256=_model_hash("solar-pro4"),
    ).ready
    assert not check_local_upstage_binding(
        binding=runtime,
        consent=consent,
        auth=AUTH,
        checked_at=NOW,
        source_sha256="a" * 64,
        model_sha256="0" * 64,
    ).ready
    other = deepcopy(profiles())
    other[0]["model_id"] = "solar-pro99"
    assert not check_local_upstage_binding(
        binding=other[0],
        consent=other[1],
        auth=AUTH,
        checked_at=NOW,
        source_sha256="a" * 64,
        model_sha256=_model_hash("solar-pro99"),
    ).ready


def _create_probe_run(tmp_path, *, model_id, profile):
    from proofops.application.registry import artifact_sha256

    from tests.integration.test_run_lifecycle import setup

    service, body = setup(tmp_path)
    service.extraction_mode = "upstage_probe"
    service.extraction_profile = profile
    service.extraction_limits = {"max_calls": 2, "max_output_tokens": 1024}
    body.update(scope="declared_subset", selected_pages=[1])
    pair = profiles()
    pair[0]["model_id"] = model_id
    pair[1]["allowed_source_sha256"] = [
        service.uploads.version_snapshot(AUTH.tenant_id, body["document_version_id"])["sha256"]
    ]
    pair[1]["allowed_document_rights"] = [
        service.uploads.version_snapshot(AUTH.tenant_id, body["document_version_id"])["metadata"][
            "rights_profile_id"
        ]
    ]
    for kind, artifact, field in [
        ("runtime", pair[0], "runtime_binding_id"),
        ("consent", pair[1], "consent_profile_id"),
    ]:
        artifact[field] = str(uuid4())
        body[field] = artifact[field]
        service.registry.with_option(
            AUTH.tenant_id,
            kind,
            artifact[field],
            "test local probe",
            status="approved",
            version="1",
            artifact=artifact,
            sha256=artifact_sha256(artifact),
            approved_by="test-user",
            approved_at=NOW,
            local_synthetic=False,
        )
    return service, body


@pytest.mark.parametrize("model_id", ["solar-pro3", "solar-pro4"])
def test_probe_run_creation_binds_matching_model_hash(tmp_path, model_id):
    profile = _profile_for(model_id)
    # The frozen real profile hash must equal the canonical model descriptor.
    assert profile.model_sha256 == _model_hash(model_id)
    assert profile.synthetic is False
    service, body = _create_probe_run(tmp_path, model_id=model_id, profile=profile)
    result = service.create(AUTH, body, str(uuid4()))
    snapshot = service.store.snapshot(AUTH.tenant_id, result["run_id"])
    assert snapshot["runtime"]["model_id"] == model_id
    assert snapshot["extraction_profile"]["model_sha256"] == _model_hash(model_id)


@pytest.mark.parametrize(
    "model_id,profile_model",
    [
        ("solar-pro3", "solar-pro4"),
        ("solar-pro4", "solar-pro3"),
        ("solar-pro99", "solar-pro3"),
        ("solar-pro3", "solar-pro99"),
    ],
)
def test_probe_run_creation_rejects_model_hash_mismatch(tmp_path, model_id, profile_model):
    from proofops.application.runs import RunRejected

    service, body = _create_probe_run(
        tmp_path, model_id=model_id, profile=_profile_for(profile_model)
    )
    with pytest.raises(RunRejected):
        service.create(AUTH, body, str(uuid4()))


def test_probe_run_creation_rejects_missing_profile_without_bypass(tmp_path):
    from proofops.application.runs import RunRejected

    from tests.integration.test_run_lifecycle import setup

    service, body = setup(tmp_path)
    service.extraction_mode = "upstage_probe"
    service.extraction_profile = None
    service.extraction_limits = {"max_calls": 2, "max_output_tokens": 1024}
    body.update(scope="declared_subset", selected_pages=[1])
    pair = profiles()
    pair[1]["allowed_source_sha256"] = [
        service.uploads.version_snapshot(AUTH.tenant_id, body["document_version_id"])["sha256"]
    ]
    pair[1]["allowed_document_rights"] = [
        service.uploads.version_snapshot(AUTH.tenant_id, body["document_version_id"])["metadata"][
            "rights_profile_id"
        ]
    ]
    from proofops.application.registry import artifact_sha256

    for kind, artifact, field in [
        ("runtime", pair[0], "runtime_binding_id"),
        ("consent", pair[1], "consent_profile_id"),
    ]:
        artifact[field] = str(uuid4())
        body[field] = artifact[field]
        service.registry.with_option(
            AUTH.tenant_id,
            kind,
            artifact[field],
            "test local probe",
            status="approved",
            version="1",
            artifact=artifact,
            sha256=artifact_sha256(artifact),
            approved_by="test-user",
            approved_at=NOW,
            local_synthetic=False,
        )
    with pytest.raises(RunRejected):
        service.create(AUTH, body, str(uuid4()))
    runtime, consent = profiles()
    result = check_local_upstage_binding(
        binding=runtime, consent=consent, auth=AUTH, checked_at=NOW, source_sha256="a" * 64
    )
    assert result.ready
    assert next(c for c in result.checks if c.name == "processing_regions").status == "not_run"


@pytest.mark.parametrize(
    "target,key,value",
    [
        (0, "purpose", "production"),
        (0, "provider", "other"),
        (0, "budget_limit_usd", "20.01"),
        (0, "expires_at", "2026-09-08T00:00:00Z"),
        (0, "role", "tagger"),
        (1, "allowed_source_sha256", ["b" * 64]),
        (1, "allowed_source_sha256", ["*"]),
        (1, "allow_cross_tenant_cache", True),
        (1, "approved_by", ""),
    ],
)
def test_invalid_local_authorization_is_blocked(target, key, value):
    pair = deepcopy(profiles())
    pair[target][key] = value
    assert not check_local_upstage_binding(
        binding=pair[0], consent=pair[1], auth=AUTH, checked_at=NOW, source_sha256="a" * 64
    ).ready


@pytest.mark.parametrize("fractional", [False, True])
def test_run_freezes_explicit_real_limits_and_source_authorization(tmp_path, fractional):
    from uuid import uuid4

    from proofops.application.registry import artifact_sha256
    from proofops.application.runs import RunRejected
    from proofops_agent.upstage_extraction import _profile

    from tests.integration.test_run_lifecycle import setup

    service, body = setup(tmp_path)
    service.extraction_mode = "upstage_probe"
    service.extraction_profile = _profile()
    service.extraction_limits = {"max_calls": 2, "max_output_tokens": 1024}
    body.update(scope="declared_subset", selected_pages=[1])
    pair = profiles()
    if fractional:
        from datetime import UTC, datetime

        timestamp = service.clock()
        service.clock = lambda: timestamp + 0.5
        for profile in pair:
            profile["approved_at"] = datetime.fromtimestamp(timestamp + 0.1, UTC).isoformat()
    pair[1]["allowed_source_sha256"] = [
        service.uploads.version_snapshot(AUTH.tenant_id, body["document_version_id"])["sha256"]
    ]
    pair[1]["allowed_document_rights"] = [
        service.uploads.version_snapshot(AUTH.tenant_id, body["document_version_id"])["metadata"][
            "rights_profile_id"
        ]
    ]
    for kind, artifact, field in [
        ("runtime", pair[0], "runtime_binding_id"),
        ("consent", pair[1], "consent_profile_id"),
    ]:
        artifact[field] = str(uuid4())
        body[field] = artifact[field]
        service.registry.with_option(
            AUTH.tenant_id,
            kind,
            artifact[field],
            "test local probe",
            status="approved",
            version="1",
            artifact=artifact,
            sha256=artifact_sha256(artifact),
            approved_by="test-user",
            approved_at=NOW,
            local_synthetic=False,
        )
    result = service.create(AUTH, body, str(uuid4()))
    snapshot = service.store.snapshot(AUTH.tenant_id, result["run_id"])
    assert snapshot["extraction_profile"]["synthetic"] is False
    assert snapshot["extraction_limits"] == service.extraction_limits
    with pytest.raises(RunRejected):
        service.create(AUTH, dict(body, scope="full"), str(uuid4()))


def test_explicit_config_loads_real_profile_and_limits_without_credentials(tmp_path):
    from dataclasses import asdict

    from proofops_agent.upstage_extraction import _profile
    from proofops_api.local_runtime import load_local_runtime

    from tests.integration.test_local_runtime_config import (
        _budget_limits,
        _parser_snapshot,
        _write_json,
    )

    parser = _write_json(tmp_path / "parser.json", _parser_snapshot())
    settings = _write_json(
        tmp_path / "run.json",
        dict(
            build_root=str(tmp_path),
            budget_limits=_budget_limits(),
            extraction_profile=asdict(_profile()),
            extraction_limits=dict(max_calls=2, max_output_tokens=1024),
        ),
    )
    value = load_local_runtime(
        dict(
            LOCAL_PARSER_PROFILE_PATH=str(parser),
            LOCAL_RUN_SETTINGS_PATH=str(settings),
            LOCAL_EXTRACTION_MODE="upstage_probe",
        )
    )
    assert value["extraction_profile"].synthetic is False
    assert value["extraction_limits"]["max_calls"] == 2


def test_extraction_reference_does_not_require_or_invent_rule_approval(tmp_path):
    from uuid import uuid4

    from proofops.adapters.local.rulepack_store import RulePackSqliteStore

    from tests.acceptance.test_rulepack_api import _files, _pack

    store = RulePackSqliteStore(tmp_path / "rules.sqlite3")
    pack = _pack(pack_id=str(uuid4()), approved_by=None)
    store.add_pack(pack, _files())
    with store._transaction() as db:
        frozen = store.extraction_snapshot_transaction(
            db, pack.tenant_id, "disclosure", pack.rule_pack_id
        )
    assert frozen.approved_by is None and frozen.status == "validated"
    assert store.active_pack_id(pack.tenant_id, "disclosure") is None
    with store._transaction() as db, pytest.raises(ValueError):
        store.extraction_snapshot_transaction(db, pack.tenant_id, "advertising", pack.rule_pack_id)


def test_pro4_binding_requires_explicit_model_hash_even_for_preflight():
    from proofops_agent.upstage_extraction import _profile

    runtime, consent = profiles()
    runtime["model_id"] = "solar-pro4"
    args = dict(binding=runtime, consent=consent, auth=AUTH, checked_at=NOW)
    assert not check_local_upstage_binding(**args).ready
    assert check_local_upstage_binding(
        **args, model_sha256=_profile("solar-pro4").model_sha256
    ).ready


@pytest.mark.parametrize(
    "cap,accepted",
    [("10.00", True), ("20.00", True), ("20.01", False), ("NaN", False), (20, False)],
)
def test_local_binding_accepts_only_user_authorized_caps(cap, accepted):
    runtime, consent = profiles()
    runtime["budget_limit_usd"] = cap
    result = check_local_upstage_binding(
        binding=runtime,
        consent=consent,
        auth=AUTH,
        checked_at=NOW,
        source_sha256="a" * 64,
    )
    assert result.ready is accepted
