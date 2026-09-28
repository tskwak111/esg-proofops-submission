"""Versioned Upstage transport for literal source-role relation tagging."""

from __future__ import annotations

import json

from proofops.application.preflight import Preflight
from proofops.application.tagging.relations import SCHEMA, SYSTEM_PROMPT
from proofops.domain.provenance import canonical_hash
from proofops.domain.values import _require_sha256, _require_uuid

from proofops_agent.upstage_tagging import UpstageTaggingTransport

MODEL_PROFILE = "upstage-relation-source-quotes-v1"
TRANSPORT_VERSION = "relation-source-quotes-v1"

_ENVELOPE_KEYS = frozenset(
    (
        "schema",
        "tenant_id",
        "claim_id",
        "graph_sha256",
        "sources_sha256",
        "retrieval_packet_sha256",
        "prompt_sha256",
        "untrusted_document_data",
    )
)


class UpstageRelationsTransport(UpstageTaggingTransport):
    """Literal relation transport; inherited receipts preserve raw relations."""

    MODEL_PROFILE = MODEL_PROFILE
    TRANSPORT_VERSION = TRANSPORT_VERSION

    def _wire_request(self, request: dict) -> tuple[str, str, dict[str, dict], Preflight]:
        settings = self._settings
        if settings.system_prompt != SYSTEM_PROMPT:
            raise ValueError("UPSTAGE_RELATIONS_PROMPT_INVALID")
        authorization = self._authorize_request(request)
        system = request.get("system_prompt")
        if not isinstance(system, str) or system != settings.rendered_system:
            raise ValueError("UPSTAGE_RELATIONS_SYSTEM_REQUIRED")
        try:
            user = json.loads(request["user_json"])
        except (TypeError, ValueError, KeyError):
            raise ValueError("UPSTAGE_RELATIONS_PACKET_INVALID") from None
        if not isinstance(user, dict) or set(user) != _ENVELOPE_KEYS or user["schema"] != SCHEMA:
            raise ValueError("UPSTAGE_RELATIONS_PACKET_INVALID")
        if (
            user.get("tenant_id") != request["tenant_id"]
            or user.get("claim_id") != request.get("claim_id")
            or user.get("retrieval_packet_sha256") != request.get("retrieval_packet_sha256")
            or user.get("prompt_sha256") != canonical_hash(SYSTEM_PROMPT)
        ):
            raise ValueError("UPSTAGE_RELATIONS_PACKET_MISMATCH")
        try:
            _require_uuid("tenant_id", user["tenant_id"])
            _require_uuid("claim_id", user["claim_id"])
            for name in (
                "graph_sha256",
                "sources_sha256",
                "retrieval_packet_sha256",
                "prompt_sha256",
            ):
                _require_sha256(name, user[name])
        except (ValueError, KeyError, TypeError, AttributeError):
            raise ValueError("UPSTAGE_RELATIONS_PACKET_INVALID") from None
        if request.get("packet_sha256") != canonical_hash(user):
            raise ValueError("UPSTAGE_RELATIONS_PACKET_MISMATCH")
        data = user["untrusted_document_data"]
        if not isinstance(data, dict) or set(data) != {"sources"}:
            raise ValueError("UPSTAGE_RELATIONS_SOURCES_REQUIRED")
        sources = data["sources"]
        if not isinstance(sources, list) or not sources:
            raise ValueError("UPSTAGE_RELATIONS_SOURCES_REQUIRED")
        for index, source in enumerate(sources):
            if (
                not isinstance(source, dict)
                or set(source) != {"source_index", "text"}
                or type(source["source_index"]) is not int
                or source["source_index"] != index
                or not isinstance(source["text"], str)
                or not source["text"]
            ):
                raise ValueError("UPSTAGE_RELATIONS_SOURCES_REQUIRED")
        return (
            system,
            json.dumps(user, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            {},
            authorization,
        )
