"""AT-024: mode isolation with real snapshots and explicitly synthetic approvals.

Synthetic advertising clauses below test selection only, not legal/domain rules.
No repository template is approved or enabled by these tests.
"""

from dataclasses import asdict, replace

import pytest
import yaml
from proofops.application.rulepacks import validate_rulepack
from proofops.domain.rulepacks import pack_content_hash, snapshot_from_validated
from proofops.domain.rules.engine import evaluate

from .test_rules import DOCUMENT, ROOT, TENANT, inputs, pack


def snapshot(mode="disclosure", *, extra=None):
    original = pack()
    data = asdict(original)
    data.update(
        mode=mode,
        status="active",
        approved_by="synthetic-approval-fixture",
        approved_at="2026-09-09T00:00:00Z",
    )
    files = {path: original.file_content(path) for path in original.files}
    if mode == "advertising":
        files = {
            "advertising/synthetic.yaml": {
                "version": data["version"],
                "effective_date": data["effective_date"],
                "source_document_sha256": data["source_document_sha256"],
                "enabled": True,
                "clauses": [{"id": "SYNTHETIC_SELECTION_ONLY", "summary": "Not a legal rule"}],
                "verification_status": "verified",
                "verified_by": "synthetic-approval-fixture",
                "verified_at": "2026-09-09T00:00:00Z",
            }
        }
    files.update(extra or {})
    data["files"] = list(files)
    data["unresolved_gap_ids"] = list(original.unresolved_gap_ids)
    data["sha256"] = pack_content_hash(data, files)
    assert validate_rulepack(data, files, data["unresolved_gap_ids"]).ok
    return snapshot_from_validated(data, files)


def select(mode, rulepack, **kwargs):
    from proofops.application.mode_gate import select_mode_rulepack

    return select_mode_rulepack(mode, rulepack, tenant_id=TENANT, **kwargs)


@pytest.mark.parametrize("mode", ["disclosure", "advertising"])
def test_matching_approved_pack_is_returned_without_rewriting_snapshot(mode):
    original = snapshot(mode)
    result = select(mode, original)
    assert result is original
    assert result.sha256 == original.sha256
    copy = result.file_content(result.files[0])
    copy.clear()
    assert original.file_content(original.files[0])


@pytest.mark.parametrize("mode", ["disclosure", "advertising"])
def test_modes_never_fall_back_to_each_other(mode):
    other = "advertising" if mode == "disclosure" else "disclosure"
    with pytest.raises(ValueError, match="MODE_RULEPACK_MISMATCH"):
        select(mode, snapshot(other))


@pytest.mark.parametrize("mode", ["disclosure", "advertising"])
def test_missing_and_foreign_pack_have_identical_nonleaking_error(mode):
    for candidate in (None, replace(snapshot(mode), tenant_id=DOCUMENT)):
        with pytest.raises(LookupError, match="^rule pack not found$"):
            select(mode, candidate)


@pytest.mark.parametrize("mode", ["disclosure", "advertising"])
@pytest.mark.parametrize(
    "changes",
    [
        {"status": "draft"},
        {"status": "validated"},
        {"status": "retired"},
        {"approved_by": None},
        {"approved_at": None},
        {"approved_by": " "},
        {"approved_at": " "},
    ],
)
def test_unapproved_inactive_or_retired_pack_cannot_start_mode(mode, changes):
    with pytest.raises(ValueError, match="RULEPACK_APPROVAL_REQUIRED"):
        select(mode, replace(snapshot(mode), **changes))


def test_local_disclosure_demo_is_explicit_and_does_not_approve_pack():
    draft = pack()
    with pytest.raises(ValueError, match="RULEPACK_APPROVAL_REQUIRED"):
        select("disclosure", draft)
    assert select("disclosure", draft, local_synthetic=True) is draft
    assert draft.status == "draft" and draft.approved_by is None


def test_synthetic_flag_cannot_enable_advertising_templates_or_retired_packs():
    for mode, candidate in (
        ("advertising", replace(snapshot("advertising"), status="draft", approved_by=None)),
        ("disclosure", replace(pack(), status="retired")),
    ):
        with pytest.raises(ValueError, match="RULEPACK_APPROVAL_REQUIRED"):
            select(mode, candidate, local_synthetic=True)


