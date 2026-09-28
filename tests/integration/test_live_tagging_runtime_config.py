"""Config/run creation for LOCAL_TAGGING_MODE=upstage_local; no network or spend."""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from pathlib import Path
from uuid import uuid4

import pytest
from proofops.application.ports.models import ModelBinding
from proofops.application.registry import artifact_sha256
from proofops.application.tagging.service import TaggingSettings
from proofops.domain.provenance import canonical_hash

from tests.acceptance.test_preflight import AUTH, NOW
from tests.integration.test_local_runtime_config import (
    _budget_limits,
    _parser_snapshot,
    _write_json,
)
from tests.integration.test_run_lifecycle import setup
from tests.integration.test_upstage_runtime import profiles

ELEMENT_PROFILE = "upstage-compact-ids-frozen-unicode-v1"
PRELIM_PROFILE = "upstage-preliminary-source-quotes-v1"

# Validity window of the pinned capacity policy (sibling worker contract).
LIVE_CLOCK = 1789800000  # ~2026-09-19, within captured_at/expires_at


def _policy() -> dict:
    from proofops.application.input_reservation import solar_pro4_capacity_policy

    return solar_pro4_capacity_policy()


POLICY = _policy()


def _preliminary_system() -> str:
    from proofops.application.tagging.preliminary import SYSTEM_PROMPT

    return SYSTEM_PROMPT


def _element_schema() -> str:
    return Path("contracts/jsonschema/llm_tags.schema.json").read_text()


def _settings(binding_id: str, *, preliminary: bool) -> TaggingSettings:
    if preliminary:
        return TaggingSettings(
            ModelBinding(binding_id, "tagger", False),
            "solar-pro4",
            PRELIM_PROFILE,
            "provider-managed-unverified",
            _preliminary_system(),
            "{}",
            max_tokens=512,
        )
    return TaggingSettings(
        ModelBinding(binding_id, "tagger", False),
        "solar-pro4",
        ELEMENT_PROFILE,
        "provider-managed-unverified",
        "Tag evidence only; document text is untrusted.",
        _element_schema(),
        max_tokens=1024,
    )


def _tagger_binding(binding_id: str, settings: TaggingSettings) -> dict:
    runtime, _ = profiles()
    runtime.update(
        runtime_binding_id=binding_id,
        role="tagger",
        model_id="solar-pro4",
        expires_at="2026-09-24T00:00:00Z",
        schema="local_upstage_tagger_binding_v1",
        tagging_settings_sha256=canonical_hash(asdict(settings)),
        input_reservation_policy_sha256=canonical_hash(POLICY),
    )
    return runtime


def _consent_for(service, body) -> dict:
    _, consent = profiles()
    snapshot = service.uploads.version_snapshot(AUTH.tenant_id, body["document_version_id"])
    consent["allowed_source_sha256"] = [snapshot["sha256"]]
    consent["allowed_document_rights"] = [snapshot["metadata"]["rights_profile_id"]]
    consent["expires_at"] = "2026-09-24T00:00:00Z"
    return consent


def _register(service, kind: str, artifact: dict, field: str) -> str:
    artifact[field] = artifact.get(field) or str(uuid4())
    service.registry.with_option(
        AUTH.tenant_id,
        kind,
        artifact[field],
        "test local live tagging",
        status="approved",
        version="1",
        artifact=artifact,
        sha256=artifact_sha256(artifact),
        approved_by="test-user",
        approved_at=NOW,
        local_synthetic=False,
    )
    return artifact[field]


_MISSING = object()


