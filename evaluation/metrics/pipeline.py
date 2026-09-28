"""Pure evaluation over a fixed prediction artifact and separate gold snapshot."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from evaluation.splits.company_split import SplitReport, verify_split

from .elements import ElementFact, Metric, element_metrics

EvaluationSplit = Literal["development", "validation", "holdout"]
_SPLITS = frozenset({"development", "validation", "holdout"})
_GRADES = ("E0", "E1", "E2", "E3")
_LABELS = ("UNSUBSTANTIATED", "INCOMPLETE", "SUBSTANTIATED")
_GRADE_LABEL = {
    "E0": "UNSUBSTANTIATED",
    "E1": "INCOMPLETE",
    "E2": "INCOMPLETE",
    "E3": "SUBSTANTIATED",
}
LabelEntry = tuple[str, str | None]
_NOT_RUN_METRICS = (
    "claim_recall",
    "parsing_table_tuple_exact",
    "retrieval_recall_at_12",
    "assurance_covered_precision",
)


def _text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    return value


def _entries(
    name: str, values: Iterable[LabelEntry], allowed: tuple[str, ...]
) -> tuple[LabelEntry, ...]:
    entries = tuple(values)
    for claim_id, value in entries:
        _text("claim_id", claim_id)
        if value is not None and value not in allowed:
            raise ValueError(f"unknown {name} value")
    if len({claim_id for claim_id, _ in entries}) != len(entries):
        raise ValueError(f"duplicate claim in {name} entries")
    return tuple((claim_id, value) for claim_id, value in entries)


@dataclass(frozen=True, slots=True)
class GoldDataset:
    tenant_id: str
    dataset_id: str
    split: EvaluationSplit
    elements: tuple[ElementFact, ...]
    grades: tuple[LabelEntry, ...]
    labels: tuple[LabelEntry, ...]

    def __post_init__(self) -> None:
        _validate_snapshot(self, gold=True)


@dataclass(frozen=True, slots=True)
class PredictionSet:
    tenant_id: str
    dataset_id: str
    split: EvaluationSplit
    elements: tuple[ElementFact, ...]
    grades: tuple[LabelEntry, ...]
    labels: tuple[LabelEntry, ...]

    def __post_init__(self) -> None:
        _validate_snapshot(self, gold=False)


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    tenant_id: str
    dataset_id: str
    split: EvaluationSplit
    metrics: tuple[Metric, ...]
    ordinal_confusion: OrdinalConfusion


@dataclass(frozen=True, slots=True)
class OrdinalConfusion:
    grades: tuple[str, ...]
    matrix: tuple[tuple[int, ...], ...]
    denominator: int


def _validate_snapshot(snapshot: GoldDataset | PredictionSet, *, gold: bool) -> None:
    _text("tenant_id", snapshot.tenant_id)
    _text("dataset_id", snapshot.dataset_id)
    if snapshot.split not in _SPLITS:
        raise ValueError("unknown evaluation split")
    elements = tuple(snapshot.elements)
    if any(not isinstance(item, ElementFact) for item in elements):
        raise ValueError("elements must be ElementFact values")
    if len(set(elements)) != len(elements):
        raise ValueError("duplicate element fact")
    if len({(item.claim_id, item.element_id) for item in elements}) != len(elements):
        raise ValueError("duplicate element assignment")
    if gold and any(not item.valid_source_binding for item in elements):
        raise ValueError("gold positive element requires a verified source binding")
    grades = _entries("grade", snapshot.grades, _GRADES)
    labels = _entries("label", snapshot.labels, _LABELS)
    grade_map = dict(grades)
    label_map = dict(labels)
    if gold and {item.claim_id for item in elements} - set(grade_map):
        raise ValueError("gold element claims require explicit grade entries, including null")
    if set(grade_map) != set(label_map):
        raise ValueError("grade and label claim sets must match")
    if any(
        (grade is None) != (label_map[claim_id] is None)
        or (grade is not None and label_map[claim_id] != _GRADE_LABEL[grade])
        for claim_id, grade in grades
    ):
        raise ValueError("label must be the deterministic function of grade")
    object.__setattr__(snapshot, "elements", elements)
    object.__setattr__(snapshot, "grades", grades)
    object.__setattr__(snapshot, "labels", labels)


def _classification_metrics(
    prefix: str,
    predictions: dict[str, str | None],
    gold: dict[str, str | None],
    classes: tuple[str, ...],
) -> tuple[Metric, Metric, Metric, Metric]:
    decided = {claim_id: value for claim_id, value in gold.items() if value is not None}
    selected = {
        claim_id: expected
        for claim_id, expected in decided.items()
        if predictions.get(claim_id) is not None
    }

    def score(entries: dict[str, str], *, selective: bool) -> tuple[Metric, Metric]:
        infix = "_selective" if selective else ""
        denominator = len(entries)
        if denominator == 0:
            return (
                Metric(f"{prefix}{infix}_accuracy", None, 0),
                Metric(f"{prefix}{infix}_macro_f1", None, 0),
            )
        accuracy = (
            sum(predictions.get(key) == expected for key, expected in entries.items()) / denominator
        )
        f1_values = []
        for label in classes:
            true_positive = sum(
                predictions.get(key) == label and expected == label
                for key, expected in entries.items()
            )
            false_positive = sum(
                predictions.get(key) == label and expected != label
                for key, expected in entries.items()
            )
            false_negative = sum(
                predictions.get(key) != label and expected == label
                for key, expected in entries.items()
            )
            total = 2 * true_positive + false_positive + false_negative
            f1_values.append(2 * true_positive / total if total else 0.0)
        return (
            Metric(f"{prefix}{infix}_accuracy", accuracy, denominator),
            Metric(
                f"{prefix}{infix}_macro_f1",
                sum(f1_values) / len(classes),
                denominator,
            ),
        )

    return (*score(decided, selective=False), *score(selected, selective=True))


def _ordinal_confusion(
    predictions: dict[str, str | None], gold: dict[str, str | None]
) -> OrdinalConfusion:
    matrix = [[0 for _ in _GRADES] for _ in _GRADES]
    denominator = 0
    for claim_id, expected in gold.items():
        predicted = predictions.get(claim_id)
        if expected is None or predicted is None:
            continue
        matrix[_GRADES.index(expected)][_GRADES.index(predicted)] += 1
        denominator += 1
    return OrdinalConfusion(_GRADES, tuple(tuple(row) for row in matrix), denominator)


def evaluate_dataset(
    predictions: PredictionSet,
    gold: GoldDataset,
    *,
    split_manifest: SplitReport,
) -> EvaluationResult:
    """Evaluate exact facts and deterministic classifications without mutating either input."""
    if not isinstance(predictions, PredictionSet) or not isinstance(gold, GoldDataset):
        raise ValueError("evaluate_dataset requires PredictionSet and GoldDataset")
    if (predictions.tenant_id, predictions.dataset_id, predictions.split) != (
        gold.tenant_id,
        gold.dataset_id,
        gold.split,
    ):
        raise ValueError("prediction and gold identity mismatch")
    if not isinstance(split_manifest, SplitReport):
        raise ValueError("evaluation requires a verified split manifest")
    if split_manifest != verify_split(split_manifest.items, tenant_id=gold.tenant_id):
        raise ValueError("invalid split manifest")

    gold_grades = dict(gold.grades)
    predicted_grades = dict(predictions.grades)
    if set(predicted_grades) - set(gold_grades):
        raise ValueError("prediction contains claims outside the fixed gold dataset")
    gold_claim_ids = set(gold_grades) | {fact.claim_id for fact in gold.elements}
    manifest_claim_ids = {item.item_id for item in split_manifest.items if item.split == gold.split}
    if manifest_claim_ids != gold_claim_ids:
        raise ValueError("gold dataset does not match the verified manifest split")
    if {fact.claim_id for fact in predictions.elements} - gold_claim_ids:
        raise ValueError("prediction element claim is outside the fixed gold dataset")
    metrics = [*element_metrics(predictions.elements, gold.elements)]
    metrics.extend(_classification_metrics("grade", predicted_grades, gold_grades, _GRADES))
    gold_labels = dict(gold.labels)
    predicted_labels = dict(predictions.labels)
    metrics.extend(_classification_metrics("label", predicted_labels, gold_labels, _LABELS))
    denominator = len(gold_labels)
    decided_labels = tuple(value for value in gold_labels.values() if value is not None)
    majority = (
        max(Counter(decided_labels).values()) / len(decided_labels) if decided_labels else None
    )
    coverage = (
        sum(value is not None for value in predicted_grades.values()) / denominator
        if denominator
        else None
    )
    metrics.extend(
        (
            Metric("label_majority_baseline", majority, len(decided_labels)),
            Metric("automatic_coverage", coverage, denominator),
            *(Metric(name, None, 0, "not_run") for name in _NOT_RUN_METRICS),
        )
    )
    return EvaluationResult(
        gold.tenant_id,
        gold.dataset_id,
        gold.split,
        tuple(metrics),
        _ordinal_confusion(predicted_grades, gold_grades),
    )
