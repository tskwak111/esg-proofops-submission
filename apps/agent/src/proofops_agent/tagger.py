"""Agent tagging boundary (TASK-000 baseline).

Validates tagger output through the domain boundary. Never grades:
no evidence_grade/label handling exists on this path by construction.
"""

from __future__ import annotations

from typing import Any

from proofops.domain.values import LlmTags, llm_tags_from_dict


def validate_tags(payload: dict[str, Any]) -> LlmTags:
    return llm_tags_from_dict(payload)


class BedrockMessagesTagger:
    """Explicit Anthropic Messages format through TASK-029's preflight transport.

    Composition selects a compatible, approved runtime binding. This adapter
    creates no SDK client, selects no model, enables no tools and never falls
    back. synthetic=True is required for synthetic SDK fixtures, never inferred.
    """

    def __init__(
        self,
        invoker,
        *,
        auth,
        runtime_binding,
        consent,
        allowed_regions,
        document_rights,
        checked_at,
        synthetic: bool,
    ):
        import json

        from proofops.domain.rulepacks import canonical_json

        if type(synthetic) is not bool:
            raise ValueError("explicit synthetic marker required")
        self._invoker = invoker
        self._auth = auth
        self._runtime_json = canonical_json(runtime_binding)
        self._consent_json = canonical_json(consent)
        self._regions = tuple(allowed_regions)
        self._rights = document_rights
        self._checked_at = checked_at
        self.synthetic = synthetic
        # Validate JSON snapshots eagerly without retaining mutable policy dicts.
        json.loads(self._runtime_json)

    def invoke(self, request: dict[str, Any]):
        import json
        from time import monotonic_ns

        from proofops.application.budget import TokenUsage
        from proofops.application.preflight import PreflightBlocked, check_runtime_binding
        from proofops.application.tagging.service import RawTagResponse
        from proofops.domain.rulepacks import canonical_json

        runtime, consent = json.loads(self._runtime_json), json.loads(self._consent_json)
        if not check_runtime_binding(
            binding=runtime,
            consent=consent,
            auth=self._auth,
            allowed_regions=self._regions,
            checked_at=self._checked_at,
        ).ready:
            raise PreflightBlocked("unapproved model binding")
        if (
            runtime["role"] != "tagger"
            or request["tenant_id"] != self._auth.tenant_id
            or request["binding"]
            != dict(
                binding_id=runtime["runtime_binding_id"], role="tagger", synthetic=self.synthetic
            )
            or (request["model_id"], request["region"])
            != (runtime["model_id"], runtime["endpoint_region"])
        ):
            raise PreflightBlocked("request/runtime identity mismatch")
        if (
            type(request["max_tokens"]) is not int
            or not 1 <= request["max_tokens"] <= runtime["max_output_tokens"]
        ):
            raise PreflightBlocked("output token limit exceeds binding")
        body = canonical_json(
            dict(
                anthropic_version="bedrock-2023-05-31",
                max_tokens=request["max_tokens"],
                temperature=request["temperature"],
                system=request["system_prompt"],
                messages=[
                    dict(role="user", content=[dict(type="text", text=request["user_json"])])
                ],
            )
        ).encode()
        started = monotonic_ns()
        response = self._invoker.invoke(
            body=body,
            auth=self._auth,
            binding=runtime,
            consent=consent,
            allowed_regions=self._regions,
            document_rights=self._rights,
            checked_at=self._checked_at,
        )
        payload = response["body"]
        if hasattr(payload, "read"):
            try:
                payload = payload.read(1_048_577)
            finally:
                payload_stream = response["body"]
                if hasattr(payload_stream, "close"):
                    payload_stream.close()
        if not isinstance(payload, bytes) or len(payload) > 1_048_576:
            raise ValueError("invalid or oversized provider response")
        raw_provider = payload.decode("utf-8")
        try:
            decoded = json.loads(raw_provider)
            if not isinstance(decoded, dict):
                raise ValueError("provider object required")
            content = decoded.get("content", [])
            if not isinstance(content, list):
                raise ValueError("provider content array required")
            valid_content = bool(content) and all(
                isinstance(part, dict)
                and part.get("type") == "text"
                and isinstance(part.get("text"), str)
                for part in content
            )
            raw = "".join(part["text"] for part in content) if valid_content else None
            complete = valid_content and decoded.get("stop_reason") == "end_turn"
            native_usage = decoded.get("usage", {})
            if not isinstance(native_usage, dict):
                raise ValueError("provider usage object required")
            # No prompt-cache directives are emitted. Unknown cache accounting remains
            # unknown rather than guessed when a provider returns cache-specific usage.
            cache_fields = any("cache" in key for key in native_usage)
            usage = TokenUsage(
                native_usage.get("input_tokens") if not cache_fields else None,
                native_usage.get("output_tokens"),
                0 if not cache_fields else None,
                0 if not cache_fields else None,
                (monotonic_ns() - started) // 1_000_000,
                "succeeded" if complete else "failed",
                decoded.get("id"),
                None if complete else "MODEL_OUTPUT_INCOMPLETE",
            )
            return RawTagResponse(raw, usage, self.synthetic, raw_provider)
        except ValueError:
            return RawTagResponse(
                None,
                TokenUsage(
                    None,
                    None,
                    None,
                    None,
                    (monotonic_ns() - started) // 1_000_000,
                    "failed",
                    None,
                    "MODEL_RESPONSE_INVALID",
                ),
                self.synthetic,
                raw_provider,
            )
