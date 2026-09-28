"""Local model adapters (TASK-000 baseline).

- SyntheticTagger: explicit local-only fake used for wiring/synthetic tests.
  Every tag it emits is marked synthetic and it never produces grades/labels.
- BedrockTagger: staging/production placeholder. P0 live calls are out of
  scope for TASK-000, so it refuses to run instead of faking results.

Real Bedrock wiring arrives in TASK-029 with preflight/binding approval.
"""

from __future__ import annotations

from typing import Any

from proofops.application.ports.models import ModelBinding
from proofops.domain.errors import ProofOpsError
from proofops.domain.values import LlmElement, LlmTags, llm_tags_from_dict


class AdapterRejectedError(ProofOpsError):
    """Raised when an adapter is not allowed in the current environment."""


class SyntheticTagger:
    """Local-only synthetic adapter. Never allowed outside local."""

    kind = "synthetic"

    def tag(self, packet: dict[str, Any], replicate_id: int, binding: ModelBinding) -> LlmTags:
        if not binding.synthetic:
            raise AdapterRejectedError("SyntheticTagger requires a synthetic binding")
        payload = {
            "claim_id": packet.get("claim_id", "00000000-0000-4000-8000-000000000004"),
            "packet_sha256": packet.get("packet_sha256", "1" * 64),
            "replicate_id": replicate_id,
            "track": packet.get("track", "goal"),
            "safe_harbor_category": None,
            "elements": [],
            "superlative_quote": None,
            "warnings": ["synthetic-local-only"],
        }
        tags = llm_tags_from_dict(payload)
        # Synthetic marker rides on warnings, never as a grade/label field.
        _ = LlmElement  # keep boundary import visible for later element support
        return tags


class BedrockTagger:
    """Production-capable adapter stub. Refuses until TASK-029 binds it."""

    kind = "bedrock"

    def tag(self, packet: dict[str, Any], replicate_id: int, binding: ModelBinding) -> LlmTags:
        raise AdapterRejectedError(
            "BedrockTagger is not bound: no approved model binding, "
            "no live calls in TASK-000 (see TASK-029 preflight)"
        )