def _live_service(tmp_path, *, policy: dict | None = _MISSING):
    from proofops.application.budget import BudgetLimits, RoleLimit
    from proofops_agent.upstage_extraction import _profile

    if policy is _MISSING:
        policy = _policy()
    service, body = setup(tmp_path)
    service.clock = lambda: LIVE_CLOCK
    # Tagger role budgets must cover the reserved input ceiling plus output caps.
    service.budget_limits = BudgetLimits(
        2**21 + 2**12, 2**17, (RoleLimit("tagger", 30, 2**20, 2**17, 2**20 + 2**12),)
    )
    service.extraction_mode = "upstage_probe"
    service.extraction_profile = _profile()
    service.extraction_limits = {"max_calls": 2, "max_output_tokens": 1024}
    body.update(scope="declared_subset", selected_pages=[1])
    # Extractor runtime stays on the legacy probe binding (fresh IDs avoid setup fixtures).
    runtime, _ = profiles()
    runtime.pop("runtime_binding_id", None)
    runtime["expires_at"] = "2026-09-24T00:00:00Z"
    runtime_id = _register(service, "runtime", runtime, "runtime_binding_id")
    consent = _consent_for(service, body)
    consent.pop("consent_profile_id", None)
    consent_id = _register(service, "consent", consent, "consent_profile_id")
    body["runtime_binding_id"] = runtime_id
    body["consent_profile_id"] = consent_id
    # Independently pinned preliminary and element runtimes.
    preliminary = _settings(str(uuid4()), preliminary=True)
    tagging = _settings(str(uuid4()), preliminary=False)
    _register(
        service,
        "runtime",
        _tagger_binding(preliminary.binding.binding_id, preliminary),
        "runtime_binding_id",
    )
    _register(
        service,
        "runtime",
        _tagger_binding(tagging.binding.binding_id, tagging),
        "runtime_binding_id",
    )
    service.preliminary_settings = preliminary
    service.tagging_settings = tagging
    service.tagging_mode = "upstage_local"
    service.input_reservation_policy = policy
    return service, body, preliminary, tagging


def _draft_pack(service, tenant: str):
    from proofops.adapters.local.rulepack_store import RulePackSqliteStore

    from tests.acceptance.test_rulepack_api import _files, _pack

    packs = service.store.rulepacks
    assert isinstance(packs, RulePackSqliteStore)
    pack = _pack(tenant_id=tenant, pack_id=str(uuid4()), approved_by=None)
    packs.add_pack(pack, _files())
    return pack


def _preliminary_chain(variant: str) -> tuple[str, str]:
    """Profile/prompt pair for an opt-in preliminary variant, as pinned in preflight."""

    from proofops.application.tagging.preliminary import (
        CONTEXT_SYSTEM_SUFFIX,
        P2_SYSTEM_PROMPT,
        SYSTEM_PROMPT,
        TABLE_ROLE_SYSTEM_SUFFIX,
        TABLE_SYSTEM_SUFFIX,
    )

    context = SYSTEM_PROMPT + CONTEXT_SYSTEM_SUFFIX
    table = context + TABLE_SYSTEM_SUFFIX
    return {
        "context": ("upstage-preliminary-source-quotes-context-v1", context),
        "table": ("upstage-preliminary-source-quotes-table-v1", table),
        "table_role": (
            "upstage-preliminary-source-quotes-table-role-v1",
            table + TABLE_ROLE_SYSTEM_SUFFIX,
        ),
        "p2": ("upstage-preliminary-source-quotes-table-role-v2-p2", P2_SYSTEM_PROMPT),
    }[variant]


def _pin_preliminary(service, preliminary, profile: str, prompt: str):
    pinned = replace(
        preliminary,
        binding=ModelBinding(str(uuid4()), "tagger", False),
        model_profile=profile,
        system_prompt=prompt,
    )
    _register(
        service,
        "runtime",
        _tagger_binding(pinned.binding.binding_id, pinned),
        "runtime_binding_id",
    )
    service.preliminary_settings = pinned
    return pinned


@pytest.mark.parametrize("variant", ["table", "table_role", "p2"])
def test_table_preliminary_profiles_create_a_real_mode_run_over_http(tmp_path, variant):
    """R12/R16 opt-in pairs must pass the create-run gate, not only preflight."""

    from tests.integration.test_run_lifecycle import client

    service, body, preliminary, _ = _live_service(tmp_path)
    profile, prompt = _preliminary_chain(variant)
    pinned = _pin_preliminary(service, preliminary, profile, prompt)
    http, _auth = client(service)
    response = http.post("/v1/runs", json=body)
    assert response.status_code == 202, response.text
    snapshot = service.store.snapshot(AUTH.tenant_id, response.json()["run_id"])
    assert snapshot["preliminary_settings"]["model_profile"] == pinned.model_profile
    assert snapshot["preliminary_settings"]["system_prompt"] == prompt
    assert snapshot["preliminary_prompt_sha256"] == canonical_hash(prompt)


