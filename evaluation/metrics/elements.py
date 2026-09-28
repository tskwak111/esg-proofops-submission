"""Exact element metrics for source-bound, normalized positive facts."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal


def _text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    return value


@dataclass(frozen=True, slots=True)
class ElementFact:
    claim_id: str
    element_id: str
    normalized_value: str | None
    valid_source_binding: bool

    def __post_init__(self) -> None:
        _text("claim_id", self.claim_id)
        _text("element_id", self.element_id)
        if self.normalized_value is not None:
            _text("normalized_value", self.normalized_value)
        if type(self.valid_source_binding) is not bool:
            raise ValueError("valid_source_binding must be a strict boolean")


@dataclass(frozen=True, slots=True)
class Metric:
    name: str
    value: float | None
    denominator: int
    status: Literal["scored", "not_run"] = "scored"


def element_metrics(
    predictions: Iterable[ElementFact], gold: Iterable[ElementFact]
) -> tuple[Metric, Metric]:
    """Return exact-match precision and recall without hiding their denominators."""
    predicted = tuple(predictions)
    expected = tuple(gold)
    if len(set(predicted)) != len(predicted) or len(set(expected)) != len(expected):
        raise ValueError("duplicate element fact")
    for facts in (predicted, expected):
        if len({(fact.claim_id, fact.element_id) for fact in facts}) != len(facts):
            raise ValueError("duplicate element assignment")
    matches = len(set(predicted) & set(expected))
    precision = matches / len(predicted) if predicted else None
    recall = matches / len(expected) if expected else None
    return (
        Metric("element_precision", precision, len(predicted)),
        Metric("element_recall", recall, len(expected)),
    )
