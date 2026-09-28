"""AT-025 · FR-025 규칙팩 검증·활성화 (TASK-025).

Acceptance: 활성화는 새 run 기본값만 바꾸며 실행 중인 규칙 스냅샷은 바꾸지 않는다.
Operation: POST /v1/rule-packs/{rule_pack_id}/activate (role=admin, idempotent).

Prohibited: source-less present, unknown-to-absent, LLM grades, tenant leaks,
overwritten revisions. Local synthetic adapters must be explicitly marked.
"""

from __future__ import annotations

import ast
import copy
from dataclasses import replace
from pathlib import Path

import pytest
from proofops.application.rulepacks import (
    RulePackRecord,
    RulePackRegistry,
    RunSnapshot,
    activate_rulepack,
    compute_pack_sha256,
    grant_demo_use,
    validate_rulepack,
)

TENANT_A = "11111111-1111-4111-8111-111111111111"
TENANT_B = "22222222-2222-4222-8222-222222222222"
PACK_1 = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PACK_2 = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
SRC_SHA = "0f9d7e3495d1bc6330ee4ffd2bb644ce8189c89e91dd5f9ae4c720b9ffaebc6a"

GAP_IDS = [f"GAP-{i:03d}" for i in range(1, 11)]


def test_repository_draft_config_validates_without_approval(tmp_path, monkeypatch, capsys):
    import json
    import runpy

    import yaml

    root = Path(__file__).resolve().parents[2]
    config_dir = root / "config"
    pack = yaml.safe_load((config_dir / "rule_pack_manifest.yaml").read_text())
    files = {path: yaml.safe_load((config_dir / path).read_text()) for path in pack["files"]}
    pack.update(rule_pack_id=PACK_1, tenant_id=TENANT_A, approved_by=None, approved_at=None)
    pack["sha256"] = compute_pack_sha256(pack, files)
    descriptor = tmp_path / "synthetic-local-pack.json"
    descriptor.write_text(json.dumps(pack))
    monkeypatch.setattr(
        "sys.argv",
        [
            "verify_rulepack",
            "--pack",
            str(descriptor),
            "--config-dir",
            str(config_dir),
            "--gaps",
            str(root / "contracts/domain_gaps.json"),
        ],
    )
    entry = runpy.run_path(str(root / "scripts/verify_rulepack.py"))
    assert entry["main"]() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True
    assert report["pack_status"] == "draft"
    assert report["activatable"] is False


def _files_content(**overrides):
    base = {
        "rubric/elements.yaml": {
            "version": "proofops-domain-v2.0-impl1",
            "effective_date": "2026-09-08",
            "elements": [{"id": "G1"}],
        },
        "rubric/goal.yaml": {
            "version": "proofops-domain-v2.0-impl1",
            "effective_date": "2026-09-08",
            "track": "goal",
        },
        "regulatory/safe_harbor.yaml": {
            "version": "proofops-domain-v2.0-impl1",
            "effective_date": "2026-09-08",
            "grade_mapping": None,
            "reasonable_basis_boolean_mapping": None,
        },
        "regulatory/timeline.yaml": {
            "version": "proofops-domain-v2.0-impl1",
            "effective_date": "2026-09-08",
            "automatic_legal_applicability_enabled": False,
        },
        "crosswalk.yaml": {
            "version": "proofops-domain-v2.0-impl1",
            "effective_date": "2026-09-08",
            "verification_status": "unverified",
            "mappings": [],
        },
    }
    base.update(overrides)
    for doc in base.values():
        if isinstance(doc, dict):
            doc.setdefault("source_document_sha256", SRC_SHA)
    return base