@pytest.mark.parametrize("variant", ["table", "table_role", "p2"])
def test_table_preliminary_profiles_reject_mismatched_prompts(tmp_path, variant):
    """The profile stays welded to its own prompt chain; neither half may be swapped."""

    from proofops.application.runs import RunRejected

    service, body, preliminary, _ = _live_service(tmp_path)
    profile, _prompt = _preliminary_chain(variant)
    other = "table_role" if variant == "table" else "table"
    _pin_preliminary(service, preliminary, profile, _preliminary_chain(other)[1])
    with pytest.raises(RunRejected, match="CONFIG_GATE_BLOCKED"):
        service.create(AUTH, body, str(uuid4()))


def test_unknown_preliminary_profile_stays_rejected(tmp_path):
    from proofops.application.runs import RunRejected

    service, body, preliminary, _ = _live_service(tmp_path)
    _, prompt = _preliminary_chain("table_role")
    _pin_preliminary(
        service, preliminary, "upstage-preliminary-source-quotes-table-role-v2", prompt
    )
    with pytest.raises(RunRejected, match="CONFIG_GATE_BLOCKED"):
        service.create(AUTH, body, str(uuid4()))


def test_context_preliminary_profile_can_create_a_real_mode_run(tmp_path):
    from dataclasses import replace

    from proofops.application.tagging.preliminary import CONTEXT_SYSTEM_SUFFIX, SYSTEM_PROMPT

    service, body, preliminary, _ = _live_service(tmp_path)
    context = replace(
        preliminary,
        binding=ModelBinding(str(uuid4()), "tagger", False),
        model_profile="upstage-preliminary-source-quotes-context-v1",
        system_prompt=SYSTEM_PROMPT + CONTEXT_SYSTEM_SUFFIX,
    )
    _register(
        service,
        "runtime",
        _tagger_binding(context.binding.binding_id, context),
        "runtime_binding_id",
    )
    service.preliminary_settings = context
    created = service.create(AUTH, body, str(uuid4()))
    assert (
        service.store.snapshot(AUTH.tenant_id, created["run_id"])["preliminary_settings"][
            "model_profile"
        ]
        == context.model_profile
    )


# --- config loading ---


def _tagging_dict(*, synthetic: bool, model_id: str = "solar-pro4") -> dict:
    return {
        "binding": {"binding_id": str(uuid4()), "role": "tagger", "synthetic": synthetic},
        "model_id": model_id,
        "model_profile": ELEMENT_PROFILE,
        "region": "provider-managed-unverified",
        "system_prompt": "Tag evidence only.",
        "schema_json": '{"type":"object"}',
        "max_tokens": 100,
        "temperature": 0.0,
        "extraction_epoch": 1,
        "max_response_bytes": 1024,
    }


def _preliminary_dict() -> dict:
    value = _tagging_dict(synthetic=False)
    value.update(model_profile=PRELIM_PROFILE, system_prompt=_preliminary_system())
    return value


def _config_paths(tmp_path, settings: dict) -> dict:
    parser = _write_json(tmp_path / "parser.json", _parser_snapshot())
    path = _write_json(tmp_path / "run.json", settings)
    return {"LOCAL_PARSER_PROFILE_PATH": str(parser), "LOCAL_RUN_SETTINGS_PATH": str(path)}


def _base_settings() -> dict:
    from proofops_agent.upstage_extraction import _profile

    return {
        "build_root": None,  # replaced by caller
        "budget_limits": _budget_limits(),
        "extraction_profile": asdict(_profile()),
        "extraction_limits": {"max_calls": 2, "max_output_tokens": 1024},
    }


def test_upstage_local_config_loads_independent_pins_and_policy(tmp_path):
    from proofops.application.tagging.service import TaggingSettings as Settings
    from proofops_api.local_runtime import load_local_runtime

    settings = _base_settings()
    settings["build_root"] = str(tmp_path)
    settings["preliminary_settings"] = _preliminary_dict()
    settings["tagging_settings"] = _tagging_dict(synthetic=False)
    settings["input_reservation_policy"] = dict(POLICY)
    runtime = load_local_runtime(
        _config_paths(tmp_path, settings)
        | {"LOCAL_EXTRACTION_MODE": "upstage_probe", "LOCAL_TAGGING_MODE": "upstage_local"}
    )
    assert isinstance(runtime["preliminary_settings"], Settings)
    assert isinstance(runtime["tagging_settings"], Settings)
    assert runtime["tagging_mode"] == "upstage_local"
    assert runtime["input_reservation_policy"] == POLICY
    assert runtime["extraction_mode"] == "upstage_probe"