@pytest.mark.parametrize(
    "path",
    ["advertising/moe_notice.yaml", "advertising/kftc_guideline.yaml", "standards/iso_14021.yaml"],
)
def test_display_advertising_files_cannot_enter_disclosure_label_input(path):
    payload = yaml.safe_load((ROOT / "config" / path).read_text())
    mixed = snapshot(extra={path: payload})
    with pytest.raises(ValueError, match="MODE_RULEPACK_MIXED"):
        select("disclosure", mixed)
    assert mixed.file_content(path) == payload  # Refuse; never silently strip and rehash.


def test_renaming_advertising_payload_does_not_bypass_disclosure_gate():
    payload = yaml.safe_load((ROOT / "config/advertising/moe_notice.yaml").read_text())
    with pytest.raises(ValueError, match="MODE_RULEPACK_MIXED"):
        select("disclosure", snapshot(extra={"rubric/extra.yaml": payload}))
    iso = yaml.safe_load((ROOT / "config/standards/iso_14021.yaml").read_text())
    with pytest.raises(ValueError, match="MODE_RULEPACK_MIXED"):
        select("disclosure", snapshot(extra={"standards/extra.yaml": iso}))


def test_advertising_requires_dedicated_files_and_never_reuses_general_ladders():
    disclosure = pack()
    files = {p: disclosure.file_content(p) for p in disclosure.files}
    with pytest.raises(ValueError, match="MODE_RULEPACK_MIXED"):
        select("advertising", snapshot("advertising", extra=files))


@pytest.mark.parametrize(
    "path",
    ["advertising/moe_notice.yaml", "advertising/kftc_guideline.yaml", "standards/iso_14021.yaml"],
)
def test_active_manifest_cannot_turn_unapproved_empty_templates_into_rules(path):
    payload = yaml.safe_load((ROOT / "config" / path).read_text())
    with pytest.raises(ValueError, match="ADVERTISING_RULEPACK_NOT_READY"):
        select("advertising", snapshot("advertising", extra={path: payload}))


def test_selected_disclosure_runs_real_engine_and_advertising_is_rejected_by_it():
    tags, context = inputs()
    result = evaluate(tags, context, select("disclosure", snapshot()))
    assert result.evidence_grade == "E3" and result.label == "SUBSTANTIATED"
    advertising = select("advertising", snapshot("advertising"))
    with pytest.raises(ValueError, match="dedicated advertising evaluator required"):
        evaluate(tags, replace(context, mode="advertising"), advertising)


@pytest.mark.parametrize("mode", ["", "combined", None, [], True, False])
def test_unknown_modes_fail_closed(mode):
    with pytest.raises(ValueError, match="MODE_NOT_SUPPORTED"):
        select(mode, snapshot())


def test_raw_dict_and_nonboolean_synthetic_flag_are_rejected():
    with pytest.raises(ValueError):
        select("disclosure", asdict(snapshot()))
    with pytest.raises(ValueError):
        select("disclosure", pack(), local_synthetic="true")


def test_empty_advertising_snapshot_is_not_an_approved_ruleset():
    original = snapshot("advertising")
    data = asdict(original)
    data["files"] = []
    data["sha256"] = pack_content_hash(data, {})
    empty = snapshot_from_validated(data, {})
    with pytest.raises(ValueError, match="ADVERTISING_RULEPACK_NOT_READY"):
        select("advertising", empty)


@pytest.mark.parametrize("marker", [{"basis": {"standard": "ISO 14021"}}, {"mode": "advertising"}])
def test_nested_advertising_rules_cannot_hide_in_disclosure_rubric(marker):
    payload = pack().file_content("rubric/goal.yaml")
    payload["branches"][0].update(marker)
    with pytest.raises(ValueError, match="MODE_RULEPACK_MIXED"):
        select("disclosure", snapshot(extra={"rubric/goal.yaml": payload}))


@pytest.mark.parametrize("field", ["verified_by", "verified_at"])
def test_blank_advertising_verification_record_is_not_approval(field):
    original = snapshot("advertising")
    payload = original.file_content("advertising/synthetic.yaml")
    payload[field] = " "
    with pytest.raises(ValueError, match="ADVERTISING_RULEPACK_NOT_READY"):
        select(
            "advertising", snapshot("advertising", extra={"advertising/synthetic.yaml": payload})
        )
