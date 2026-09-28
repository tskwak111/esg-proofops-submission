"""Run the pure evaluator from three bounded local JSON snapshots."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from dataclasses import asdict
from pathlib import Path
from uuid import UUID, uuid4

from proofops.adapters.local.evaluation_store import EvaluationConflict, LocalEvaluationStore

from evaluation.metrics.elements import ElementFact
from evaluation.metrics.pipeline import GoldDataset, PredictionSet, evaluate_dataset
from evaluation.splits.company_split import DatasetItem, SplitReport, verify_split

MAX_INPUT_BYTES = 1024 * 1024
EVALUATOR_VERSION = "task-032-local-v1"
_SNAPSHOT_FIELDS = frozenset({"tenant_id", "dataset_id", "split", "elements", "grades", "labels"})
_ELEMENT_FIELDS = frozenset({"claim_id", "element_id", "normalized_value", "valid_source_binding"})
_MANIFEST_FIELDS = frozenset({"tenant_id", "items"})
_ITEM_FIELDS = frozenset(
    {"tenant_id", "item_id", "company_id", "reporting_period", "split", "origin"}
)


class InputRejected(ValueError):
    pass


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise InputRejected("duplicate JSON field")
        result[key] = value
    return result


def _constant(_value: str) -> object:
    raise InputRejected("non-finite JSON number")


def _load(path: Path) -> tuple[dict[str, object], str]:
    try:
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_INPUT_BYTES:
            raise InputRejected("JSON input must be a bounded regular file")
        with path.open("rb") as source:
            raw = source.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES or len(raw) != metadata.st_size:
            raise InputRejected("JSON file changed while reading")
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InputRejected("invalid JSON file") from exc
    if not isinstance(value, dict):
        raise InputRejected("JSON root must be an object")
    return value, hashlib.sha256(raw).hexdigest()


def _exact(value: object, fields: frozenset[str], name: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != fields:
        raise InputRejected(f"invalid {name} fields")
    return value


def _text(value: object, name: str, *, limit: int = 10_000) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise InputRejected(f"invalid {name}")
    return value


def _uuid(value: object, name: str) -> str:
    text = _text(value, name, limit=36)
    try:
        parsed = UUID(text)
    except ValueError as exc:
        raise InputRejected(f"invalid {name}") from exc
    if str(parsed) != text:
        raise InputRejected(f"invalid {name}")
    return text


def _entries(value: object, name: str) -> tuple[tuple[str, str | None], ...]:
    if not isinstance(value, list):
        raise InputRejected(f"invalid {name}")
    entries = []
    for entry in value:
        if not isinstance(entry, list) or len(entry) != 2:
            raise InputRejected(f"invalid {name} entry")
        claim_id = _text(entry[0], "claim_id", limit=512)
        result = entry[1]
        if result is not None and not isinstance(result, str):
            raise InputRejected(f"invalid {name} value")
        entries.append((claim_id, result))
    return tuple(entries)


def _elements(value: object) -> tuple[ElementFact, ...]:
    if not isinstance(value, list):
        raise InputRejected("invalid elements")
    elements = []
    for raw in value:
        item = _exact(raw, _ELEMENT_FIELDS, "element")
        normalized = item["normalized_value"]
        if normalized is not None:
            normalized = _text(normalized, "normalized_value")
        elements.append(
            ElementFact(
                _text(item["claim_id"], "claim_id", limit=512),
                _text(item["element_id"], "element_id", limit=512),
                normalized,
                item["valid_source_binding"],  # type: ignore[arg-type]
            )
        )
    return tuple(elements)


def _snapshot(value: object, kind: type[GoldDataset] | type[PredictionSet]):
    item = _exact(value, _SNAPSHOT_FIELDS, "snapshot")
    return kind(
        _uuid(item["tenant_id"], "tenant_id"),
        _text(item["dataset_id"], "dataset_id", limit=512),
        item["split"],  # type: ignore[arg-type]
        _elements(item["elements"]),
        _entries(item["grades"], "grades"),
        _entries(item["labels"], "labels"),
    )


def _manifest(value: object) -> SplitReport:
    manifest = _exact(value, _MANIFEST_FIELDS, "manifest")
    tenant_id = _uuid(manifest["tenant_id"], "tenant_id")
    raw_items = manifest["items"]
    if not isinstance(raw_items, list):
        raise InputRejected("invalid manifest items")
    items = []
    for raw in raw_items:
        item = _exact(raw, _ITEM_FIELDS, "manifest item")
        items.append(
            DatasetItem(
                _uuid(item["tenant_id"], "tenant_id"),
                _text(item["item_id"], "item_id", limit=512),
                _text(item["company_id"], "company_id", limit=512),
                _text(item["reporting_period"], "reporting_period", limit=128),
                item["split"],  # type: ignore[arg-type]
                _text(item["origin"], "origin", limit=512),
            )
        )
    return verify_split(items, tenant_id=tenant_id)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate fixed local JSON snapshots")
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--evaluation-id")
    parser.add_argument("--synthetic-fixture", action="store_true")
    return parser


def run(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        prediction_json, prediction_hash = _load(args.predictions)
        gold_json, gold_hash = _load(args.gold)
        manifest_json, manifest_hash = _load(args.manifest)
        predictions = _snapshot(prediction_json, PredictionSet)
        gold = _snapshot(gold_json, GoldDataset)
        manifest = _manifest(manifest_json)
        result = evaluate_dataset(predictions, gold, split_manifest=manifest)
        evaluation_id = (
            _uuid(args.evaluation_id, "evaluation_id")
            if args.evaluation_id is not None
            else str(uuid4())
        )
        report = {
            "schema_version": 1,
            "evaluation_id": evaluation_id,
            "tenant_id": result.tenant_id,
            "dataset_id": result.dataset_id,
            "split": result.split,
            "status": "completed",
            "synthetic_fixture": args.synthetic_fixture,
            "evaluator_version": EVALUATOR_VERSION,
            "input_sha256": {
                "predictions": prediction_hash,
                "gold": gold_hash,
                "manifest": manifest_hash,
            },
            "metrics": [asdict(metric) for metric in result.metrics],
            "ordinal_confusion": {
                "grades": list(result.ordinal_confusion.grades),
                "matrix": [list(row) for row in result.ordinal_confusion.matrix],
                "denominator": result.ordinal_confusion.denominator,
            },
        }
        stored = LocalEvaluationStore(
            Path(os.environ.get("LOCAL_DATABASE_PATH", ".local/state.sqlite3"))
        ).insert(report)
    except (EvaluationConflict, InputRejected, OSError, TypeError, ValueError) as exc:
        print(f"evaluation input rejected: {type(exc).__name__}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "evaluation_id": stored["evaluation_id"],
                "report_sha256": stored["report_sha256"],
                "status": stored["status"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