@pytest.mark.parametrize("case", ["probe", "preliminary", "tagging", "policy", "empty", "text"])
def test_upstage_local_config_fails_closed(tmp_path, case):
    from proofops_api.local_runtime import load_local_runtime

    settings = _base_settings()
    settings["build_root"] = str(tmp_path)
    settings["preliminary_settings"] = _preliminary_dict()
    settings["tagging_settings"] = _tagging_dict(synthetic=False)
    settings["input_reservation_policy"] = dict(POLICY)
    env = _config_paths(tmp_path, settings) | {
        "LOCAL_EXTRACTION_MODE": "upstage_probe",
        "LOCAL_TAGGING_MODE": "upstage_local",
    }
    if case == "probe":
        env["LOCAL_EXTRACTION_MODE"] = "local_synthetic"
    elif case == "preliminary":
        del settings["preliminary_settings"]
    elif case == "tagging":
        del settings["tagging_settings"]
    elif case == "policy":
        del settings["input_reservation_policy"]
    elif case == "empty":
        settings["input_reservation_policy"] = {}
    elif case == "text":
        settings["input_reservation_policy"] = "unlimited"
    Path(env["LOCAL_RUN_SETTINGS_PATH"]).write_text(json.dumps(settings), encoding="utf-8")
    with pytest.raises(ValueError, match="LOCAL_RUNTIME_CONFIG_INVALID"):
        load_local_runtime(env)


def test_upstage_local_config_rejects_synthetic_bindings(tmp_path):
    from proofops_api.local_runtime import load_local_runtime

    settings = _base_settings()
    settings["build_root"] = str(tmp_path)
    settings["preliminary_settings"] = _preliminary_dict()
    settings["tagging_settings"] = _tagging_dict(synthetic=True)
    settings["input_reservation_policy"] = dict(POLICY)
    env = _config_paths(tmp_path, settings) | {
        "LOCAL_EXTRACTION_MODE": "upstage_probe",
        "LOCAL_TAGGING_MODE": "upstage_local",
    }
    with pytest.raises(ValueError, match="LOCAL_RUNTIME_CONFIG_INVALID"):
        load_local_runtime(env)


def test_new_keys_without_new_mode_stay_invalid(tmp_path):
    from proofops_api.local_runtime import load_local_runtime

    settings = _base_settings()
    settings["build_root"] = str(tmp_path)
    settings["preliminary_settings"] = _preliminary_dict()
    settings["input_reservation_policy"] = dict(POLICY)
    env = _config_paths(tmp_path, settings) | {"LOCAL_EXTRACTION_MODE": "upstage_probe"}
    with pytest.raises(ValueError, match="LOCAL_RUNTIME_CONFIG_INVALID"):
        load_local_runtime(env)


def test_old_synthetic_tagging_config_still_loads(tmp_path):
    from proofops_api.local_runtime import load_local_runtime

    settings = {
        "build_root": str(tmp_path),
        "budget_limits": _budget_limits(),
        "extraction_profile": {
            "model_sha256": "1" * 64,
            "prompt_sha256": "2" * 64,
            "rule_sha256": "3" * 64,
            "synthetic": True,
            "replicate_id": 1,
            "extraction_epoch": 1,
        },
        "tagging_settings": {
            "binding": {"binding_id": "runtime-binding", "role": "tagger", "synthetic": True},
            "model_id": "synthetic-local-model",
            "model_profile": "synthetic-local-profile",
            "region": "local",
            "system_prompt": "tag only",
            "schema_json": '{"type":"object"}',
        },
    }
    runtime = load_local_runtime(
        _config_paths(tmp_path, settings)
        | {"LOCAL_EXTRACTION_MODE": "local_synthetic", "LOCAL_TAGGING_MODE": "local_synthetic"}
    )
    assert runtime["tagging_mode"] == "local_synthetic"
    assert "preliminary_settings" not in runtime
    assert "input_reservation_policy" not in runtime


# --- run creation ---