def _pack_dict(status="validated", **overrides):
    files = [
        "rubric/elements.yaml",
        "rubric/goal.yaml",
        "regulatory/safe_harbor.yaml",
        "regulatory/timeline.yaml",
        "crosswalk.yaml",
    ]
    pack = {
        "rule_pack_id": PACK_1,
        "tenant_id": TENANT_A,
        "version": "proofops-domain-v2.0-impl1",
        "effective_date": "2026-09-08",
        "mode": "disclosure",
        "status": status,
        "ontology_version": "proofops-elements-1",
        "source_document_sha256": SRC_SHA,
        "files": files,
        "unresolved_gap_ids": list(GAP_IDS),
        "approved_by": "domain-owner",
        "approved_at": "2026-09-08T10:00:00Z",
    }
    pack.update(overrides)
    pack["sha256"] = compute_pack_sha256(pack, _files_content())
    return pack


def test_valid_pack_passes_validation():
    pack = _pack_dict()
    result = validate_rulepack(pack, _files_content(), GAP_IDS)
    assert result.ok is True
    assert list(result.errors) == []
    assert result.computed_sha256 == pack["sha256"]


@pytest.mark.parametrize("field", ["sha256", "files", "unresolved_gap_ids"])
def test_null_required_pack_values_are_rejected(field):
    pack = _pack_dict()
    pack[field] = None
    assert not validate_rulepack(pack, _files_content(), GAP_IDS).ok


def test_snapshot_copies_mutable_content_pairs():
    from dataclasses import replace

    from proofops.domain.rulepacks import snapshot_from_validated

    snap = snapshot_from_validated(_pack_dict(), _files_content())
    pairs = [list(pair) for pair in snap.content]
    frozen = replace(snap, content=pairs)
    path, original = snap.content[0]
    pairs[0][1] = "{}"
    assert frozen.file_content(path) == snap.file_content(path)
    assert frozen.content[0][1] == original

    active = [[TENANT_A, "disclosure", PACK_1]]
    registry = RulePackRegistry(active=active)
    active[0][2] = PACK_2
    assert registry.active_pack_id(TENANT_A, "disclosure") == PACK_1


def test_cli_rejects_source_escape_before_reading(tmp_path, monkeypatch):
    import json
    import runpy
    import sys

    config = tmp_path / "config"
    config.mkdir()
    outside = tmp_path / "outside.yaml"
    outside.write_text("hidden: fixture-only\n", encoding="utf-8")
    pack = _pack_dict()
    pack["files"] = ["../outside.yaml"]
    pack_path = tmp_path / "pack.json"
    pack_path.write_text(json.dumps(pack), encoding="utf-8")
    gaps = tmp_path / "gaps.json"
    gaps.write_text(json.dumps([{"id": gap} for gap in GAP_IDS]), encoding="utf-8")
    reads = []
    read_text = Path.read_text

    def observed_read(path, *args, **kwargs):
        if path.resolve() == outside.resolve():
            reads.append(path)
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", observed_read)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "verify_rulepack",
            "--pack",
            str(pack_path),
            "--config-dir",
            str(config),
            "--gaps",
            str(gaps),
        ],
    )
    entry = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts/verify_rulepack.py"))
    assert entry["main"]() == 1
    assert not reads


def test_missing_version_rejected():
    pack = _pack_dict()
    del pack["version"]
    result = validate_rulepack(pack, _files_content(), GAP_IDS)
    assert result.ok is False
    assert any("version" in e for e in result.errors)


def test_bad_effective_date_rejected():
    pack = _pack_dict(effective_date="next-friday")
    result = validate_rulepack(pack, _files_content(), GAP_IDS)
    assert result.ok is False
    assert any("effective_date" in e for e in result.errors)


def test_sha256_mismatch_rejected():
    pack = _pack_dict()
    pack["sha256"] = "0" * 64
    result = validate_rulepack(pack, _files_content(), GAP_IDS)
    assert result.ok is False
    assert any("sha256" in e for e in result.errors)


def test_unknown_gap_id_rejected():
    pack = _pack_dict(unresolved_gap_ids=["GAP-999"])
    result = validate_rulepack(pack, _files_content(), GAP_IDS)
    assert result.ok is False
    assert any("GAP-999" in e for e in result.errors)


