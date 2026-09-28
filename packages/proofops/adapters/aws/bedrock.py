"""Guarded SDK dispatch for TASK-029; no SDK creation or credentials lookup.

Implements application.preflight.ModelInvocationPort. Composition supplies an
AWS SDK client and its verified caller account ID. This module never chooses
an alternative model, endpoint, region, or transport after failure. Payload
construction, token accounting and tag validation belong to tagging tasks.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from proofops.application.authorization import AuthContext
from proofops.application.preflight import PreflightBlocked, check_runtime_binding
from proofops.domain.rulepacks import canonical_json


class BedrockClient(Protocol):
    meta: Any

    def invoke_model(self, **kwargs: Any) -> dict[str, Any]: ...


class BedrockInvoker:
    def __init__(self, client: BedrockClient, *, account_id: str):
        self._client = client
        self._account_id = account_id

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
    ) -> dict[str, Any]:
        # One snapshot feeds both authorization and SDK arguments: callers
        # cannot mutate a target after it was checked and redirect this call.
        runtime = json.loads(canonical_json(dict(binding)))
        profile = json.loads(canonical_json(dict(consent)))
        result = check_runtime_binding(
            binding=runtime,
            consent=profile,
            auth=auth,
            allowed_regions=allowed_regions,
            checked_at=checked_at,
        )
        if not result.ready:
            raise PreflightBlocked("preflight failed; external transmission blocked")
        if document_rights not in profile["allowed_document_rights"]:
            raise PreflightBlocked("document rights not approved")
        region = runtime["endpoint_region"]
        meta = self._client.meta
        if (
            self._account_id != runtime["account_id"]
            or meta.region_name != region
            or meta.endpoint_url != f"https://bedrock-runtime.{region}.amazonaws.com"
        ):
            raise PreflightBlocked("REGION_DENIED: SDK account or endpoint differs from binding")
        if not isinstance(body, bytes):
            raise PreflightBlocked("model request body must be JSON bytes")
        try:
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ValueError("object required")
            canonical_json(payload)  # rejects non-finite JSON values
        except (ValueError, UnicodeError):
            raise PreflightBlocked("invalid model request JSON") from None
        try:
            return self._client.invoke_model(
                modelId=runtime.get("inference_profile_arn") or runtime["model_id"],
                body=body,
                contentType="application/json",
                accept="application/json",
                trace="DISABLED",
            )
        except Exception:
            # SDK exception messages can contain request content. Preserve
            # neither message nor implicit exception chain in public failures.
            raise PreflightBlocked("MODEL_UNAVAILABLE: no automatic fallback") from None
