"""Bounded preliminary transport over the paid-transport safeguards; no paid calls here.

A subclass of UpstageTaggingTransport that sends only the trusted
preliminary_request envelope (application/tagging/preliminary) as literal
Unicode JSON. It never approves source quality and never computes domain
grades or labels: the trusted composer must build the packet from
preliminary_request and authorize the actual source. Locks, stop/incomplete
fences, exclusive receipt directories, the shared probe ledger, raw receipts
and authorization are all inherited unchanged.

Three model profiles select three structurally distinct, non-overlapping wire
shapes: the legacy MODEL_PROFILE (sources only, byte-identical to before),
CONTEXT_MODEL_PROFILE (adds a separate, non-indexable context_blocks list with
full block provenance) and TABLE_MODEL_PROFILE (additionally allows numbered
sources that carry a table_role: real, source-verified cells of the claim
value's own table row/column). A packet's schema must match its profile;
nothing here weakens the legacy or context path, and a table-role source is
refused outright on the two older profiles.

TABLE_ROLE_MODEL_PROFILE (R16) is a fourth profile that reuses the TABLE wire
shape and the TABLE validator unchanged and differs ONLY in its pinned prompt,
which adds the additive suffix resolving the atomic-source/table-axis conflict.
It is therefore validated exactly like TABLE_MODEL_PROFILE except that its
packet must pin the longer prompt hash, so the two prompts can never be swapped.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from proofops.adapters.local.openrouter import OpenRouterProbe
from proofops.adapters.local.upstage import UpstageProbe
from proofops.application.preflight import Preflight
from proofops.application.tagging.preliminary import (
    ACTOR_ROLE_SYSTEM_SUFFIX,
    CONTEXT_POSITION_ORDER,
    CONTEXT_SCHEMA,
    CONTEXT_SYSTEM_SUFFIX,
    GOAL_ROLE_SYSTEM_SUFFIX,
    P1_SYSTEM_PROMPT,
    P2_SYSTEM_PROMPT,
    PERIOD_ROLE_SYSTEM_SUFFIX,
    SCHEMA,
    SYSTEM_PROMPT,
    TABLE_ROLE_SYSTEM_SUFFIX,
    TABLE_SCHEMA,
    TABLE_SYSTEM_SUFFIX,
)
from proofops.application.tagging.service import TaggingSettings
from proofops.domain.provenance import canonical_hash
from proofops.domain.values import _require_sha256, _require_uuid

from proofops_agent.upstage_tagging import TransportResume, UpstageTaggingTransport

MODEL_PROFILE = "upstage-preliminary-source-quotes-v1"
CONTEXT_MODEL_PROFILE = "upstage-preliminary-source-quotes-context-v1"
TABLE_MODEL_PROFILE = "upstage-preliminary-source-quotes-table-v1"
# R16: same TABLE wire schema and same validator; only the pinned prompt differs.
TABLE_ROLE_MODEL_PROFILE = "upstage-preliminary-source-quotes-table-role-v1"
POSITION_TABLE_ROLE_MODEL_PROFILE = "upstage-preliminary-source-quotes-table-role-v1-position-v1"
P1_MODEL_PROFILE = "upstage-preliminary-source-quotes-table-role-v2-p1"
P2_MODEL_PROFILE = "upstage-preliminary-source-quotes-table-role-v2-p2"
POSITION_P2_MODEL_PROFILE = "upstage-preliminary-source-quotes-table-role-v2-p2-position-v1"
# R34: same TABLE wire schema and same TABLE validator; adds GOAL_ROLE_SYSTEM_SUFFIX
# after TABLE_ROLE_SYSTEM_SUFFIX so the goal-role prompt is a strict extension of
# the table-role prompt. Requires preliminary_table_role=True and its dependencies.
GOAL_ROLE_MODEL_PROFILE = "upstage-preliminary-source-quotes-goal-role-v1"
ACTOR_ROLE_MODEL_PROFILE = "upstage-preliminary-source-quotes-actor-role-v1"
ACTOR_ROLE_MODEL_PROFILE_V2 = "upstage-preliminary-source-quotes-actor-role-v2"
TRANSPORT_VERSION = "preliminary-source-quotes-v1"
CONTEXT_TRANSPORT_VERSION = "preliminary-source-quotes-context-v1"
TABLE_TRANSPORT_VERSION = "preliminary-source-quotes-table-v1"
TABLE_ROLE_TRANSPORT_VERSION = "preliminary-source-quotes-table-role-v1"
POSITION_TABLE_ROLE_TRANSPORT_VERSION = "preliminary-source-quotes-table-role-v1-position-v1"
P1_TRANSPORT_VERSION = "preliminary-source-quotes-table-role-v2-p1"
P2_TRANSPORT_VERSION = "preliminary-source-quotes-table-role-v2-p2"
POSITION_P2_TRANSPORT_VERSION = "preliminary-source-quotes-table-role-v2-p2-position-v1"
# New transport version for replay separation: a stored goal-role receipt can never
# be replayed as a table-role response and vice versa.
GOAL_ROLE_TRANSPORT_VERSION = "preliminary-source-quotes-goal-role-v1"
ACTOR_ROLE_TRANSPORT_VERSION = "preliminary-source-quotes-actor-role-v1"
ACTOR_ROLE_TRANSPORT_VERSION_V2 = "preliminary-source-quotes-actor-role-v2"
CONTEXT_SYSTEM_PROMPT = SYSTEM_PROMPT + CONTEXT_SYSTEM_SUFFIX
TABLE_SYSTEM_PROMPT = CONTEXT_SYSTEM_PROMPT + TABLE_SYSTEM_SUFFIX
TABLE_ROLE_SYSTEM_PROMPT = TABLE_SYSTEM_PROMPT + TABLE_ROLE_SYSTEM_SUFFIX
GOAL_ROLE_SYSTEM_PROMPT = TABLE_ROLE_SYSTEM_PROMPT + GOAL_ROLE_SYSTEM_SUFFIX
ACTOR_ROLE_SYSTEM_PROMPT = GOAL_ROLE_SYSTEM_PROMPT + ACTOR_ROLE_SYSTEM_SUFFIX
ACTOR_ROLE_SYSTEM_PROMPT_V2 = ACTOR_ROLE_SYSTEM_PROMPT + PERIOD_ROLE_SYSTEM_SUFFIX

_ENVELOPE_KEYS = frozenset(
    (
        "schema",
        "tenant_id",
        "claim_id",
        "claim_sha256",
        "graph_sha256",
        "prompt_sha256",
        "untrusted_document_data",
    )
)
_CONTEXT_ENVELOPE_KEYS = _ENVELOPE_KEYS | {"context_policy"}
_TABLE_ENVELOPE_KEYS = _CONTEXT_ENVELOPE_KEYS | {"table_policy"}
_CONTEXT_ENTRY_KEYS = frozenset(
    ("context_index", "role", "source_id", "page_num", "quality", "text", "source_ref")
)
_CONTEXT_ROLES = frozenset(("parent_paragraph", "heading", "nearby"))
# Table axis cells that did NOT pass source verification travel as context only,
# under their own prefixed roles so they can never be read as numbered sources.
_TABLE_CONTEXT_ROLES = frozenset(("table_row_header", "table_column_header", "table_row_qualifier"))
_TABLE_SOURCE_ROLES = frozenset(("row_header", "column_header", "row_qualifier"))
_TABLE_SOURCE_KEYS = frozenset(
    (
        "source_index",
        "text",
        "table_role",
        "table_association",
        "table_row_number",
        "table_column_number",
    )
)
_TABLE_ASSOCIATIONS = frozenset(("row_covered", "column_covered"))
_TABLE_POLICY_KEYS = frozenset(
    (
        "policy",
        "role_basis",
        "lineage",
        "focal_table_native_id",
        "focal_row_number",
        "focal_column_number",
        "max_table_sources",
        "max_table_chars",
        "verified_sources_only",
    )
)
_CONTEXT_POLICY_KEYS = frozenset(
    (
        "same_page_or_section_parent_only",
        "excluded_kinds",
        "max_context_chars",
        "max_context_blocks",
        "whole_blocks_only",
    )
)


class UpstagePreliminaryTransport(UpstageTaggingTransport):
    """Preliminary source-quotes transport; wire contract only, no grading."""

    MODEL_PROFILE = MODEL_PROFILE
    TRANSPORT_VERSION = TRANSPORT_VERSION

    def __init__(
        self,
        probe,
        receipts,
        *,
        settings,
        tenant_id,
        authorize,
        resume=None,
    ):
        _require_uuid("tenant_id", tenant_id)
        if settings.model_profile in (P2_MODEL_PROFILE, POSITION_P2_MODEL_PROFILE):
            expected_prompt = P2_SYSTEM_PROMPT
        elif settings.model_profile == P1_MODEL_PROFILE:
            expected_prompt = P1_SYSTEM_PROMPT
        elif settings.model_profile == ACTOR_ROLE_MODEL_PROFILE_V2:
            expected_prompt = ACTOR_ROLE_SYSTEM_PROMPT_V2
        elif settings.model_profile == ACTOR_ROLE_MODEL_PROFILE:
            expected_prompt = ACTOR_ROLE_SYSTEM_PROMPT
        elif settings.model_profile == GOAL_ROLE_MODEL_PROFILE:
            expected_prompt = GOAL_ROLE_SYSTEM_PROMPT
        elif settings.model_profile in (
            TABLE_ROLE_MODEL_PROFILE,
            POSITION_TABLE_ROLE_MODEL_PROFILE,
        ):
            expected_prompt = TABLE_ROLE_SYSTEM_PROMPT
        elif settings.model_profile == TABLE_MODEL_PROFILE:
            expected_prompt = TABLE_SYSTEM_PROMPT
        elif settings.model_profile == CONTEXT_MODEL_PROFILE:
            expected_prompt = CONTEXT_SYSTEM_PROMPT
        elif settings.model_profile == MODEL_PROFILE:
            expected_prompt = SYSTEM_PROMPT
        else:
            raise ValueError("UPSTAGE_TAGGING_BINDING_INVALID")
        if (
            not isinstance(probe, self._probe_type())
            or not isinstance(settings, TaggingSettings)
            or settings.binding.synthetic
            or settings.model_id != probe.model
            or (
                isinstance(probe, OpenRouterProbe)
                and settings.wire_policy_version != probe.wire_policy_version
            )
            or settings.region != "provider-managed-unverified"
            or settings.system_prompt != expected_prompt
        ):
            raise ValueError("UPSTAGE_TAGGING_BINDING_INVALID")
        if not callable(authorize):
            raise ValueError("UPSTAGE_TAGGING_AUTHORIZER_REQUIRED")
        if resume is not None and not isinstance(resume, TransportResume):
            raise ValueError("UPSTAGE_TAGGING_RESUME_INVALID")
        if settings.model_profile == POSITION_P2_MODEL_PROFILE:
            self.TRANSPORT_VERSION = POSITION_P2_TRANSPORT_VERSION
        elif settings.model_profile == POSITION_TABLE_ROLE_MODEL_PROFILE:
            self.TRANSPORT_VERSION = POSITION_TABLE_ROLE_TRANSPORT_VERSION
        elif settings.model_profile == P2_MODEL_PROFILE:
            self.TRANSPORT_VERSION = P2_TRANSPORT_VERSION
        elif settings.model_profile == P1_MODEL_PROFILE:
            self.TRANSPORT_VERSION = P1_TRANSPORT_VERSION
        elif settings.model_profile == CONTEXT_MODEL_PROFILE:
            self.TRANSPORT_VERSION = CONTEXT_TRANSPORT_VERSION
        elif settings.model_profile == TABLE_MODEL_PROFILE:
            self.TRANSPORT_VERSION = TABLE_TRANSPORT_VERSION
        elif settings.model_profile == TABLE_ROLE_MODEL_PROFILE:
            self.TRANSPORT_VERSION = TABLE_ROLE_TRANSPORT_VERSION
        elif settings.model_profile == ACTOR_ROLE_MODEL_PROFILE_V2:
            self.TRANSPORT_VERSION = ACTOR_ROLE_TRANSPORT_VERSION_V2
        elif settings.model_profile == ACTOR_ROLE_MODEL_PROFILE:
            self.TRANSPORT_VERSION = ACTOR_ROLE_TRANSPORT_VERSION
        elif settings.model_profile == GOAL_ROLE_MODEL_PROFILE:
            self.TRANSPORT_VERSION = GOAL_ROLE_TRANSPORT_VERSION
        self._authorize = authorize
        self._probe, self._settings, self._tenant = probe, settings, tenant_id
        self._resume = resume
        self._receipts = Path(receipts)
        self._receipts.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._init_operation_state()

    @staticmethod
    def _probe_type():
        from proofops.adapters.local.openrouter import OpenRouterProbe

        return (UpstageProbe, OpenRouterProbe)

    def bound_context(self, packet: dict) -> dict:
        """Fit optional whole context blocks BEFORE packet hashing/authorization.

        Numbered sources are never shortened. Omitted context IDs remain in the
        frozen packet, so all replicas and receipts describe exactly the same
        bounded input. An oversized sources-only packet still fails normally.
        """
        bounded = deepcopy(packet)
        data = bounded["untrusted_document_data"]
        blocks = data.get("context_blocks", [])
        while blocks:
            wire = json.dumps(bounded, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            try:
                self._probe.request_body(
                    self._settings.rendered_system,
                    wire,
                    request_id="context-size-preflight",
                    max_tokens=self._settings.max_tokens,
                    json_mode=True,
                )
                break
            except ValueError as error:
                if str(error) != "PROBE_REQUEST_TOO_LARGE":
                    # Preserve the normal per-claim authorization/error path.
                    break
            removed = blocks.pop()
            if removed["source_id"] not in data["omitted_source_ids"]:
                data["omitted_source_ids"].append(removed["source_id"])
        return bounded

    def _wire_request(self, request: dict) -> tuple[str, str, dict[str, dict], Preflight]:
        settings = self._settings
        actor_role_v2 = settings.model_profile == ACTOR_ROLE_MODEL_PROFILE_V2
        actor_role = settings.model_profile == ACTOR_ROLE_MODEL_PROFILE or actor_role_v2
        goal_role = settings.model_profile == GOAL_ROLE_MODEL_PROFILE or actor_role
        role_table = (
            settings.model_profile
            in (
                TABLE_ROLE_MODEL_PROFILE,
                POSITION_TABLE_ROLE_MODEL_PROFILE,
                P1_MODEL_PROFILE,
                P2_MODEL_PROFILE,
                POSITION_P2_MODEL_PROFILE,
            )
            or goal_role
        )
        is_table = settings.model_profile == TABLE_MODEL_PROFILE or role_table
        is_context = settings.model_profile == CONTEXT_MODEL_PROFILE or is_table
        if is_table:
            expected_schema = TABLE_SCHEMA
            if settings.model_profile in (P2_MODEL_PROFILE, POSITION_P2_MODEL_PROFILE):
                expected_prompt = P2_SYSTEM_PROMPT
            elif settings.model_profile == P1_MODEL_PROFILE:
                expected_prompt = P1_SYSTEM_PROMPT
            elif actor_role_v2:
                expected_prompt = ACTOR_ROLE_SYSTEM_PROMPT_V2
            elif actor_role:
                expected_prompt = ACTOR_ROLE_SYSTEM_PROMPT
            elif goal_role:
                expected_prompt = GOAL_ROLE_SYSTEM_PROMPT
            elif role_table:
                expected_prompt = TABLE_ROLE_SYSTEM_PROMPT
            else:
                expected_prompt = TABLE_SYSTEM_PROMPT
            expected_keys = _TABLE_ENVELOPE_KEYS | (
                {"context_ordering"}
                if settings.model_profile
                in (POSITION_P2_MODEL_PROFILE, POSITION_TABLE_ROLE_MODEL_PROFILE)
                else set()
            )
        elif is_context:
            expected_schema, expected_prompt = CONTEXT_SCHEMA, CONTEXT_SYSTEM_PROMPT
            expected_keys = _CONTEXT_ENVELOPE_KEYS
        else:
            expected_schema, expected_prompt = SCHEMA, SYSTEM_PROMPT
            expected_keys = _ENVELOPE_KEYS
        # Mandatory callback plus common tenant/claim/request UUID, packet/
        # signature SHA, settings/binding/model/region/temperature/replica/
        # output-cap validation; raises before any spend on failure.
        authorization = self._authorize_request(request)
        if settings.system_prompt != expected_prompt:
            raise ValueError("UPSTAGE_PRELIMINARY_PROMPT_INVALID")
        system = request.get("system_prompt")
        if not isinstance(system, str) or system != settings.rendered_system:
            raise ValueError("UPSTAGE_PRELIMINARY_SYSTEM_REQUIRED")
        try:
            user = json.loads(request["user_json"])
        except (TypeError, ValueError, KeyError):
            raise ValueError("UPSTAGE_PRELIMINARY_PACKET_INVALID") from None
        if not isinstance(user, dict) or set(user) != expected_keys:
            raise ValueError("UPSTAGE_PRELIMINARY_PACKET_INVALID")
        if user["schema"] != expected_schema:
            raise ValueError("UPSTAGE_PRELIMINARY_PACKET_INVALID")
        if user.get("prompt_sha256") != canonical_hash(expected_prompt):
            raise ValueError("UPSTAGE_PRELIMINARY_PROMPT_INVALID")
        if (
            settings.model_profile in (POSITION_P2_MODEL_PROFILE, POSITION_TABLE_ROLE_MODEL_PROFILE)
            and user.get("context_ordering") != CONTEXT_POSITION_ORDER
        ):
            raise ValueError("UPSTAGE_PRELIMINARY_CONTEXT_POLICY_INVALID")
        if user.get("tenant_id") != request["tenant_id"] or user.get("claim_id") != request.get(
            "claim_id"
        ):
            raise ValueError("UPSTAGE_PRELIMINARY_PACKET_MISMATCH")
        try:
            _require_uuid("tenant_id", user["tenant_id"])
            _require_uuid("claim_id", user["claim_id"])
            for name in ("claim_sha256", "graph_sha256", "prompt_sha256"):
                _require_sha256(name, user[name])
        except (ValueError, KeyError, TypeError, AttributeError):
            raise ValueError("UPSTAGE_PRELIMINARY_PACKET_INVALID") from None
        if request.get("packet_sha256") != canonical_hash(user):
            raise ValueError("UPSTAGE_PRELIMINARY_PACKET_MISMATCH")
        data = user["untrusted_document_data"]
        expected_data_keys = (
            {"sources", "context_blocks", "omitted_source_ids"} if is_context else {"sources"}
        )
        if not isinstance(data, dict) or set(data) != expected_data_keys:
            raise ValueError("UPSTAGE_PRELIMINARY_SOURCES_REQUIRED")
        sources = data["sources"]
        if not isinstance(sources, list) or not sources:
            raise ValueError("UPSTAGE_PRELIMINARY_SOURCES_REQUIRED")
        for index, entry in enumerate(sources):
            if (
                not isinstance(entry, dict)
                or type(entry.get("source_index")) is not int
                or entry["source_index"] != index
                or not isinstance(entry.get("text"), str)
                or not entry["text"]
            ):
                raise ValueError("UPSTAGE_PRELIMINARY_SOURCES_REQUIRED")
            if set(entry) == {"source_index", "text"}:
                continue
            # Only a TABLE_SCHEMA envelope may carry table-role sources, and a
            # role source may never precede the claim's own sources at index 0.
            if not is_table or index == 0 or set(entry) != _TABLE_SOURCE_KEYS:
                raise ValueError("UPSTAGE_PRELIMINARY_SOURCES_REQUIRED")
            if (
                entry["table_role"] not in _TABLE_SOURCE_ROLES
                or entry["table_association"] not in _TABLE_ASSOCIATIONS
                or type(entry["table_row_number"]) is not int
                or entry["table_row_number"] < 0
                or type(entry["table_column_number"]) is not int
                or entry["table_column_number"] < 0
            ):
                raise ValueError("UPSTAGE_PRELIMINARY_TABLE_SOURCE_INVALID")
        if is_context:
            self._validate_context_shape(user, data, table=is_table)
        wire_user = json.dumps(user, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return system, wire_user, {}, authorization

    @staticmethod
    def _validate_context_shape(user: dict, data: dict, *, table: bool = False) -> None:
        """Structural-only guard: context_blocks can never be indexed by a
        dimension's source_index (disjoint list, disjoint index namespace)."""
        policy = user.get("context_policy")
        if not isinstance(policy, dict) or set(policy) != _CONTEXT_POLICY_KEYS:
            raise ValueError("UPSTAGE_PRELIMINARY_CONTEXT_POLICY_INVALID")
        if table:
            table_policy = user.get("table_policy")
            if (
                not isinstance(table_policy, dict)
                or set(table_policy) != _TABLE_POLICY_KEYS
                or table_policy["lineage"] not in ("resolved", "unresolved")
                or table_policy["role_basis"] != "structural_layout_interpretation"
                or table_policy["verified_sources_only"] is not True
            ):
                raise ValueError("UPSTAGE_PRELIMINARY_TABLE_POLICY_INVALID")
        allowed_roles = _CONTEXT_ROLES | (_TABLE_CONTEXT_ROLES if table else frozenset())
        blocks = data["context_blocks"]
        omitted = data["omitted_source_ids"]
        if not isinstance(blocks, list) or not isinstance(omitted, list):
            raise ValueError("UPSTAGE_PRELIMINARY_CONTEXT_INVALID")
        if any(not isinstance(item, str) or not item for item in omitted):
            raise ValueError("UPSTAGE_PRELIMINARY_CONTEXT_INVALID")
        seen_ids = set()
        for index, entry in enumerate(blocks):
            if (
                not isinstance(entry, dict)
                or set(entry) != _CONTEXT_ENTRY_KEYS
                or entry["context_index"] != index
                or entry["role"] not in allowed_roles
                or not isinstance(entry["source_id"], str)
                or not entry["source_id"]
                or entry["source_id"] in seen_ids
                or type(entry["page_num"]) is not int
                or entry["page_num"] < 1
                or entry["quality"] not in ("verified", "unverified")
                or not isinstance(entry["text"], str)
                or not entry["text"]
                or not isinstance(entry["source_ref"], dict)
            ):
                raise ValueError("UPSTAGE_PRELIMINARY_CONTEXT_INVALID")
            seen_ids.add(entry["source_id"])