def test_missing_referenced_file_rejected():
    pack = _pack_dict()
    files = _files_content()
    del files["rubric/goal.yaml"]
    result = validate_rulepack(pack, files, GAP_IDS)
    assert result.ok is False
    assert any("rubric/goal.yaml" in e for e in result.errors)


def test_file_missing_version_rejected():
    pack = _pack_dict()
    files = _files_content()
    files["rubric/goal.yaml"] = {"track": "goal"}
    result = validate_rulepack(pack, files, GAP_IDS)
    assert result.ok is False
    assert any("version" in e for e in result.errors)


def test_unverified_clause_claimed_verified_rejected():
    # GAP-008: 번호 생성/verified 사칭 금지. clause가 있는데 검증 완료 표시는 거부.
    files = _files_content()
    files["rubric/elements.yaml"] = {
        "version": "proofops-domain-v2.0-impl1",
        "effective_date": "2026-09-08",
        "elements": [
            {
                "id": "G1",
                "basis": {"clause": "S2-99", "verification_status": "verified"},
            }
        ],
    }
    pack = _pack_dict()
    pack["sha256"] = compute_pack_sha256(pack, files)
    result = validate_rulepack(pack, files, GAP_IDS)
    assert result.ok is False
    assert any("verification" in e for e in result.errors)


def test_safe_harbor_grade_mapping_without_approval_rejected():
    # GAP-001: 승인 매핑 없으면 grade_mapping은 null이어야 한다.
    files = _files_content()
    files["regulatory/safe_harbor.yaml"] = {
        "version": "proofops-domain-v2.0-impl1",
        "effective_date": "2026-09-08",
        "grade_mapping": {"emissions_estimate": "E2"},
        "reasonable_basis_boolean_mapping": None,
    }
    pack = _pack_dict()
    pack["sha256"] = compute_pack_sha256(pack, files)
    result = validate_rulepack(pack, files, GAP_IDS)
    assert result.ok is False
    assert any("grade_mapping" in e for e in result.errors)


def test_automatic_legal_applicability_rejected():
    # GAP-009: 자동 법적적용 true는 거부.
    files = _files_content()
    files["regulatory/timeline.yaml"] = {
        "version": "proofops-domain-v2.0-impl1",
        "effective_date": "2026-09-08",
        "automatic_legal_applicability_enabled": True,
    }
    pack = _pack_dict()
    pack["sha256"] = compute_pack_sha256(pack, files)
    result = validate_rulepack(pack, files, GAP_IDS)
    assert result.ok is False
    assert any("legal" in e for e in result.errors)


def test_draft_pack_cannot_activate():
    registry = RulePackRegistry.empty()
    draft = RulePackRecord.from_dict(_pack_dict(status="draft"))
    registry = registry.with_pack(draft)
    with pytest.raises(ValueError, match="validated"):
        activate_rulepack(
            registry,
            PACK_1,
            TENANT_A,
            actor="admin-operator",
            reason="promote draft",
            files_content=_files_content(),
            gap_ids=GAP_IDS,
        )


def test_activation_without_approver_rejected():
    registry = RulePackRegistry.empty()
    pack = RulePackRecord.from_dict(_pack_dict(approved_by=None, approved_at=None))
    registry = registry.with_pack(pack)
    with pytest.raises(ValueError, match="approved_by"):
        activate_rulepack(
            registry,
            PACK_1,
            TENANT_A,
            actor="admin-operator",
            reason="no approver",
            files_content=_files_content(),
            gap_ids=GAP_IDS,
        )


def test_cross_tenant_activation_rejected_without_leak():
    # 테넌트 격리: B의 팩을 A가 활성화하면 존재 여부를 숨기는 not-found로 거부.
    registry = RulePackRegistry.empty()
    other = RulePackRecord.from_dict(_pack_dict())
    other_b = RulePackRecord(
        rule_pack_id=other.rule_pack_id,
        tenant_id=TENANT_B,
        version=other.version,
        effective_date=other.effective_date,
        mode=other.mode,
        status=other.status,
        ontology_version=other.ontology_version,
        source_document_sha256=other.source_document_sha256,
        files=other.files,
        sha256=other.sha256,
        unresolved_gap_ids=other.unresolved_gap_ids,
        approved_by=other.approved_by,
        approved_at=other.approved_at,
    )
    registry = registry.with_pack(other_b)
    with pytest.raises(LookupError, match="not found"):
        activate_rulepack(
            registry,
            PACK_1,
            TENANT_A,
            actor="admin-operator",
            reason="cross tenant",
            files_content=_files_content(),
            gap_ids=GAP_IDS,
        )
    # A의 목록에는 B의 팩이 보이지 않는다.
    assert registry.packs_for_tenant(TENANT_A) == []


