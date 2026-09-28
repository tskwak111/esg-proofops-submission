"""Callable ports preserved by the reconciliation 1.1 integration contract."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol


class SourceReader(Protocol):
    def __call__(self, ref: Mapping[str, Any]) -> bytes: ...


class ExplanationSearch(Protocol):
    def __call__(self, packet: Mapping[str, Any]) -> list[dict[str, Any]]: ...