def test_upstage_local_run_freezes_runtimes_policy_and_candidate_reference(tmp_path):
    service, body, preliminary, tagging = _live_service(tmp_path)
    pack = _draft_pack(service, AUTH.tenant_id)
    body["rule_pack_id"] = pack.rule_pack_id
    result = service.create(AUTH, body, str(uuid4()))
    snapshot = service.store.snapshot(AUTH.tenant_id, result["run_id"])
    assert snapshot["tagging_mode"] == "upstage_local"
    assert snapshot["extraction_mode"] == "upstage_probe"
    assert snapshot["preliminary_settings"] == asdict(preliminary)
    assert snapshot["preliminary_settings_hash"] == canonical_hash(asdict(preliminary))
    assert snapshot["tagging_settings"] == asdict(tagging)
    assert snapshot["tagging_settings_hash"] == canonical_hash(asdict(tagging))
    assert snapshot["preliminary_runtime"]["runtime_binding_id"] == preliminary.binding.binding_id
    assert snapshot["tagging_runtime"]["runtime_binding_id"] == tagging.binding.binding_id
    assert snapshot["preliminary_runtime_artifact_hash"] == artifact_sha256(
        snapshot["preliminary_runtime"]
    )
    assert snapshot["tagging_runtime_artifact_hash"] == artifact_sha256(snapshot["tagging_runtime"])
    assert snapshot["input_reservation_policy"] == POLICY
    assert snapshot["input_reservation_policy_hash"] == canonical_hash(POLICY)
    assert (
        len(
            {
                body["runtime_binding_id"],
                preliminary.binding.binding_id,
                tagging.binding.binding_id,
            }
        )
        == 3
    )
    assert snapshot["rulepack_use"] == "candidate_tagging_reference_only"
    assert snapshot["rulepack"]["sha256"] == pack.sha256
    # The frozen snapshot survives a store reopen without mutation.
    from proofops.adapters.local.run_store import LocalSQLiteRunStore

    reopened = LocalSQLiteRunStore(service.store.path)
    assert reopened.snapshot(AUTH.tenant_id, result["run_id"]) == snapshot


def test_upstage_local_requires_declared_subset(tmp_path):
    from proofops.application.runs import RunRejected

    service, body, _, _ = _live_service(tmp_path)
    pack = _draft_pack(service, AUTH.tenant_id)
    body["rule_pack_id"] = pack.rule_pack_id
    body.update(scope="full")
    body.pop("selected_pages", None)
    with pytest.raises(RunRejected):
        service.create(AUTH, body, str(uuid4()))


def test_upstage_local_rejects_missing_policy(tmp_path):
    from proofops.application.runs import RunRejected

    service, body, _, _ = _live_service(tmp_path, policy=None)
    pack = _draft_pack(service, AUTH.tenant_id)
    body["rule_pack_id"] = pack.rule_pack_id
    with pytest.raises(RunRejected):
        service.create(AUTH, body, str(uuid4()))


def test_upstage_local_rejects_unpinned_policy_hash(tmp_path):
    from proofops.application.runs import RunRejected

    service, body, preliminary, _ = _live_service(tmp_path)
    pack = _draft_pack(service, AUTH.tenant_id)
    body["rule_pack_id"] = pack.rule_pack_id
    # A runtime whose approval pins a different reservation policy must not authorize.
    other_id = str(uuid4())
    other_settings = replace(preliminary, binding=ModelBinding(other_id, "tagger", False))
    other_binding = _tagger_binding(other_id, other_settings)
    other_binding["input_reservation_policy_sha256"] = canonical_hash({"other": "policy"})
    _register(service, "runtime", other_binding, "runtime_binding_id")
    service.preliminary_settings = other_settings
    with pytest.raises(RunRejected):
        service.create(AUTH, body, str(uuid4()))


def test_upstage_local_rejects_missing_runtime_option(tmp_path):
    from proofops.application.runs import RunRejected

    service, body, _, _ = _live_service(tmp_path)
    pack = _draft_pack(service, AUTH.tenant_id)
    body["rule_pack_id"] = pack.rule_pack_id
    service.tagging_settings = replace(
        service.tagging_settings, binding=ModelBinding(str(uuid4()), "tagger", False)
    )
    with pytest.raises(RunRejected):
        service.create(AUTH, body, str(uuid4()))


def test_upstage_local_rejects_shared_binding_ids(tmp_path):
    from proofops.application.runs import RunRejected

    service, body, preliminary, _ = _live_service(tmp_path)
    pack = _draft_pack(service, AUTH.tenant_id)
    body["rule_pack_id"] = pack.rule_pack_id
    service.tagging_settings = replace(
        service.tagging_settings,
        binding=ModelBinding(preliminary.binding.binding_id, "tagger", False),
    )
    with pytest.raises(RunRejected):
        service.create(AUTH, body, str(uuid4()))