def test_activation_only_changes_new_run_default():
    """핵심 수용 기준: 실행 중 run의 규칙 스냅샷은 불변, 새 run 기본값만 변경."""
    old_files = _files_content()
    old_pack = RulePackRecord.from_dict(_pack_dict())
    registry = RulePackRegistry.empty().with_pack(old_pack)
    registry, _ = activate_rulepack(
        registry,
        PACK_1,
        TENANT_A,
        actor="admin-operator",
        reason="go live v1",
        files_content=old_files,
        gap_ids=GAP_IDS,
    )
    running = RunSnapshot(
        run_id="33333333-3333-4333-8333-333333333333",
        tenant_id=TENANT_A,
        rule_pack_id=PACK_1,
        rule_pack_sha256=old_pack.sha256,
        status="running",
    )
    registry = registry.with_run(running)
    before_runs = copy.deepcopy(registry.runs_for_tenant(TENANT_A))

    new_pack_dict = _pack_dict(status="validated")
    new_pack_dict["rule_pack_id"] = PACK_2
    new_files = _files_content()
    new_files["rubric/goal.yaml"] = {
        "version": "proofops-domain-v2.0-impl1",
        "effective_date": "2026-09-09",
        "track": "goal",
    }
    new_pack_dict["sha256"] = compute_pack_sha256(new_pack_dict, new_files)
    new_pack = RulePackRecord.from_dict(new_pack_dict)
    registry = registry.with_pack(new_pack)
    registry, record = activate_rulepack(
        registry,
        PACK_2,
        TENANT_A,
        actor="admin-operator",
        reason="go live v2",
        files_content=new_files,
        gap_ids=GAP_IDS,
    )

    # 실행 중 run 스냅샷 불변.
    assert registry.runs_for_tenant(TENANT_A) == before_runs
    assert registry.runs_for_tenant(TENANT_A)[0].rule_pack_sha256 == old_pack.sha256
    assert registry.runs_for_tenant(TENANT_A)[0].rule_pack_id == PACK_1
    # 새 run 기본값은 새 팩.
    assert registry.active_pack_id(TENANT_A, "disclosure") == PACK_2
    assert record.before_pack_id == PACK_1
    assert record.after_pack_id == PACK_2
    # 기존 팩 revision 덮어쓰기 금지:旧 팩은 retired로 보존, 삭제되지 않음.
    assert registry.get_pack(TENANT_A, PACK_1).status == "retired"
    assert registry.get_pack(TENANT_A, PACK_2).status == "active"


def test_reactivate_same_pack_is_idempotent():
    registry = RulePackRegistry.empty().with_pack(RulePackRecord.from_dict(_pack_dict()))
    registry, first = activate_rulepack(
        registry,
        PACK_1,
        TENANT_A,
        actor="admin-operator",
        reason="go live",
        files_content=_files_content(),
        gap_ids=GAP_IDS,
    )
    registry, second = activate_rulepack(
        registry,
        PACK_1,
        TENANT_A,
        actor="admin-operator",
        reason="go live",
        files_content=_files_content(),
        gap_ids=GAP_IDS,
    )
    assert first.after_pack_id == second.after_pack_id == PACK_1
    assert registry.active_pack_id(TENANT_A, "disclosure") == PACK_1


