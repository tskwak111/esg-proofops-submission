"""Model ports. Pure protocols; no I/O."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from proofops.domain.values import LlmTags


@dataclass(frozen=True, slots=True)
class ModelBinding:
    """Minimal binding descriptor. Real account bindings arrive via config."""

    binding_id: str
    role: str
    synthetic: bool


class TaggerPort(Protocol):
    """Boundary every model adapter must implement."""

    kind: str

    def tag(self, packet: dict[str, Any], replicate_id: int, binding: ModelBinding) -> LlmTags: ...
