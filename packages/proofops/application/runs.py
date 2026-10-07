"""Local run orchestration: freeze trusted inputs, gate, enqueue; never invoke models."""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

from proofops.application.budget import BudgetLimits, cost_summary
from proofops.application.claim_scope import validate_extraction_limits
from proofops.application.claims import ExtractionProfile
from proofops.application.ingest.graph_fusion import ParserProfile
from proofops.application.preflight import (
    check_local_upstage_binding,
    check_local_upstage_raster,
    check_local_upstage_tagger,
    check_runtime_binding,
    combine_build_checks,
)
from proofops.application.registry import RegistryNotFound, artifact_sha256
from proofops.application.tagging.consensus import PARTIAL_FACTS_V1
from proofops.application.tagging.relations import SYSTEM_PROMPT as RELATION_SYSTEM_PROMPT
from proofops.application.tagging.report_level_link import (
    validate_config as validate_report_level_link,
)
from proofops.application.tagging.service import TaggingSettings
from proofops.application.uploads_security import UploadRejected
from proofops.domain.provenance import canonical_hash
from proofops.domain.values import _require_sha256, _require_uuid


class RunRejected(ValueError):
    def __init__(self, code: str, status: int = 409):
        super().__init__(code)
        self.code, self.status = code, status


_LIVE_TAGGING_MODELS = ("solar-pro4", "openai/gpt-6-luna")


def _capacity_accommodated(limits: BudgetLimits | None, upper: int, output_cap: int) -> bool:
    """Role budgets must cover the reserved input upper bound plus the output cap."""
    if limits is None:
        return False
    candidates = [role for role in limits.roles if role.role == "tagger"]
    if not candidates:
        return False
    return any(
        role.max_input_tokens >= upper
        and role.max_output_tokens >= output_cap
        and role.max_context_tokens >= upper + output_cap
        for role in candidates
    )