def test_retired_pack_cannot_activate():
    registry = RulePackRegistry.empty().with_pack(
        RulePackRecord.from_dict(_pack_dict(status="retired"))
    )
    with pytest.raises(ValueError, match="[Rr]etired"):
        activate_rulepack(
            registry,
            PACK_1,
            TENANT_A,
            actor="admin-operator",
            reason="revive retired",
            files_content=_files_content(),
            gap_ids=GAP_IDS,
        )


# --- Acceptance repair: registry identity keyed by tenant+ID ---


def test_same_pack_id_coexists_across_tenants():
    pack_a = RulePackRecord.from_dict(_pack_dict())
    pack_b_dict = _pack_dict()
    pack_b_dict["tenant_id"] = TENANT_B
    pack_b = RulePackRecord.from_dict(pack_b_dict)
    registry = RulePackRegistry.empty().with_pack(pack_a).with_pack(pack_b)
    assert registry.get_pack(TENANT_A, PACK_1) == pack_a
    assert registry.get_pack(TENANT_B, PACK_1) == pack_b
    assert [p.tenant_id for p in registry.packs_for_tenant(TENANT_A)] == [TENANT_A]


def test_identical_replay_is_idempotent():
    registry = RulePackRegistry.empty().with_pack(RulePackRecord.from_dict(_pack_dict()))
    assert registry.with_pack(RulePackRecord.from_dict(_pack_dict())) is registry


def test_changed_same_id_rejected():
    registry = RulePackRegistry.empty().with_pack(RulePackRecord.from_dict(_pack_dict()))
    changed = _pack_dict()
    changed["version"] = "proofops-domain-v2.0-impl2"
    changed["sha256"] = compute_pack_sha256(changed, _files_content())
    with pytest.raises(ValueError, match="immutable"):
        registry.with_pack(RulePackRecord.from_dict(changed))


def test_registry_freezes_nested_inputs():
    packs = [RulePackRecord.from_dict(_pack_dict())]
    registry = RulePackRegistry(packs=packs, runs=[], active=[])
    packs.clear()
    assert len(registry.packs) == 1
    assert registry.runs == ()
    assert registry.active == ()


# --- Acceptance repair: hash binds the semantic manifest ---


def test_hash_binds_manifest_version():
    pack = _pack_dict()
    files = _files_content()
    altered = dict(pack)
    altered["version"] = "proofops-domain-v2.0-impl2"
    assert compute_pack_sha256(altered, files) != pack["sha256"]
    result = validate_rulepack(altered, files, GAP_IDS)
    assert result.ok is False
    assert any("sha256" in e for e in result.errors)


def test_extra_file_beyond_manifest_rejected():
    pack = _pack_dict()
    files = _files_content()
    files["rubric/stowaway.yaml"] = {
        "version": "proofops-domain-v2.0-impl1",
        "effective_date": "2026-09-08",
    }
    result = validate_rulepack(pack, files, GAP_IDS)
    assert result.ok is False
    assert any("stowaway" in e for e in result.errors)


def test_duplicate_file_entry_rejected():
    pack = _pack_dict()
    pack["files"] = [*pack["files"], "rubric/goal.yaml"]
    pack["sha256"] = compute_pack_sha256(pack, _files_content())
    result = validate_rulepack(pack, _files_content(), GAP_IDS)
    assert result.ok is False
    assert any("duplicate" in e for e in result.errors)


def test_unsafe_path_rejected():
    pack = _pack_dict()
    pack["files"] = ["../escape.yaml"]
    files = {
        "../escape.yaml": {
            "version": "proofops-domain-v2.0-impl1",
            "effective_date": "2026-09-08",
        }
    }
    pack["sha256"] = compute_pack_sha256(pack, files)
    result = validate_rulepack(pack, files, GAP_IDS)
    assert result.ok is False
    assert any("unsafe" in e for e in result.errors)


def test_file_version_mismatch_rejected():
    files = _files_content()
    files["rubric/goal.yaml"] = {
        **files["rubric/goal.yaml"],
        "version": "something-else",
    }
    pack = _pack_dict()
    pack["sha256"] = compute_pack_sha256(pack, files)
    result = validate_rulepack(pack, files, GAP_IDS)
    assert result.ok is False
    assert any("version" in e and "rubric/goal.yaml" in e for e in result.errors)


