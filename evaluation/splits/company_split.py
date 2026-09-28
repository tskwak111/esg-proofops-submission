"""Company-level dataset split checks for local evaluation catalogs."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

SplitName = Literal["silver", "development", "fewshot", "validation", "holdout"]
SPLITS: tuple[SplitName, ...] = (
    "silver",
    "development",
    "fewshot",
    "validation",
    "holdout",
)


def _text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    return value


@dataclass(frozen=True, slots=True)
class DatasetItem:
    tenant_id: str
    item_id: str
    company_id: str
    reporting_period: str
    split: SplitName
    origin: str

    def __post_init__(self) -> None:
        for name in ("tenant_id", "item_id", "company_id", "reporting_period", "origin"):
            _text(name, getattr(self, name))
        if self.split not in SPLITS:
            raise ValueError("unknown dataset split")


@dataclass(frozen=True, slots=True)
class SplitReport:
    tenant_id: str
    item_counts: tuple[tuple[SplitName, int], ...]
    company_counts: tuple[tuple[SplitName, int], ...]
    items: tuple[DatasetItem, ...]


def verify_split(items: Iterable[DatasetItem], *, tenant_id: str) -> SplitReport:
    """Fail closed on tenant, company, duplicate, or review-to-holdout leakage."""
    _text("tenant_id", tenant_id)
    records = tuple(items)
    if any(not isinstance(item, DatasetItem) for item in records):
        raise ValueError("split entries must be DatasetItem values")
    if any(item.tenant_id != tenant_id for item in records):
        raise ValueError("dataset split tenant mismatch")
    if len({item.item_id for item in records}) != len(records):
        raise ValueError("duplicate item_id in dataset split")
    if any(item.origin == "review_confirmed" and item.split != "fewshot" for item in records):
        raise ValueError("review-confirmed data may only enter the fewshot split")

    company_splits: dict[str, set[SplitName]] = defaultdict(set)
    for item in records:
        company_splits[item.company_id].add(item.split)
    if any(len(splits) != 1 for splits in company_splits.values()):
        raise ValueError("company appears in multiple splits")

    item_counts = Counter(item.split for item in records)
    company_counts = Counter(next(iter(splits)) for splits in company_splits.values())
    return SplitReport(
        tenant_id,
        tuple((split, item_counts[split]) for split in SPLITS),
        tuple((split, company_counts[split]) for split in SPLITS),
        records,
    )