def _detach(value):
    if isinstance(value, Mapping):
        return {key: _detach(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_detach(item) for item in value]
    return value


def validate_raster_policy(policy):
    """Validate trusted policy shape without filesystem/provider dependencies."""
    hashes = (
        "native_policy_sha256",
        "raster_helper_sha256",
        "composition_helper_sha256",
        "checkpoint_helper_sha256",
    )
    if (
        not isinstance(policy, Mapping)
        or set(policy) != {"schema", "mode", "max_pages", "max_calls", "reader_versions", *hashes}
        or policy["schema"] != "local_raster_ocr_policy_v1"
        or policy["mode"] not in ("standard", "enhanced")
        or type(policy["max_pages"]) is not int
        or not 1 <= policy["max_pages"] <= 10
        or type(policy["max_calls"]) is not int
        or not 1 <= policy["max_calls"] <= 20
        or not isinstance(policy["reader_versions"], Mapping)
        or set(policy["reader_versions"]) != {"pypdfium2", "pdfplumber", "pypdf", "Pillow"}
        or any(not isinstance(v, str) or not v.strip() for v in policy["reader_versions"].values())
    ):
        raise ValueError("RASTER_POLICY_INVALID")
    for name in hashes:
        _require_sha256(name, policy[name])
    return _detach(policy)


def validate_raster_snapshot(snapshot):
    fields = {
        "raster_ocr_policy",
        "raster_ocr_policy_hash",
        "raster_ocr_runtime",
        "raster_ocr_runtime_artifact_hash",
    }
    present = {k for k in snapshot if k.startswith("raster_ocr_")}
    if not present:
        return
    if (
        present != fields
        or snapshot.get("extraction_mode") != "upstage_probe"
        or snapshot.get("mode") != "disclosure"
        or snapshot.get("scope") != "declared_subset"
    ):
        raise ValueError("RASTER_SNAPSHOT_INVALID")
    policy = validate_raster_policy(snapshot["raster_ocr_policy"])
    grant = snapshot["raster_ocr_runtime"]
    if (
        not isinstance(grant, Mapping)
        or canonical_hash(policy) != snapshot["raster_ocr_policy_hash"]
        or artifact_sha256(grant) != snapshot["raster_ocr_runtime_artifact_hash"]
        or grant.get("raster_policy_sha256") != snapshot["raster_ocr_policy_hash"]
        or grant.get("mode") != policy["mode"]
        or type(grant.get("max_pages")) is not int
        or not policy["max_pages"] <= grant["max_pages"] <= 10
        or type(grant.get("max_calls")) is not int
        or not policy["max_calls"] <= grant["max_calls"] <= 20
    ):
        raise ValueError("RASTER_SNAPSHOT_INVALID")


class RunService:
    """Root supplies trusted parser/budget/build config; absent config fails closed."""

    def __init__(
        self,
        store,
        uploads,
        registry,
        *,
        parser_profile_hash=None,
        parser_profile=None,
        extraction_profile: ExtractionProfile | None = None,
        extraction_mode: str | None = None,
        extraction_limits=None,
        claim_source_policy=None,
        tagging_settings: TaggingSettings | None = None,
        tagging_mode: str | None = None,
        fact_assembly_profile: str = "strict-v1",
        report_level_link=None,
        preliminary_settings: TaggingSettings | None = None,
        relation_settings: TaggingSettings | None = None,
        raster_runtime_binding_id: str | None = None,
        raster_policy=None,
        input_reservation_policy=None,
        position_context_order=None,
        budget_limits=None,
        allowed_regions=(),
        build_result=None,
        clock=time.time,
        app_env="local",
        model_adapter="synthetic",
    ):
        if app_env != "local" or model_adapter != "synthetic" or not uploads.local_synthetic:
            raise ValueError("run lifecycle requires local-synthetic composition")
        if parser_profile_hash is not None:
            _require_sha256("parser_profile_hash", parser_profile_hash)
        if parser_profile is not None:
            if not isinstance(parser_profile, Mapping):
                raise ValueError("trusted parser profile required")
            parser_profile = _detach(parser_profile)
            profile = ParserProfile(
                parse_manifest_id="00000000-0000-4000-8000-000000000000", **parser_profile
            )
            if (
                profile.config_snapshot() != parser_profile
                or profile.config_hash() != parser_profile_hash
            ):
                raise ValueError("parser profile hash or configuration mismatch")
            parser_profile = MappingProxyType(parser_profile)
        if budget_limits is not None and not isinstance(budget_limits, BudgetLimits):
            raise ValueError("trusted BudgetLimits required")
        if preliminary_settings is not None and not isinstance(
            preliminary_settings, TaggingSettings
        ):
            raise ValueError("trusted preliminary TaggingSettings required")
        if relation_settings is not None and not isinstance(relation_settings, TaggingSettings):
            raise ValueError("trusted relation TaggingSettings required")
        if input_reservation_policy is not None and not isinstance(
            input_reservation_policy, Mapping
        ):
            raise ValueError("trusted input reservation policy required")
        self.store, self.uploads, self.registry = store, uploads, registry
        self.parser_profile_hash, self.budget_limits = parser_profile_hash, budget_limits
        self.parser_profile = parser_profile
        self.extraction_profile, self.extraction_mode = extraction_profile, extraction_mode
        self.extraction_limits = _detach(extraction_limits)
        if claim_source_policy is not None and (
            not isinstance(claim_source_policy, Mapping)
            or claim_source_policy.get("schema")
            not in {
                "claim_source_policy_v1",
                "claim_source_policy_v2",
                # Opt-in render-resolution wrapper. Its own distinct
                # schema; the adapter refuses any policy it cannot reproduce.
                "claim_span_render_resolution_policy_v1",
                # Opt-in bullet-alignment wrapper, which wraps the one
                # above rather than replacing it. Same refusal guarantee.
                "claim_span_bullet_alignment_policy_v1",
                # Opt-in rendered-side-only typography wrapper, which
                # wraps the one above. Same refusal guarantee.
                "claim_span_typography_policy_v1",
            }
            or extraction_mode != "upstage_probe"
        ):
            raise ValueError("invalid claim source policy")
        self.claim_source_policy = _detach(claim_source_policy)
        self.tagging_settings, self.tagging_mode = tagging_settings, tagging_mode
        if report_level_link is not None and (tagging_mode is None or tagging_settings is None):
            raise ValueError("report-level link requires tagging")
        if fact_assembly_profile not in ("strict-v1", PARTIAL_FACTS_V1):
            raise ValueError("unknown fact assembly profile")
        self.fact_assembly_profile = fact_assembly_profile
        self.report_level_link = (
            _detach(validate_report_level_link(report_level_link))
            if report_level_link is not None
            else None
        )
        self.preliminary_settings = preliminary_settings
        if position_context_order is not None:
            from proofops.application.tagging.preliminary import CONTEXT_POSITION_ORDER

            if position_context_order != CONTEXT_POSITION_ORDER:
                raise ValueError("invalid context order policy")
        if preliminary_settings is not None and (position_context_order is not None) != (
            preliminary_settings.model_profile
            in {
                "upstage-preliminary-source-quotes-table-role-v1-position-v1",
                "upstage-preliminary-source-quotes-table-role-v2-p2-position-v1",
            }
        ):
            raise ValueError("context order profile mismatch")
        self.position_context_order = position_context_order
        self.relation_settings = relation_settings
        if (raster_runtime_binding_id is None) != (raster_policy is None):
            raise ValueError("complete raster configuration required")
        if raster_runtime_binding_id is not None:
            _require_uuid("raster_runtime_binding_id", raster_runtime_binding_id)
            raster_policy = validate_raster_policy(raster_policy)
        self.raster_runtime_binding_id, self.raster_policy = (
            raster_runtime_binding_id,
            raster_policy,
        )
        self.input_reservation_policy = (
            _detach(dict(input_reservation_policy))
            if input_reservation_policy is not None
            else None
        )
        self.allowed_regions, self.build_result = tuple(allowed_regions), build_result
        self.clock = clock
        self.local_synthetic = True

    def create(self, auth, body: dict[str, Any], key: str):
        if self.report_level_link is not None:
            validate_report_level_link(self.report_level_link)
        created_time = self.clock()
        now = int(created_time)
        replay = self.store.replay(auth.tenant_id, body, key, now=now)
        if replay is not None:
            return replay
        try:
            document = self.uploads.version_snapshot(auth.tenant_id, body["document_version_id"])
        except UploadRejected:
            raise RunRejected("RESOURCE_NOT_FOUND", 404) from None
        if document["status"] != "ready" or not document.get("object_version_id"):
            raise RunRejected("CONFIG_GATE_BLOCKED")
        count = document["page_count"]
        pages = body.get("selected_pages")
        if body["scope"] == "full":
            valid = "selected_pages" not in body
        else:
            valid = (
                isinstance(pages, list)
                and bool(pages)
                and all(type(p) is int and 1 <= p <= count for p in pages)
                and pages == sorted(set(pages))
            )
        if not valid:
            raise RunRejected("VALIDATION_ERROR", 422)
        if (
            self.parser_profile is None
            or canonical_hash(dict(self.parser_profile)) != self.parser_profile_hash
            or self.budget_limits is None
        ):
            raise RunRejected("CONFIG_GATE_BLOCKED")
        try:
            runtime = _detach(
                self.registry.resolve_profile(auth, "runtime", body["runtime_binding_id"])
            )
            consent = _detach(
                self.registry.resolve_profile(auth, "consent", body["consent_profile_id"])
            )
            rights_id = document["metadata"]["rights_profile_id"]
            rights = _detach(self.registry.resolve_profile(auth, "rights", rights_id))
        except RegistryNotFound:
            raise RunRejected("CONFIG_GATE_BLOCKED") from None
        timestamp = datetime.fromtimestamp(created_time, UTC).isoformat().replace("+00:00", "Z")
        probe_model_sha256: str | None = None
        if self.extraction_mode == "upstage_probe":
            # The frozen extraction profile hash must authorize run creation.
            # An invalid or missing profile must fail closed here and never
            # become an omitted model_sha256 bypass in the binding check.
            if (
                not isinstance(self.extraction_profile, ExtractionProfile)
                or self.extraction_profile.synthetic is not False
            ):
                raise RunRejected("CONFIG_GATE_BLOCKED")
            probe_model_sha256 = self.extraction_profile.model_sha256
        preflight = combine_build_checks(
            check_local_upstage_binding(
                binding=runtime,
                consent=consent,
                auth=auth,
                checked_at=timestamp,
                source_sha256=document["sha256"],
                model_sha256=probe_model_sha256,
            )
            if self.extraction_mode == "upstage_probe"
            else check_runtime_binding(
                binding=runtime,
                consent=consent,
                auth=auth,
                allowed_regions=self.allowed_regions,
                checked_at=timestamp,
            ),
            self.build_result,
        )
        if not preflight.ready or rights_id not in consent["allowed_document_rights"]:
            raise RunRejected("CONFIG_GATE_BLOCKED")
        if (
            self.parser_profile.get("parser_mode") == "upstage"
            and consent.get("allow_raster_upload") is not True
        ):
            raise RunRejected("CONFIG_GATE_BLOCKED")
        raster_snapshot = {}
        if self.raster_runtime_binding_id is not None or self.raster_policy is not None:
            try:
                _require_uuid("raster_runtime_binding_id", self.raster_runtime_binding_id)
                raster_policy = validate_raster_policy(self.raster_policy)
                raster_runtime = _detach(
                    self.registry.resolve_profile(auth, "runtime", self.raster_runtime_binding_id)
                )
                raster_preflight = check_local_upstage_raster(
                    binding=raster_runtime,
                    consent=consent,
                    auth=auth,
                    checked_at=timestamp,
                    source_sha256=document["sha256"],
                    document_rights=rights_id,
                    model_sha256=canonical_hash(
                        dict(
                            model="document-parse-260128",
                            provider="upstage",
                            transport="UpstageParseProbe",
                        )
                    ),
                )
                if not raster_preflight.ready:
                    raise ValueError("RASTER_PREFLIGHT_BLOCKED")
                raster_snapshot = dict(
                    raster_ocr_policy=raster_policy,
                    raster_ocr_policy_hash=canonical_hash(raster_policy),
                    raster_ocr_runtime=raster_runtime,
                    raster_ocr_runtime_artifact_hash=artifact_sha256(raster_runtime),
                )
                validate_raster_snapshot(
                    dict(
                        raster_snapshot,
                        extraction_mode=self.extraction_mode,
                        mode=body["mode"],
                        scope=body["scope"],
                    )
                )
            except (RegistryNotFound, ValueError, KeyError, TypeError):
                raise RunRejected("CONFIG_GATE_BLOCKED") from None
        if (self.extraction_profile is not None or self.extraction_mode is not None) and (
            not isinstance(self.extraction_profile, ExtractionProfile)
            or (self.extraction_profile.synthetic, self.extraction_mode)
            not in {(True, "local_synthetic"), (False, "upstage_probe")}
        ):
            raise RunRejected("CONFIG_GATE_BLOCKED")
        if self.extraction_mode == "upstage_probe":
            limits = self.extraction_limits
            live_tagging = self.tagging_mode == "upstage_local"
            if body["scope"] != "declared_subset" or (
                not live_tagging
                and (self.tagging_settings is not None or self.tagging_mode is not None)
            ):
                raise RunRejected("CONFIG_GATE_BLOCKED")
            # ``pages`` is the validated broad declared subset; claim_pages must be
            # a subset of it and never widens the parse/evidence scope.
            try:
                validate_extraction_limits(limits, pages)
            except ValueError:
                raise RunRejected("CONFIG_GATE_BLOCKED") from None
        elif self.extraction_limits is not None:
            raise RunRejected("CONFIG_GATE_BLOCKED")
        tagging = self.tagging_settings
        live_tagging = self.tagging_mode == "upstage_local"
        if self.relation_settings is not None and not live_tagging:
            raise RunRejected("CONFIG_GATE_BLOCKED")
        if live_tagging:
            if self.extraction_mode != "upstage_probe":
                raise RunRejected("CONFIG_GATE_BLOCKED")
            preliminary = self.preliminary_settings
            relation = self.relation_settings
            if not isinstance(preliminary, TaggingSettings) or not isinstance(
                tagging, TaggingSettings
            ):
                raise RunRejected("CONFIG_GATE_BLOCKED")
            if relation is not None and not isinstance(relation, TaggingSettings):
                raise RunRejected("CONFIG_GATE_BLOCKED")
            if preliminary.model_profile not in {
                "upstage-preliminary-source-quotes-v1",
                "upstage-preliminary-source-quotes-context-v1",
                # Opt-in table pairs. The profile↔prompt welding for
                # every name here is enforced by ``check_local_upstage_tagger``
                # below, so this set only names the accepted profiles.
                "upstage-preliminary-source-quotes-table-v1",
                "upstage-preliminary-source-quotes-table-role-v1",
                "upstage-preliminary-source-quotes-table-role-v1-position-v1",
                "upstage-preliminary-source-quotes-table-role-v2-p2",
                "upstage-preliminary-source-quotes-table-role-v2-p2-position-v1",
                # opt-in goal-role profile; requires table-role and its deps.
                "upstage-preliminary-source-quotes-goal-role-v1",
                "upstage-preliminary-source-quotes-actor-role-v1",
                "upstage-preliminary-source-quotes-actor-role-v2",
            } or tagging.model_profile not in {
                "upstage-compact-ids-frozen-unicode-v1",
                "upstage-compact-coverage-unicode-v2",
                "upstage-compact-source-quotes-v3",
                "upstage-compact-source-quotes-v4",
                "upstage-compact-source-quotes-v5",
            }:
                raise RunRejected("CONFIG_GATE_BLOCKED")
            if relation is not None and (
                relation.model_profile != "upstage-relation-source-quotes-v1"
                or relation.system_prompt != RELATION_SYSTEM_PROMPT
            ):
                raise RunRejected("CONFIG_GATE_BLOCKED")
            pinned_settings = (preliminary, tagging) + ((relation,) if relation is not None else ())
            for pinned in pinned_settings:
                if (
                    pinned.binding.synthetic is not False
                    or pinned.binding.role != "tagger"
                    or pinned.model_id not in _LIVE_TAGGING_MODELS
                    or type(pinned.max_tokens) is not int
                    or not 1 <= pinned.max_tokens <= 4096
                ):
                    raise RunRejected("CONFIG_GATE_BLOCKED")
            policy = self.input_reservation_policy
            if not isinstance(policy, dict) or not policy:
                raise RunRejected("CONFIG_GATE_BLOCKED")
            policy_hash = canonical_hash(policy)
            try:
                preliminary_runtime = _detach(
                    self.registry.resolve_profile(auth, "runtime", preliminary.binding.binding_id)
                )
                tagging_runtime = _detach(
                    self.registry.resolve_profile(auth, "runtime", tagging.binding.binding_id)
                )
                relation_runtime = (
                    _detach(
                        self.registry.resolve_profile(auth, "runtime", relation.binding.binding_id)
                    )
                    if relation is not None
                    else None
                )
            except RegistryNotFound:
                raise RunRejected("CONFIG_GATE_BLOCKED") from None
            if len(
                {
                    body["runtime_binding_id"],
                    preliminary.binding.binding_id,
                    tagging.binding.binding_id,
                    *((relation.binding.binding_id,) if relation is not None else ()),
                }
            ) != 3 + (relation is not None):
                raise RunRejected("CONFIG_GATE_BLOCKED")
            runtime_pairs = [
                (preliminary, preliminary_runtime),
                (tagging, tagging_runtime),
            ] + ([(relation, relation_runtime)] if relation is not None else [])
            for pinned, bound_runtime in runtime_pairs:
                if (
                    pinned.binding.binding_id != bound_runtime.get("runtime_binding_id")
                    or bound_runtime.get("input_reservation_policy_sha256") != policy_hash
                ):
                    raise RunRejected("CONFIG_GATE_BLOCKED")
                if not check_local_upstage_tagger(
                    binding=bound_runtime,
                    consent=consent,
                    auth=auth,
                    checked_at=timestamp,
                    source_sha256=document["sha256"],
                    document_rights=rights_id,
                    settings=pinned,
                ).ready:
                    raise RunRejected("CONFIG_GATE_BLOCKED")
            from proofops.adapters.local.openrouter import (
                validate_capacity_policy as validate_luna_capacity,
            )
            from proofops.application.input_reservation import validate_capacity_policy

            for pinned in pinned_settings:
                try:
                    upper = (
                        validate_luna_capacity(policy, model_id=pinned.model_id)
                        if pinned.model_id == "openai/gpt-6-luna"
                        else validate_capacity_policy(
                            policy,
                            model_id=pinned.model_id,
                            checked_at=datetime.fromtimestamp(created_time, UTC),
                        )
                    )
                except ValueError:
                    raise RunRejected("CONFIG_GATE_BLOCKED") from None
                if not _capacity_accommodated(self.budget_limits, upper, pinned.max_tokens):
                    raise RunRejected("CONFIG_GATE_BLOCKED")
        elif (tagging is not None or self.tagging_mode is not None) and (
            not isinstance(tagging, TaggingSettings)
            or self.tagging_mode != "local_synthetic"
            or self.extraction_profile is None
            or tagging.binding.synthetic is not True
            or tagging.binding.binding_id != runtime["runtime_binding_id"]
            or tagging.binding.role != runtime["role"]
            or (tagging.model_id, tagging.region)
            != (runtime["model_id"], runtime["endpoint_region"])
            or type(tagging.max_tokens) is not int
            or not 1 <= tagging.max_tokens <= runtime["max_output_tokens"]
        ):
            raise RunRejected("CONFIG_GATE_BLOCKED")
        snapshot = dict(
            document=document,
            runtime=runtime,
            consent=consent,
            rights=rights,
            runtime_artifact_hash=artifact_sha256(runtime),
            consent_artifact_hash=artifact_sha256(consent),
            rights_artifact_hash=artifact_sha256(rights),
            model_binding_hash=preflight.binding_sha256,
            parser_profile_hash=self.parser_profile_hash,
            parser_profile=dict(self.parser_profile),
            budget_limits=asdict(self.budget_limits),
            build_result=asdict(self.build_result),
            build_result_hash=canonical_hash(asdict(self.build_result)),
            selected_pages=list(range(1, count + 1)) if pages is None else pages,
            mode=body["mode"],
            scope=body["scope"],
            created_at=timestamp,
        )
        if self.extraction_profile is not None:
            snapshot.update(
                extraction_profile=asdict(self.extraction_profile),
                extraction_profile_hash=canonical_hash(asdict(self.extraction_profile)),
                extraction_mode=self.extraction_mode,
            )
        if self.extraction_mode == "upstage_probe":
            snapshot["extraction_limits"] = dict(self.extraction_limits)
        if self.claim_source_policy is not None:
            snapshot["claim_source_policy"] = _detach(self.claim_source_policy)
        if tagging is not None:
            snapshot.update(
                tagging_settings=asdict(tagging),
                tagging_settings_hash=canonical_hash(asdict(tagging)),
                tagging_mode=self.tagging_mode,
            )
            if self.fact_assembly_profile == PARTIAL_FACTS_V1:
                snapshot["fact_assembly_profile"] = PARTIAL_FACTS_V1
            if self.report_level_link is not None:
                snapshot["report_level_link"] = _detach(self.report_level_link)
        if live_tagging:
            assert isinstance(preliminary, TaggingSettings)
            assert isinstance(tagging, TaggingSettings)
            assert isinstance(policy, dict)
            snapshot.update(
                preliminary_settings=asdict(preliminary),
                preliminary_settings_hash=canonical_hash(asdict(preliminary)),
                preliminary_prompt_sha256=canonical_hash(preliminary.system_prompt),
                preliminary_runtime=preliminary_runtime,
                preliminary_runtime_artifact_hash=artifact_sha256(preliminary_runtime),
                tagging_runtime=tagging_runtime,
                tagging_runtime_artifact_hash=artifact_sha256(tagging_runtime),
                input_reservation_policy=dict(policy),
                input_reservation_policy_hash=policy_hash,
            )
            if relation is not None:
                snapshot.update(
                    relation_settings=asdict(relation),
                    relation_settings_hash=canonical_hash(asdict(relation)),
                    relation_runtime=relation_runtime,
                    relation_runtime_artifact_hash=artifact_sha256(relation_runtime),
                )
        snapshot.update(raster_snapshot)
        if self.position_context_order is not None:
            snapshot["position_context_order"] = dict(self.position_context_order)
        return self.store.create(auth, body, key, snapshot, self.budget_limits, now=now)

    def get(self, tenant_id, run_id):
        return self.store.get(tenant_id, run_id)

    def list(self, tenant_id, *, cursor=None, limit=50):
        return self.store.list(tenant_id, cursor=cursor, limit=limit, now=int(self.clock()))

    def action(self, auth, run_id, action, *, expected_revision, key, reason):
        return self.store.action(
            auth,
            run_id,
            action,
            expected_revision=expected_revision,
            key=key,
            reason=reason,
            now=int(self.clock()),
        )

    def cost(self, tenant_id, run_id):
        self.get(tenant_id, run_id)
        result = cost_summary(self.store.usage, tenant_id, run_id)
        usage = self.store.jobs.list_usage(tenant_id, run_id)
        automatic_notes = any("note_request_ids" in row for row in usage)
        if (
            automatic_notes
            or self.store.snapshot(tenant_id, run_id).get("extraction_mode") == "upstage_probe"
        ):
            from decimal import Decimal

            records = [
                row
                for row in usage
                if "reserved_calls" in row or row.get("accounting_complete") is False
            ]
            calls = sum(row.get("reserved_calls", 0) for row in records)
            finalized = {
                identifier for row in records for identifier in row.get("note_request_ids", [])
            }
            pending = {
                identifier
                for row in records
                for identifier in row.get("note_pending_request_ids", [])
            } - finalized
            known = (
                (bool(calls) or automatic_notes)
                and not pending
                and all(row.get("token_usage_complete") is True for row in records)
            )
            result.update(
                input_tokens=sum(row.get("input_tokens", 0) for row in records),
                output_tokens=sum(row.get("output_tokens", 0) for row in records),
                attempt_count=calls,
                amount=str(
                    sum((Decimal(row["cost_with_vat_reserve_usd"]) for row in records), Decimal(0))
                )
                if known
                else None,
                cost_status="known"
                if known
                else "partial"
                if any(row.get("settled_calls", 0) for row in records)
                else "unknown_cost",
            )
        return result

    def audit(self, tenant_id, run_id, *, cursor=None, limit=50):
        return self.store.audit(
            tenant_id, run_id, cursor=cursor, limit=limit, now=int(self.clock())
        )