def test_source_identity_mismatch_rejected():
    files = _files_content()
    files["rubric/goal.yaml"] = {
        **files["rubric/goal.yaml"],
        "source_document_sha256": "1" * 64,
    }
    pack = _pack_dict()
    pack["sha256"] = compute_pack_sha256(pack, files)
    result = validate_rulepack(pack, files, GAP_IDS)
    assert result.ok is False
    assert any("source" in e and "rubric/goal.yaml" in e for e in result.errors)


def test_nan_payload_rejected_without_crash():
    files = _files_content()
    files["rubric/goal.yaml"] = {
        "version": "proofops-domain-v2.0-impl1",
        "effective_date": "2026-09-08",
        "threshold": float("nan"),
    }
    result = validate_rulepack(_pack_dict(), files, GAP_IDS)
    assert result.ok is False
    assert any("malformed" in e for e in result.errors)


# --- Acceptance repair: immutable pure-domain snapshot for TASK-014 ---


def test_domain_snapshot_retains_verified_content():
    from proofops.domain.rulepacks import snapshot_from_validated

    pack = _pack_dict()
    files = _files_content()
    assert validate_rulepack(pack, files, GAP_IDS).ok is True
    snap = snapshot_from_validated(pack, files)
    assert snap.sha256 == pack["sha256"]
    assert snap.file_content("rubric/goal.yaml")["track"] == "goal"
    files["rubric/goal.yaml"]["track"] = "mutated"
    pack["version"] = "mutated"
    assert snap.file_content("rubric/goal.yaml")["track"] == "goal"
    assert snap.version == "proofops-domain-v2.0-impl1"
    snap.file_content("rubric/goal.yaml")["track"] = "mutated"
    assert snap.file_content("rubric/goal.yaml")["track"] == "goal"


def test_domain_snapshot_rejects_tampered_sha():
    from proofops.domain.errors import DomainValidationError
    from proofops.domain.rulepacks import snapshot_from_validated

    pack = _pack_dict()
    pack["sha256"] = "0" * 64
    with pytest.raises(DomainValidationError):
        snapshot_from_validated(pack, _files_content())


