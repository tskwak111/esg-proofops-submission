"""Select a tenant's pinned rules without mixing disclosure and advertising.

Accept server-resolved, validated snapshots, never client approval metadata.
Selection does not activate a pack, grant rights/legal approval or evaluate ads.
Advertising reference material belongs in a separate recommendation path; it
must not be added to the disclosure grading snapshot, even while disabled.
"""

from __future__ import annotations

from typing import Any

from proofops.application.rulepacks import DEMO_STATUSES, MODES
from proofops.domain.errors import DomainValidationError
from proofops.domain.rulepacks import RulePackSnapshot


def _advertising_marker(node: Any) -> bool:
    """Inspect explicit policy metadata, never infer legal meaning from prose."""
    if isinstance(node, dict):
        return (
            node.get("standard") == "ISO 14021"
            or node.get("standard_id") == "ISO 14021"
            or node.get("mode") == "advertising"
            or node.get("gate") == "separate_approved_advertising_rulepack_required"
            or any(_advertising_marker(value) for value in node.values())
        )
    return isinstance(node, list) and any(_advertising_marker(value) for value in node)


def select_mode_rulepack(
    mode: str,
    rulepack: RulePackSnapshot | None,
    *,
    tenant_id: str,
    local_synthetic: bool = False,
) -> RulePackSnapshot:
    """Return the original snapshot or a fail-closed gate error for a new run."""
    if mode not in MODES:
        raise DomainValidationError("MODE_NOT_SUPPORTED")
    if type(local_synthetic) is not bool:
        raise DomainValidationError("local_synthetic must be boolean")
    if rulepack is not None and not isinstance(rulepack, RulePackSnapshot):
        raise DomainValidationError("expected a validated RulePackSnapshot")
    if rulepack is None or rulepack.tenant_id != tenant_id:
        raise LookupError("rule pack not found")
    if rulepack.mode != mode:
        raise DomainValidationError("MODE_RULEPACK_MISMATCH")
    approved = rulepack.status == "active" and all(
        isinstance(value, str) and value.strip()
        for value in (rulepack.approved_by, rulepack.approved_at)
    )
    demo = mode == "disclosure" and local_synthetic and rulepack.status in DEMO_STATUSES
    if not (approved or demo):
        raise DomainValidationError("RULEPACK_APPROVAL_REQUIRED")

    if not rulepack.files:
        raise DomainValidationError(
            "ADVERTISING_RULEPACK_NOT_READY" if mode == "advertising" else "RULEPACK_NOT_READY"
        )
    for path in rulepack.files:
        payload = rulepack.file_content(path)
        dedicated = path.startswith("advertising/") or path == "standards/iso_14021.yaml"
        if (mode == "advertising" and not dedicated) or (
            mode == "disclosure" and (dedicated or _advertising_marker(payload))
        ):
            raise DomainValidationError("MODE_RULEPACK_MIXED")
        if mode == "advertising" and (
            payload.get("enabled") is False
            or not payload.get("clauses")
            or payload.get("verification_status") != "verified"
            or not all(
                isinstance(payload.get(field), str) and payload[field].strip()
                for field in ("verified_by", "verified_at")
            )
            or payload.get("license_status") == "review_required"
        ):
            raise DomainValidationError("ADVERTISING_RULEPACK_NOT_READY")
    return rulepack
