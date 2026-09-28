"""R07b synthetic checklist interpretation; no PDF/model accuracy claim."""

import json
from copy import deepcopy
from dataclasses import asdict

import pytest
from proofops.application.rulepacks import RulePackRecord, validate_rulepack
from proofops.domain.rulepacks import pack_content_hash, snapshot_from_validated
from proofops.domain.rules.engine import evaluate
from proofops.domain.rules.safe_harbor import record_safe_harbor

from .test_rules import fact, pack
from .test_safe_harbor import _inputs

POLICY = "project_checklist_completeness_v1"
GAPS = [f"GAP-{i:03}" for i in range(1, 11)]


def derived():
    from scripts.review_rulepack import derive_checklist_policy_pack

    original = pack()
    files = {path: original.file_content(path) for path in original.files}
    record = RulePackRecord.from_dict(asdict(original))
    new, changed = derive_checklist_policy_pack(
        record,
        files,
        version=record.version + "-checklist-v1",
        reviewer="coordinator(AI-delegated)",
        reviewed_at="2026-09-20T15:00:00Z",
        source_authority="user delegation 2026-09-20",
        note="R07b bounded adoption",
    )
    assert record.sha256 == original.sha256
    assert files == {path: original.file_content(path) for path in original.files}
    return record, new, changed


@pytest.mark.parametrize(
    "states,expected",
    [
        ({"assumptions": "present", "scenario_or_premises": "present"}, True),
        ({"assumptions": "absent", "scenario_or_premises": "present"}, False),
        ({"assumptions": "absent", "scenario_or_premises": "unknown"}, False),
        ({"assumptions": "absent", "scenario_or_premises": "conflict"}, False),
        ({"assumptions": "unknown", "scenario_or_premises": "present"}, None),
        ({"assumptions": "conflict", "scenario_or_premises": "present"}, None),
        ({"assumptions": "not_applicable", "scenario_or_premises": "present"}, None),
        ({"assumptions": "present"}, None),
        ({}, None),
    ],
)
def test_pinned_policy_boundaries_and_legacy_output(states, expected):
    _, new, files = derived()
    tags, context = _inputs("forward_looking", **states)
    snapshot = snapshot_from_validated(new.to_dict(), files)
    record = record_safe_harbor(tags, context, snapshot)
    assert record.reasonable_basis_documented is expected
    assert record.mapping_status == "unresolved"
    assert record.legal_effect == "not_determined"
    assert record.gap_ids == ("GAP-001",)
    before = record_safe_harbor(tags, context, pack()).to_api_dict()
    assert before["reasonable_basis_documented"] is None
    assert before | {"reasonable_basis_documented": expected} == record.to_api_dict()
    old_decision, decision = evaluate(tags, context, pack()), evaluate(tags, context, snapshot)
    assert decision.evidence_grade is old_decision.evidence_grade is None
    assert decision.label is old_decision.label is None


def test_raw_absence_and_unreadable_are_not_accepted_facts():
    with pytest.raises(ValueError, match="verified search coverage"):
        fact("assumptions", "absent", search_coverage_verified=False)
    # Unreadable evidence must use the existing unknown/conflict state contract.
    with pytest.raises(ValueError, match="invalid confirmed fact"):
        fact("assumptions", "unreadable")


def test_policy_validation_and_empty_checklist():
    old, new, files = derived()
    assert new.rule_pack_id != old.rule_pack_id and new.sha256 != old.sha256
    assert validate_rulepack(new.to_dict(), files, GAPS).ok
    config = files["regulatory/safe_harbor.yaml"]
    for mapping in ("future_policy", {}, False, 1):
        config["reasonable_basis_boolean_mapping"] = mapping
        manifest = new.to_dict() | {"sha256": pack_content_hash(new.to_dict(), files)}
        assert not validate_rulepack(manifest, files, GAPS).ok
        tags, context = _inputs("forward_looking")
        with pytest.raises(ValueError, match="mapping"):
            record_safe_harbor(tags, context, snapshot_from_validated(manifest, files))
    config["reasonable_basis_boolean_mapping"] = POLICY
    config["category_checklists"]["forward_looking"] = []
    manifest = new.to_dict() | {"sha256": pack_content_hash(new.to_dict(), files)}
    tags, context = _inputs("forward_looking")
    assert (
        record_safe_harbor(
            tags, context, snapshot_from_validated(manifest, files)
        ).reasonable_basis_documented
        is None
    )


def test_policy_keeps_provenance_grade_legal_and_present_gates():
    _, new, files = derived()
    for key, value in (
        ("checklist_policy_review", None),
        ("grade_mapping", {"all_present": "E3"}),
        ("legal_effect", "protected"),
    ):
        changed = deepcopy(files)
        changed["regulatory/safe_harbor.yaml"][key] = value
        data = new.to_dict() | {"sha256": pack_content_hash(new.to_dict(), changed)}
        assert not validate_rulepack(data, changed, GAPS).ok
    data = new.to_dict() | {"unresolved_gap_ids": ["GAP-008", "GAP-009"]}
    data["sha256"] = pack_content_hash(data, files)
    assert not validate_rulepack(data, files, GAPS).ok
    snapshot = snapshot_from_validated(new.to_dict(), files)
    for category, names in files["regulatory/safe_harbor.yaml"]["category_checklists"].items():
        tags, context = _inputs(category, **dict.fromkeys(names, "present"))
        assert record_safe_harbor(tags, context, snapshot).reasonable_basis_documented is True
    from dataclasses import replace

    from .test_rules import SOURCE

    bad = fact("assumptions", evidence_refs=(replace(SOURCE, verification_state="candidate"),))
    tags, context = _inputs("forward_looking")
    with pytest.raises(ValueError, match="source"):
        record_safe_harbor(replace(tags, facts=tags.facts + (bad,)), context, snapshot)


def test_opt_in_cli_creates_new_pack_without_changing_source(tmp_path, capsys):
    from proofops.adapters.local.rulepack_store import RulePackSqliteStore

    from scripts.review_rulepack import main

    original = pack()
    record = RulePackRecord.from_dict(asdict(original))
    files = {path: original.file_content(path) for path in original.files}
    db = tmp_path / "rules.sqlite3"
    store = RulePackSqliteStore(db)
    store.add_pack(record, files)
    args = [
        "--state-db",
        str(db),
        "--tenant-id",
        record.tenant_id,
        "--rule-pack-id",
        record.rule_pack_id,
        "--reviewer",
        "coordinator",
        "--reviewed-at",
        "2026-09-20T15:00:00Z",
        "--note",
        "R07b adoption",
        "--checklist-policy",
        POLICY,
        "--derived-version",
        "r07b-checklist-v1",
    ]
    assert main(args) == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["applied"] is False and not store.list_active_packs(record.tenant_id)
    assert main(args + ["--apply"]) == 0
    result = json.loads(capsys.readouterr().out)
    active = store.list_active_packs(record.tenant_id)[0]
    assert active.rule_pack_id != record.rule_pack_id
    assert active.sha256 == result["new_validated_sha256"]
    assert store.get_pack_with_files(record.tenant_id, record.rule_pack_id) == (record, files)
    _, changed = store.get_pack_with_files(record.tenant_id, active.rule_pack_id)
    review = changed["regulatory/safe_harbor.yaml"]["checklist_policy_review"]
    assert review["before_sha256"] == record.sha256
    assert review["source_section"] == "4.6"
    assert review["review_origin"] == "ai_project_interpretation"
    assert review["policy_identifier"] == POLICY
    assert review["boundary_vectors"]