def test_domain_snapshot_module_is_application_free():
    root = Path(__file__).resolve().parents[2]
    src = (root / "packages" / "proofops" / "domain" / "rulepacks.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not any(name.name.startswith("proofops.application") for name in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[:2] != ["proofops", "application"]


# --- Acceptance repair: local validated-draft demo without invented approval ---


def test_demo_grant_for_draft_without_invented_approval():
    pack = _pack_dict(status="draft", approved_by=None, approved_at=None)
    grant = grant_demo_use(
        pack,
        _files_content(),
        GAP_IDS,
        actor="local-demo",
        purpose="explicit ladder demo",
    )
    assert grant.rule_pack_id == PACK_1
    assert grant.local_only is True
    assert grant.approved_by is None
    assert any("legal" in limit for limit in grant.limits)


def test_demo_grant_does_not_activate_and_draft_still_blocked():
    registry = RulePackRegistry.empty().with_pack(
        RulePackRecord.from_dict(_pack_dict(status="draft", approved_by=None))
    )
    grant_demo_use(
        _pack_dict(status="draft", approved_by=None),
        _files_content(),
        GAP_IDS,
        actor="local-demo",
        purpose="explicit ladder demo",
    )
    assert registry.active_pack_id(TENANT_A, "disclosure") is None
    with pytest.raises(ValueError, match="validated"):
        activate_rulepack(
            registry,
            PACK_1,
            TENANT_A,
            actor="admin-operator",
            reason="draft must not activate as customer default",
            files_content=_files_content(),
            gap_ids=GAP_IDS,
        )


# --- AT-R07: audited AI-delegated project review activation path ---


def test_ai_delegated_review_activates_validated_pack_for_real_runs():
    """Positive: an unapproved, validated pack becomes the real (tenant, mode)
    default through the AI-delegated review path, distinguishably from a
    human approver, and existing running-run snapshots stay untouched."""
    from proofops.application.rulepacks import record_ai_delegated_review

    registry = RulePackRegistry.empty().with_pack(
        RulePackRecord.from_dict(_pack_dict(approved_by=None, approved_at=None))
    )
    registry, record, provenance = record_ai_delegated_review(
        registry,
        PACK_1,
        TENANT_A,
        reviewer="review-operator",
        reviewed_at="2026-09-20T10:00:00Z",
        source_authority="user delegation 2026-09-20",
        note="R07 AI-delegated project review of explicit ladder",
        files_content=_files_content(),
        gap_ids=GAP_IDS,
    )

    activated = registry.get_pack(TENANT_A, PACK_1)
    assert activated.status == "active"
    assert registry.active_pack_id(TENANT_A, "disclosure") == PACK_1
    assert activated.approved_by == "ai-delegated-review:review-operator"
    assert activated.approved_by != "review-operator"  # never bare/human-looking
    assert provenance.review_origin == "ai_project_interpretation"
    assert provenance.pack_sha256 == activated.sha256
    assert provenance.source_authority == "user delegation 2026-09-20"
    assert record.after_pack_id == PACK_1


def test_ai_delegated_review_rejects_empty_reviewer_no_self_approval():
    """Negative: no reviewer identity supplied => rejected outright, not
    silently defaulted to some ambient/self identity."""
    from proofops.application.rulepacks import record_ai_delegated_review

    registry = RulePackRegistry.empty().with_pack(
        RulePackRecord.from_dict(_pack_dict(approved_by=None, approved_at=None))
    )
    with pytest.raises(ValueError, match="reviewer"):
        record_ai_delegated_review(
            registry,
            PACK_1,
            TENANT_A,
            reviewer="",
            reviewed_at="2026-09-20T10:00:00Z",
            source_authority="user delegation 2026-09-20",
            note="missing reviewer",
            files_content=_files_content(),
            gap_ids=GAP_IDS,
        )


def test_ai_delegated_review_never_overwrites_existing_human_approval():
    """Negative: a pack that already carries a human approved_by is refused,
    never silently re-stamped with an AI-delegated reviewer."""
    from proofops.application.rulepacks import record_ai_delegated_review

    registry = RulePackRegistry.empty().with_pack(RulePackRecord.from_dict(_pack_dict()))
    with pytest.raises(ValueError, match="already has a human approved_by"):
        record_ai_delegated_review(
            registry,
            PACK_1,
            TENANT_A,
            reviewer="review-operator",
            reviewed_at="2026-09-20T10:00:00Z",
            source_authority="user delegation 2026-09-20",
            note="attempted override",
            files_content=_files_content(),
            gap_ids=GAP_IDS,
        )


def test_ai_delegated_review_rejects_cross_tenant_pack_without_leak():
    """Negative: reviewing tenant B's pack id from tenant A's call still gets
    the uniform not-found, matching human activation's tenant isolation."""
    from proofops.application.rulepacks import record_ai_delegated_review

    other = RulePackRecord.from_dict(_pack_dict(approved_by=None, approved_at=None))
    other_b = replace(other, tenant_id=TENANT_B)
    registry = RulePackRegistry.empty().with_pack(other_b)
    with pytest.raises(LookupError, match="not found"):
        record_ai_delegated_review(
            registry,
            PACK_1,
            TENANT_A,
            reviewer="review-operator",
            reviewed_at="2026-09-20T10:00:00Z",
            source_authority="user delegation 2026-09-20",
            note="cross tenant attempt",
            files_content=_files_content(),
            gap_ids=GAP_IDS,
        )


def test_ai_delegated_review_reason_provenance_round_trips_and_legacy_reason_is_unknown():
    """The structured provenance is recoverable from the activation reason
    (proving it flows through the same field human activation already uses),
    while an ordinary human activation reason parses to None (unknown
    reviewer), never auto-upgraded to reviewed."""
    from proofops.application.rulepacks import ReviewProvenance, record_ai_delegated_review

    registry = RulePackRegistry.empty().with_pack(
        RulePackRecord.from_dict(_pack_dict(approved_by=None, approved_at=None))
    )
    _, record, provenance = record_ai_delegated_review(
        registry,
        PACK_1,
        TENANT_A,
        reviewer="review-operator",
        reviewed_at="2026-09-20T10:00:00Z",
        source_authority="user delegation 2026-09-20",
        note="round trip check",
        files_content=_files_content(),
        gap_ids=GAP_IDS,
    )
    parsed = ReviewProvenance.from_reason(record.reason)
    assert parsed == provenance
    assert ReviewProvenance.from_reason("go live v1") is None


def test_ai_delegated_review_rejects_blank_and_naive_datetime_fields():
    """Negative: whitespace-only strings are treated as blank (not merely
    falsy-checked), and a reviewed_at without timezone info is rejected --
    a naive local time is not an audit-grade timestamp."""
    from proofops.application.rulepacks import record_ai_delegated_review

    registry = RulePackRegistry.empty().with_pack(
        RulePackRecord.from_dict(_pack_dict(approved_by=None, approved_at=None))
    )
    with pytest.raises(ValueError, match="reviewer"):
        record_ai_delegated_review(
            registry,
            PACK_1,
            TENANT_A,
            reviewer="   ",
            reviewed_at="2026-09-20T10:00:00Z",
            source_authority="user delegation 2026-09-20",
            note="whitespace-only reviewer",
            files_content=_files_content(),
            gap_ids=GAP_IDS,
        )
    with pytest.raises(ValueError, match="reviewed_at"):
        record_ai_delegated_review(
            registry,
            PACK_1,
            TENANT_A,
            reviewer="review-operator",
            reviewed_at="2026-09-20T10:00:00",  # naive, no offset/Z
            source_authority="user delegation 2026-09-20",
            note="naive timestamp",
            files_content=_files_content(),
            gap_ids=GAP_IDS,
        )
    with pytest.raises(ValueError, match="reviewed_at"):
        record_ai_delegated_review(
            registry,
            PACK_1,
            TENANT_A,
            reviewer="review-operator",
            reviewed_at="not-a-date",
            source_authority="user delegation 2026-09-20",
            note="garbage timestamp",
            files_content=_files_content(),
            gap_ids=GAP_IDS,
        )


def test_review_provenance_from_reason_rejects_malformed_without_coercion():
    """from_reason must not str()-coerce a malformed payload into something
    that looks valid: wrong-typed fields, an unknown review_origin, and a
    non-sha256 pack_sha256 must all parse to None, not a fabricated record."""
    import json as _json

    from proofops.application.rulepacks import ReviewProvenance

    base = {
        "review_origin": "ai_project_interpretation",
        "reviewer": "review-operator",
        "reviewed_at": "2026-09-20T10:00:00Z",
        "source_authority": "user delegation 2026-09-20",
        "pack_sha256": "a" * 64,
        "note": "ok",
    }

    def reason_for(**overrides):
        data = {**base, **overrides}
        return "review_provenance:" + _json.dumps(data)

    # A non-string field (e.g. reviewer as an int) must not be str()-coerced into "123".
    assert ReviewProvenance.from_reason(reason_for(reviewer=123)) is None
    # An unknown/forged review_origin must not be accepted as this module's known origin.
    assert ReviewProvenance.from_reason(reason_for(review_origin="human_expert_verified")) is None
    # A pack_sha256 that is not a real sha256 hex string must not be accepted.
    assert ReviewProvenance.from_reason(reason_for(pack_sha256="not-a-hash")) is None
    # A naive reviewed_at embedded in an otherwise well-formed payload is still rejected.
    assert ReviewProvenance.from_reason(reason_for(reviewed_at="2026-09-20T10:00:00")) is None
    # The well-formed baseline parses successfully (control case).
    assert ReviewProvenance.from_reason(reason_for()) == ReviewProvenance(**base)
