"""Evaluate trusted, tenant-scoped approval snapshots without I/O.

Inputs are server-resolved ApprovedProfile artifacts, never request-body
assertions of approval. Account permission/routing/schema/image evidence must
be captured by the approved deployment process; this function does not claim
to discover AWS capabilities. Each dispatch evaluates its frozen run snapshots.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from proofops.application.authorization import AuthContext, TenantNotFoundError
from proofops.application.supply_chain import SupplyChainResult
from proofops.domain.provenance import canonical_hash
from proofops.domain.rulepacks import canonical_json


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    reason: str


@dataclass(frozen=True)
class Preflight:
    ready: bool
    checks: tuple[Check, ...]
    binding_sha256: str | None
    checked_at: str

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "checks": [asdict(check) for check in self.checks]}


class PreflightBlocked(ValueError):
    """A sanitized failure at the external-transmission boundary."""


class ModelInvocationPort(Protocol):
    def invoke(
        self,
        *,
        body: bytes,
        auth: AuthContext,
        binding: Mapping[str, Any],
        consent: Mapping[str, Any],
        allowed_regions: Sequence[str],
        document_rights: str,
        checked_at: str,
    ) -> dict[str, Any]: ...


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value == value.strip()


def _uuid(value: Any) -> bool:
    try:
        return isinstance(value, str) and str(UUID(value)) == value.lower()
    except ValueError:
        return False


def _model_target(value: Any, region: Any, account: Any, *, profile_only: bool = False) -> bool:
    if not _text(value) or not _text(region) or not _text(account):
        return False
    if not value.startswith("arn:"):
        return not profile_only and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,2047}", value))
    # P0 supports foundation models and explicit inference profiles only.
    # Other Bedrock resource kinds fail closed until their routing is reviewed.
    match = re.fullmatch(
        r"arn:aws:bedrock:([a-z0-9-]+):([0-9]{12}|):"
        r"(foundation-model|inference-profile|application-inference-profile)/"
        r"([A-Za-z0-9][A-Za-z0-9._:-]*)",
        value,
    )
    if match is None or match[1] != region:
        return False
    if match[3] == "foundation-model":
        return not profile_only and match[2] == ""
    return match[2] == account


def _timestamp(value: Any) -> datetime | None:
    if not _text(value):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else None
    except ValueError:
        return None


def _regions(value: Any) -> set[str]:
    if not isinstance(value, list | tuple) or not value:
        return set()
    if any(
        not isinstance(r, str) or not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d+", r) for r in value
    ):
        return set()
    return set(value)


def check_runtime_binding(
    *,
    binding: Mapping[str, Any],
    consent: Mapping[str, Any],
    auth: AuthContext,
    allowed_regions: Sequence[str],
    checked_at: str,
    include_live_model_probe: bool = False,
) -> Preflight:
    """Check an approved model role against consent and server region policy.

    `ready` means supplied approval evidence passes, not a new live AWS test.
    A requested new live probe remains not_run/blocked in this offline checker.
    Alternative bindings must start a new ensemble run and pass this gate;
    nested fallback configuration is rejected rather than silently followed.
    """
    for profile in (binding, consent):
        if not auth.tenant_id or profile.get("tenant_id") != auth.tenant_id:
            raise TenantNotFoundError("profile not found")
    now = _timestamp(checked_at)
    if now is None:
        raise ValueError("checked_at must be a timezone-aware timestamp")
    if type(include_live_model_probe) is not bool:
        raise ValueError("include_live_model_probe must be a bool")
    # Detach mutable config before inspecting nested values and hashing it.
    runtime = json.loads(canonical_json(dict(binding)))
    profile = json.loads(canonical_json(dict(consent)))
    checks: list[Check] = []

    def add(name: str, passed: bool, reason: str) -> None:
        checks.append(
            Check(name, "pass" if passed else "fail", "verified snapshot" if passed else reason)
        )

    for name, data, id_field in (
        ("runtime_approval", runtime, "runtime_binding_id"),
        ("consent_approval", profile, "consent_profile_id"),
    ):
        approved_at = _timestamp(data.get("approved_at"))
        add(
            name,
            data.get("status") == "approved"
            and _uuid(data.get(id_field))
            and all(_text(data.get(key)) for key in ("version", "approved_by"))
            and approved_at is not None
            and approved_at <= now,
            "approved version, reviewer and timestamp required",
        )
    verified_at = _timestamp(runtime.get("checked_at"))
    add(
        "account_permission",
        _text(runtime.get("account_id"))
        and bool(re.fullmatch(r"[0-9]{12}", runtime["account_id"]))
        and runtime.get("permissions_verified") is True
        and verified_at is not None
        and verified_at <= now,
        "account access verification missing or invalid",
    )
    model_id = runtime.get("model_id")
    inference_profile = runtime.get("inference_profile_arn")
    region = runtime.get("endpoint_region")
    add(
        "model_binding",
        _model_target(model_id, region, runtime.get("account_id"))
        and runtime.get("role") in ("extractor", "tagger", "vision", "writer")
        and (
            inference_profile is None
            or _model_target(
                inference_profile, region, runtime.get("account_id"), profile_only=True
            )
        ),
        "model ID, role or inference profile is missing/invalid",
    )
    destinations = _regions(runtime.get("allowed_processing_regions"))
    deployment_regions = _regions(list(allowed_regions))
    consent_regions = _regions(profile.get("allowed_processing_regions"))
    add(
        "processing_regions",
        bool(destinations and deployment_regions and consent_regions)
        and runtime.get("processing_regions_verified") is True
        and _text(region)
        and region in destinations
        and destinations <= deployment_regions & consent_regions,
        "REGION_DENIED: all endpoint and processing destinations must be verified and allowed",
    )
    add(
        "fallback",
        runtime.get("fallback_bindings", []) == [],
        "fallback requires a separate approved binding and new ensemble run",
    )
    add(
        "structured_output",
        _text(runtime.get("structured_output_strategy"))
        and runtime.get("structured_output_verified") is True,
        "structured output capability verification missing",
    )
    image_required = runtime.get("role") == "vision"
    add(
        "image_input",
        type(runtime.get("accepts_images")) is bool
        and (
            not image_required
            or runtime.get("accepts_images") is True
            and runtime.get("image_input_verified") is True
        ),
        "image input capability verification missing",
    )
    context, output = runtime.get("max_context_tokens"), runtime.get("max_output_tokens")
    add(
        "token_limits",
        type(context) is int and type(output) is int and 0 < output <= context,
        "positive model token limits required; output exceeds context",
    )
    rights = profile.get("allowed_document_rights")
    add(
        "document_rights",
        isinstance(rights, list)
        and bool(rights)
        and all(_text(right) and right != "*" for right in rights),
        "approved document rights required",
    )
    add(
        "data_consent",
        profile.get("provider_terms_approved") is True
        and profile.get("allow_cross_tenant_cache", False) is False
        and profile.get("allow_agentcore_memory", False) is False,
        "provider terms approval required; cross-tenant cache and Memory remain disabled",
    )
    checks.append(
        Check(
            "live_model_probe",
            "not_run",
            (
                "live probe requested but no approved live execution is wired"
                if include_live_model_probe
                else "offline snapshot verification; no model call performed"
            ),
        )
    )
    return Preflight(
        ready=all(c.status != "fail" for c in checks) and not include_live_model_probe,
        checks=tuple(checks),
        binding_sha256=hashlib.sha256(canonical_json(runtime).encode("ascii")).hexdigest(),
        checked_at=checked_at,
    )


def combine_build_checks(result: Preflight, build: SupplyChainResult | None) -> Preflight:
    """Compose build evidence; never run build tooling in an API image.

    Composition supplies a build-time verifier result for its immutable image.
    Missing attestation is not_run and cannot imply deployment readiness.
    Error details may contain internal paths, so expose a stable summary only.
    """
    check = Check(
        "supply_chain",
        "not_run" if build is None else "pass" if build.passed else "fail",
        "build/license verification unavailable"
        if build is None
        else "build/license checks passed"
        if build.passed
        else "build/license checks blocked; inspect deployment evidence",
    )
    return Preflight(
        result.ready and build is not None and build.passed,
        result.checks + (check,),
        result.binding_sha256,
        result.checked_at,
    )


def check_local_upstage_binding(
    *,
    binding: Mapping[str, Any],
    consent: Mapping[str, Any],
    auth: AuthContext,
    checked_at: str,
    source_sha256: str | None = None,
    include_live_model_probe: bool = False,
    model_sha256: str | None = None,
) -> Preflight:
    """Explicit local test consent, never production routing/rights attestation.

    Only the local composition may call this path. No AWS account is invented;
    provider-managed processing geography remains unverified.

    Only solar-pro3/solar-pro4 model IDs are authorized. When ``model_sha256``
    is supplied it must exactly equal
    ``canonical_hash({model: model_id, provider: upstage, transport: UpstageProbe})``.
    Omission preserves the legacy model3-only call shape; run creation and the
    per-call worker must still supply the frozen profile hash so an invalid or
    missing profile can never become an omitted bypass.
    """
    return _check_local_upstage_binding(
        binding=binding,
        consent=consent,
        auth=auth,
        checked_at=checked_at,
        source_sha256=source_sha256,
        include_live_model_probe=include_live_model_probe,
        model_sha256=model_sha256,
        expected_role="extractor",
    )


def _check_local_upstage_binding(
    *,
    binding: Mapping[str, Any],
    consent: Mapping[str, Any],
    auth: AuthContext,
    checked_at: str,
    source_sha256: str | None,
    include_live_model_probe: bool,
    model_sha256: str | None,
    expected_role: str,
    document_parse: bool = False,
) -> Preflight:
    if any(p.get("tenant_id") != auth.tenant_id for p in (binding, consent)):
        raise TenantNotFoundError("profile not found")
    now = _timestamp(checked_at)
    if now is None or type(include_live_model_probe) is not bool:
        raise ValueError("invalid preflight input")
    if model_sha256 is not None and (
        type(model_sha256) is not str or not re.fullmatch(r"[0-9a-f]{64}", model_sha256)
    ):
        raise ValueError("invalid model_sha256")
    checks = []

    def add(name, passed):
        checks.append(
            Check(
                name,
                "pass" if passed else "fail",
                "local test authorization" if passed else "local test authorization invalid",
            )
        )

    for name, profile, id_field in (
        ("runtime_approval", binding, "runtime_binding_id"),
        ("consent_approval", consent, "consent_profile_id"),
    ):
        approved = _timestamp(profile.get("approved_at"))
        expires = _timestamp(profile.get("expires_at"))
        add(
            name,
            profile.get("status") == "approved"
            and _uuid(profile.get(id_field))
            and all(_text(profile.get(k)) for k in ("version", "approved_by"))
            and approved is not None
            and expires is not None
            and approved <= now < expires
            and profile.get("purpose") == "local_test"
            and profile.get("provider") == "upstage",
        )
    model_id = binding.get("model_id")
    models = ("document-parse-260128",) if document_parse else ("solar-pro3", "solar-pro4")
    transport = "UpstageParseProbe" if document_parse else "UpstageProbe"
    endpoint = (
        "https://api.upstage.ai/v1/document-digitization"
        if document_parse
        else "https://api.upstage.ai/v1/chat/completions"
    )
    model_hash_ok = not document_parse and model_id == "solar-pro3"
    if model_sha256 is not None:
        expected = (
            canonical_hash({"model": model_id, "provider": "upstage", "transport": transport})
            if model_id in models
            else None
        )
        model_hash_ok = expected is not None and model_sha256 == expected
    add(
        "model_binding",
        binding.get("role") == expected_role
        and binding.get("model_id") in models
        and model_hash_ok
        and binding.get("endpoint") == endpoint
        and binding.get("budget_limit_usd") in ("10.00", "20.00")
        and binding.get("fallback_bindings", []) == [],
    )
    hashes = consent.get("allowed_source_sha256")
    add(
        "document_scope",
        isinstance(hashes, list | tuple)
        and bool(hashes)
        and all(isinstance(h, str) and re.fullmatch(r"[0-9a-f]{64}", h) for h in hashes)
        and (source_sha256 is None or source_sha256 in hashes),
    )
    rights = consent.get("allowed_document_rights")
    add(
        "document_rights",
        isinstance(rights, list | tuple)
        and bool(rights)
        and all(_text(r) and r != "*" for r in rights),
    )
    add(
        "data_consent",
        consent.get("allow_cross_tenant_cache") is False
        and consent.get("allow_agentcore_memory") is False,
    )
    checks.extend(
        (
            Check(
                "processing_regions",
                "not_run",
                "provider-managed; local test only, deployment blocked",
            ),
            Check("live_model_probe", "not_run", "no model call during preflight"),
        )
    )
    return Preflight(
        all(c.status != "fail" for c in checks) and not include_live_model_probe,
        tuple(checks),
        hashlib.sha256(canonical_json(dict(binding)).encode("ascii")).hexdigest(),
        checked_at,
    )


def check_local_upstage_raster(
    *,
    binding: Mapping[str, Any],
    consent: Mapping[str, Any],
    auth: AuthContext,
    checked_at: str,
    source_sha256: str,
    document_rights: str,
    model_sha256: str,
    include_live_model_probe: bool = False,
) -> Preflight:
    """Separate local-test raster authorization; no network or deployment approval.

    Profiles must be resolved from trusted tenant-scoped Registry storage. Text
    grants never authorize image egress. Callers recheck immediately before spend.
    """
    if (
        not isinstance(source_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None
        or not _text(document_rights)
        or document_rights == "*"
    ):
        raise ValueError("invalid local raster preflight input")
    common = _check_local_upstage_binding(
        binding=binding,
        consent=consent,
        auth=auth,
        checked_at=checked_at,
        source_sha256=source_sha256,
        include_live_model_probe=include_live_model_probe,
        model_sha256=model_sha256,
        expected_role="vision",
        document_parse=True,
    )
    valid = (
        binding.get("schema") == "local_upstage_raster_binding_v1"
        and binding.get("mode") in ("standard", "enhanced")
        and type(binding.get("max_pages")) is int
        and 1 <= binding["max_pages"] <= 10
        and binding.get("accepts_images") is True
        and binding.get("image_input_verified") is True
        and consent.get("allow_raster_upload") is True
        and isinstance(consent.get("allowed_document_rights"), list | tuple)
        and document_rights in consent["allowed_document_rights"]
    )
    checks = common.checks + (
        Check(
            "raster_upload_scope",
            "pass" if valid else "fail",
            "explicit local raster scope" if valid else "local raster scope invalid",
        ),
    )
    return Preflight(common.ready and valid, checks, common.binding_sha256, checked_at)


def check_local_upstage_tagger(
    *,
    binding: Mapping[str, Any],
    consent: Mapping[str, Any],
    auth: AuthContext,
    checked_at: str,
    source_sha256: str,
    document_rights: str,
    settings,
) -> Preflight:
    """Separate local tagger approval, not worker activation or token authorization.

    A trusted registry binding must pin the entire TaggingSettings value. The
    caller must independently enforce source validation, run token accounting,
    the shared USD ledger and lease/cancellation fences before every dispatch.
    Legacy extractor bindings never authorize this role and are not rewritten.
    """
    from proofops.application.tagging.service import TaggingSettings

    if (
        not isinstance(settings, TaggingSettings)
        or not isinstance(source_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", source_sha256)
        or not _text(document_rights)
        or document_rights == "*"
    ):
        raise ValueError("invalid local tagger preflight input")
    common = _check_local_upstage_binding(
        binding=binding,
        consent=consent,
        auth=auth,
        checked_at=checked_at,
        source_sha256=source_sha256,
        include_live_model_probe=False,
        model_sha256=canonical_hash(
            dict(model=settings.model_id, provider="upstage", transport="UpstageProbe")
        ),
        expected_role="tagger",
    )
    from proofops.application.tagging.preliminary import (
        ACTOR_ROLE_SYSTEM_SUFFIX,
        CONTEXT_SYSTEM_SUFFIX,
        GOAL_ROLE_SYSTEM_SUFFIX,
        P1_SYSTEM_PROMPT,
        P2_SYSTEM_PROMPT,
        PERIOD_ROLE_SYSTEM_SUFFIX,
        TABLE_ROLE_SYSTEM_SUFFIX,
        TABLE_SYSTEM_SUFFIX,
    )
    from proofops.application.tagging.preliminary import SYSTEM_PROMPT as PRELIMINARY_SYSTEM_PROMPT
    from proofops.application.tagging.relations import SYSTEM_PROMPT as RELATIONS_SYSTEM_PROMPT

    profile_valid = (
        settings.model_profile
        in {
            "upstage-compact-ids-frozen-unicode-v1",
            "upstage-compact-coverage-unicode-v2",
            "upstage-compact-source-quotes-v3",
            "upstage-compact-source-quotes-v4",
            "upstage-compact-source-quotes-v5",
        }
        or (
            settings.model_profile == "upstage-preliminary-source-quotes-v1"
            and settings.system_prompt == PRELIMINARY_SYSTEM_PROMPT
        )
        or (
            settings.model_profile == "upstage-preliminary-source-quotes-context-v1"
            and settings.system_prompt == PRELIMINARY_SYSTEM_PROMPT + CONTEXT_SYSTEM_SUFFIX
        )
        or (
            # opt-in: context prompt plus the table-source suffix, pinned as
            # one pair so neither half can be swapped independently.
            settings.model_profile == "upstage-preliminary-source-quotes-table-v1"
            and settings.system_prompt
            == PRELIMINARY_SYSTEM_PROMPT + CONTEXT_SYSTEM_SUFFIX + TABLE_SYSTEM_SUFFIX
        )
        or (
            # opt-in: the same table pair plus the role-resolution suffix,
            # pinned as one longer chain so the shorter table prompt can never
            # be sent under this profile and vice versa.
            settings.model_profile == "upstage-preliminary-source-quotes-table-role-v1"
            and settings.system_prompt
            == PRELIMINARY_SYSTEM_PROMPT
            + CONTEXT_SYSTEM_SUFFIX
            + TABLE_SYSTEM_SUFFIX
            + TABLE_ROLE_SYSTEM_SUFFIX
        )
        or (
            settings.model_profile == "upstage-preliminary-source-quotes-table-role-v2-p1"
            and settings.system_prompt == P1_SYSTEM_PROMPT
        )
        or (
            settings.model_profile == "upstage-preliminary-source-quotes-table-role-v2-p2"
            and settings.system_prompt == P2_SYSTEM_PROMPT
        )
        or (
            settings.model_profile == "upstage-preliminary-source-quotes-table-role-v1-position-v1"
            and settings.system_prompt
            == PRELIMINARY_SYSTEM_PROMPT
            + CONTEXT_SYSTEM_SUFFIX
            + TABLE_SYSTEM_SUFFIX
            + TABLE_ROLE_SYSTEM_SUFFIX
        )
        or (
            settings.model_profile
            == "upstage-preliminary-source-quotes-table-role-v2-p2-position-v1"
            and settings.system_prompt == P2_SYSTEM_PROMPT
        )
        or (
            # opt-in: the table-role chain plus the goal-role suffix, pinned
            # as one longer chain so neither the table-role prompt nor any older
            # prompt can be sent under this profile and vice versa. Requires
            # preliminary_table_role=True and its dependencies.
            settings.model_profile == "upstage-preliminary-source-quotes-goal-role-v1"
            and settings.system_prompt
            == PRELIMINARY_SYSTEM_PROMPT
            + CONTEXT_SYSTEM_SUFFIX
            + TABLE_SYSTEM_SUFFIX
            + TABLE_ROLE_SYSTEM_SUFFIX
            + GOAL_ROLE_SYSTEM_SUFFIX
        )
        or (
            settings.model_profile == "upstage-preliminary-source-quotes-actor-role-v1"
            and settings.system_prompt
            == PRELIMINARY_SYSTEM_PROMPT
            + CONTEXT_SYSTEM_SUFFIX
            + TABLE_SYSTEM_SUFFIX
            + TABLE_ROLE_SYSTEM_SUFFIX
            + GOAL_ROLE_SYSTEM_SUFFIX
            + ACTOR_ROLE_SYSTEM_SUFFIX
        )
        or (
            settings.model_profile == "upstage-preliminary-source-quotes-actor-role-v2"
            and settings.system_prompt
            == PRELIMINARY_SYSTEM_PROMPT
            + CONTEXT_SYSTEM_SUFFIX
            + TABLE_SYSTEM_SUFFIX
            + TABLE_ROLE_SYSTEM_SUFFIX
            + GOAL_ROLE_SYSTEM_SUFFIX
            + ACTOR_ROLE_SYSTEM_SUFFIX
            + PERIOD_ROLE_SYSTEM_SUFFIX
        )
        or (
            settings.model_profile == "upstage-relation-source-quotes-v1"
            and settings.system_prompt == RELATIONS_SYSTEM_PROMPT
        )
    )
    pinned = (
        binding.get("schema") == "local_upstage_tagger_binding_v1"
        and binding.get("tagging_settings_sha256") == canonical_hash(asdict(settings))
        and settings.binding.binding_id == binding.get("runtime_binding_id")
        and settings.binding.role == "tagger"
        and settings.binding.synthetic is False
        and settings.model_id == binding.get("model_id")
        and profile_valid
        and settings.region == "provider-managed-unverified"
        and type(settings.temperature) in (int, float)
        and settings.temperature == 0
        and type(settings.max_tokens) is int
        and 1 <= settings.max_tokens <= 4096
        and type(settings.extraction_epoch) is int
        and settings.extraction_epoch > 0
    )
    rights = consent.get("allowed_document_rights")
    authorized_rights = isinstance(rights, list | tuple) and document_rights in rights
    checks = common.checks + (
        Check("tagging_settings", "pass" if pinned else "fail", "frozen local tagger settings"),
        Check(
            "selected_document_rights",
            "pass" if authorized_rights else "fail",
            "exact local document rights approval",
        ),
    )
    return Preflight(
        common.ready and pinned and authorized_rights, checks, common.binding_sha256, checked_at
    )
