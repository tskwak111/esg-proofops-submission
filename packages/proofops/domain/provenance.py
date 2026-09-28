"""Pure, deterministic provenance identities."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from typing import Any

from proofops.domain.errors import DomainValidationError
from proofops.domain.rulepacks import canonical_json
from proofops.domain.values import _require_sha256, _require_strict_int


def canonical_hash(value: Any) -> str:
    """Stable SHA-256 of all supplied semantic content; no fields are removed."""
    return hashlib.sha256(canonical_json(value).encode("ascii")).hexdigest()


def _required_text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value:
        raise DomainValidationError(f"{name} must be a non-empty string")
    return value


def request_signature(
    *,
    temperature: float,
    model_id: str,
    model_profile: str,
    prompt_sha256: str,
    schema_sha256: str,
    packet_sha256: str,
    tools: Sequence[Mapping[str, Any]],
    max_tokens: int,
    replicate_id: int,
    extraction_epoch: int,
) -> str:
    """Hash every input that can change one model response.

    Tenant, consent, document version, and role deliberately belong to the
    cache namespace, not this provider-request signature (docs/09 §8).
    """
    if isinstance(temperature, bool) or not isinstance(temperature, int | float):
        raise DomainValidationError("temperature must be a finite number")
    if not math.isfinite(float(temperature)):
        raise DomainValidationError("temperature must be a finite number")
    _required_text("model_id", model_id)
    _required_text("model_profile", model_profile)
    _require_sha256("prompt_sha256", prompt_sha256)
    _require_sha256("schema_sha256", schema_sha256)
    _require_sha256("packet_sha256", packet_sha256)
    if not isinstance(tools, list | tuple) or any(not isinstance(tool, Mapping) for tool in tools):
        raise DomainValidationError("tools must be a sequence of mappings")
    for name, value in (
        ("max_tokens", max_tokens),
        ("replicate_id", replicate_id),
        ("extraction_epoch", extraction_epoch),
    ):
        if _require_strict_int(name, value) < 1:
            raise DomainValidationError(f"{name} must be positive")
    if replicate_id not in (1, 2, 3):
        raise DomainValidationError("replicate_id must be 1, 2, or 3")
    return canonical_hash(
        {
            "temperature": temperature,
            "model_id": model_id,
            "model_profile": model_profile,
            "prompt_sha256": prompt_sha256,
            "schema_sha256": schema_sha256,
            "packet_sha256": packet_sha256,
            "tools": list(tools),
            "max_tokens": max_tokens,
            "replicate_id": replicate_id,
            "extraction_epoch": extraction_epoch,
        }
    )


def provenance_hash(*, semantic_hash: str, actor_id: str, occurred_at: str) -> str:
    """Hash external audit metadata without altering the decision semantic hash."""
    _require_sha256("semantic_hash", semantic_hash)
    _required_text("actor_id", actor_id)
    _required_text("occurred_at", occurred_at)
    return canonical_hash(
        {
            "semantic_hash": semantic_hash,
            "actor_id": actor_id,
            "occurred_at": occurred_at,
        }
    )
