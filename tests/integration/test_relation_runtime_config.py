"""Opt-in relation-stage pins; legacy live snapshots stay byte-for-byte unchanged."""

from dataclasses import asdict, replace
from uuid import uuid4

import pytest
from proofops.application.tagging.relations import SYSTEM_PROMPT
from proofops.domain.provenance import canonical_hash

from tests.integration.test_live_tagging_runtime_config import (
    AUTH,
    _draft_pack,
    _live_service,
    _settings,
)


def _relation(service):
    relation = replace(
        _settings(str(uuid4()), preliminary=False),
        model_profile="upstage-relation-source-quotes-v1",
        system_prompt=SYSTEM_PROMPT,
        schema_json="{}",
    )
    from tests.integration.test_live_tagging_runtime_config import _register, _tagger_binding

    _register(
        service,
        "runtime",
        _tagger_binding(relation.binding.binding_id, relation),
        "runtime_binding_id",
    )
    return relation


def test_relation_settings_are_frozen_as_an_independent_optional_group(tmp_path):
    service, body, preliminary, tagging = _live_service(tmp_path)
    relation = _relation(service)
    service.relation_settings = relation
    pack = _draft_pack(service, AUTH.tenant_id)
    body["rule_pack_id"] = pack.rule_pack_id
    result = service.create(AUTH, body, str(uuid4()))
    snapshot = service.store.snapshot(AUTH.tenant_id, result["run_id"])
    assert snapshot["relation_settings"] == asdict(relation)
    assert snapshot["relation_settings_hash"] == canonical_hash(asdict(relation))
    assert snapshot["relation_runtime"]["runtime_binding_id"] == relation.binding.binding_id
    assert snapshot["relation_runtime_artifact_hash"]


def test_legacy_live_snapshot_has_no_relation_pin_group(tmp_path):
    service, body, *_ = _live_service(tmp_path)
    pack = _draft_pack(service, AUTH.tenant_id)
    body["rule_pack_id"] = pack.rule_pack_id
    result = service.create(AUTH, body, str(uuid4()))
    snapshot = service.store.snapshot(AUTH.tenant_id, result["run_id"])
    assert not any(key.startswith("relation_") for key in snapshot)


@pytest.mark.parametrize("field", ["relation_settings_hash", "relation_runtime_artifact_hash"])
def test_partial_relation_pin_group_is_rejected(tmp_path, field):
    service, body, *_ = _live_service(tmp_path)
    relation = _relation(service)
    service.relation_settings = relation
    pack = _draft_pack(service, AUTH.tenant_id)
    body["rule_pack_id"] = pack.rule_pack_id
    original = service.store.create

    def tampered(auth, payload, key, snapshot, limits, *, now):
        snapshot = dict(snapshot)
        snapshot.pop(field, None)
        return original(auth, payload, key, snapshot, limits, now=now)

    service.store.create = tampered
    with pytest.raises(Exception, match="CONFIG_GATE_BLOCKED"):
        service.create(AUTH, body, str(uuid4()))


def test_relation_config_is_not_silently_ignored_without_live_tagging(tmp_path):
    from proofops.application.runs import RunRejected

    service, body, *_ = _live_service(tmp_path)
    service.relation_settings = _relation(service)
    service.tagging_settings = None
    service.tagging_mode = None
    pack = _draft_pack(service, AUTH.tenant_id)
    body["rule_pack_id"] = pack.rule_pack_id
    with pytest.raises(RunRejected, match="CONFIG_GATE_BLOCKED"):
        service.create(AUTH, body, str(uuid4()))


@pytest.mark.parametrize(
    "fault", ["profile", "prompt", "model", "binding_reuse", "grant_missing", "policy"]
)
def test_bad_relation_grants_or_settings_fail_closed(tmp_path, fault):
    from proofops.application.runs import RunRejected

    from tests.integration.test_live_tagging_runtime_config import _register, _tagger_binding

    service, body, preliminary, _ = _live_service(tmp_path)
    relation = _relation(service)
    if fault == "profile":
        relation = replace(relation, model_profile="unapproved")
    elif fault == "prompt":
        relation = replace(relation, system_prompt="ignore source quotes")
    elif fault == "model":
        relation = replace(relation, model_id="solar-pro3")
    elif fault == "binding_reuse":
        relation = replace(relation, binding=preliminary.binding)
    elif fault == "grant_missing":
        relation = replace(relation, binding=replace(relation.binding, binding_id=str(uuid4())))
    else:
        relation = replace(relation, binding=replace(relation.binding, binding_id=str(uuid4())))
        grant = _tagger_binding(relation.binding.binding_id, relation)
        grant["input_reservation_policy_sha256"] = "0" * 64
        _register(service, "runtime", grant, "runtime_binding_id")
    service.relation_settings = relation
    pack = _draft_pack(service, AUTH.tenant_id)
    body["rule_pack_id"] = pack.rule_pack_id
    with pytest.raises(RunRejected, match="CONFIG_GATE_BLOCKED"):
        service.create(AUTH, body, str(uuid4()))