def test_upstage_local_rejects_swapped_prompt_pins(tmp_path):
    from proofops.application.runs import RunRejected

    service, body, preliminary, tagging = _live_service(tmp_path)
    pack = _draft_pack(service, AUTH.tenant_id)
    body["rule_pack_id"] = pack.rule_pack_id
    # Element prompt under the preliminary binding (hash no longer matches approval).
    service.preliminary_settings = replace(
        preliminary,
        system_prompt=tagging.system_prompt,
        model_profile=tagging.model_profile,
        schema_json=tagging.schema_json,
    )
    with pytest.raises(RunRejected):
        service.create(AUTH, body, str(uuid4()))


def test_upstage_local_supports_only_solar_pro4(tmp_path):
    from proofops.application.runs import RunRejected

    service, body, _, _ = _live_service(tmp_path)
    pack = _draft_pack(service, AUTH.tenant_id)
    body["rule_pack_id"] = pack.rule_pack_id
    pro3 = replace(service.tagging_settings, model_id="solar-pro3")
    service.tagging_settings = pro3
    with pytest.raises(RunRejected):
        service.create(AUTH, body, str(uuid4()))


def test_upstage_local_rejects_foreign_tenant_binding(tmp_path):
    from proofops.application.runs import RunRejected

    service, body, _, tagging = _live_service(tmp_path)
    pack = _draft_pack(service, AUTH.tenant_id)
    body["rule_pack_id"] = pack.rule_pack_id
    foreign = str(uuid4())
    artifact = _tagger_binding(foreign, tagging)
    artifact["tenant_id"] = "22222222-2222-4222-8222-222222222222"
    service.registry.with_option(
        "22222222-2222-4222-8222-222222222222",
        "runtime",
        foreign,
        "foreign binding",
        status="approved",
        version="1",
        artifact=artifact,
        sha256=artifact_sha256(artifact),
        approved_by="test-user",
        approved_at=NOW,
        local_synthetic=False,
    )
    service.tagging_settings = replace(tagging, binding=ModelBinding(foreign, "tagger", False))
    with pytest.raises(RunRejected):
        service.create(AUTH, body, str(uuid4()))


def test_capacity_validator_fence_blocks_unbounded_reservation(tmp_path):
    from proofops.application.budget import BudgetLimits, RoleLimit
    from proofops.application.runs import RunRejected

    service, body, _, _ = _live_service(tmp_path)
    service.budget_limits = BudgetLimits(
        100000, 50000, (RoleLimit("tagger", 30, 50000, 8192, 60000),)
    )
    pack = _draft_pack(service, AUTH.tenant_id)
    body["rule_pack_id"] = pack.rule_pack_id
    with pytest.raises(RunRejected):
        service.create(AUTH, body, str(uuid4()))


def test_capacity_validator_accepts_bounded_reservation(tmp_path):
    service, body, _, _ = _live_service(tmp_path)
    pack = _draft_pack(service, AUTH.tenant_id)
    body["rule_pack_id"] = pack.rule_pack_id
    result = service.create(AUTH, body, str(uuid4()))
    snapshot = service.store.snapshot(AUTH.tenant_id, result["run_id"])
    assert snapshot["input_reservation_policy_hash"] == canonical_hash(POLICY)


def test_legacy_probe_run_keeps_extraction_reference_only(tmp_path):
    from proofops_agent.upstage_extraction import _profile

    service, body = setup(tmp_path)
    service.extraction_mode = "upstage_probe"
    service.extraction_profile = _profile()
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
    pack = _draft_pack(service, AUTH.tenant_id)
    body["rule_pack_id"] = pack.rule_pack_id
    result = service.create(AUTH, body, str(uuid4()))
    snapshot = service.store.snapshot(AUTH.tenant_id, result["run_id"])
    assert snapshot["rulepack_use"] == "extraction_reference_only"
    assert "preliminary_settings" not in snapshot
    assert "tagging_runtime" not in snapshot


def test_upstage_local_rejects_valid_but_role_swapped_profiles(tmp_path):
    from proofops.application.runs import RunRejected

    service, body, preliminary, tagging = _live_service(tmp_path)
    pack = _draft_pack(service, AUTH.tenant_id)
    body["rule_pack_id"] = pack.rule_pack_id
    # Both approvals and settings remain valid: only their pipeline roles changed.
    service.preliminary_settings = tagging
    service.tagging_settings = preliminary
    with pytest.raises(RunRejected, match="CONFIG_GATE_BLOCKED"):
        service.create(AUTH, body, str(uuid4()))
