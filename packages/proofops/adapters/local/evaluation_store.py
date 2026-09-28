"""Additive evaluation schema v1; rollback disables writers and retains immutable rows.

Retention and destructive down migrations belong to the later retention task.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

_REPORT_FIELDS = frozenset(
    {
        "schema_version",
        "evaluation_id",
        "tenant_id",
        "dataset_id",
        "split",
        "status",
        "synthetic_fixture",
        "evaluator_version",
        "input_sha256",
        "metrics",
        "ordinal_confusion",
    }
)
_HEX64 = re.compile(r"[0-9a-f]{64}")


class EvaluationNotFound(LookupError):
    pass


class EvaluationConflict(ValueError):
    pass


class EvaluationCorrupt(RuntimeError):
    pass


def _canonical(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise EvaluationCorrupt("duplicate report field")
        value[key] = item
    return value


def _load_report(value: str) -> dict[str, object]:
    try:
        decoded = json.loads(
            value,
            object_pairs_hook=_object_pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise EvaluationCorrupt("evaluation report corruption") from exc
    if not isinstance(decoded, dict):
        raise EvaluationCorrupt("evaluation report corruption")
    try:
        return _validate_report(decoded)
    except ValueError as exc:
        raise EvaluationCorrupt("evaluation report corruption") from exc


def _text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError(f"{name} must be bounded non-empty text")
    return value


def _uuid(name: str, value: object) -> str:
    text = _text(name, value)
    try:
        parsed = UUID(text)
    except ValueError as exc:
        raise ValueError(f"{name} must be a UUID") from exc
    if str(parsed) != text:
        raise ValueError(f"{name} must be a canonical UUID")
    return text


def _metric(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {"name", "value", "denominator", "status"}:
        raise ValueError("invalid metric")
    name = _text("metric name", value["name"])
    metric_value = value["value"]
    if metric_value is not None and (
        isinstance(metric_value, bool)
        or not isinstance(metric_value, int | float)
        or not math.isfinite(metric_value)
    ):
        raise ValueError("metric value must be finite or null")
    denominator = value["denominator"]
    if isinstance(denominator, bool) or not isinstance(denominator, int) or denominator < 0:
        raise ValueError("metric denominator must be a non-negative integer")
    status = value["status"]
    if status not in {"scored", "not_run"}:
        raise ValueError("invalid metric status")
    if status == "not_run" and metric_value is not None:
        raise ValueError("not_run metric must be null")
    return {"name": name, "value": metric_value, "denominator": denominator, "status": status}


def _confusion(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {"grades", "matrix", "denominator"}:
        raise ValueError("invalid ordinal confusion")
    grades = value["grades"]
    matrix = value["matrix"]
    denominator = value["denominator"]
    if grades != ["E0", "E1", "E2", "E3"]:
        raise ValueError("invalid ordinal grade order")
    if not isinstance(matrix, list) or len(matrix) != 4:
        raise ValueError("invalid ordinal matrix")
    rows: list[list[int]] = []
    for row in matrix:
        if not isinstance(row, list) or len(row) != 4:
            raise ValueError("invalid ordinal matrix")
        if any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in row):
            raise ValueError("invalid ordinal matrix count")
        rows.append(list(row))
    if (
        isinstance(denominator, bool)
        or not isinstance(denominator, int)
        or denominator < 0
        or sum(map(sum, rows)) != denominator
    ):
        raise ValueError("invalid ordinal denominator")
    return {"grades": list(grades), "matrix": rows, "denominator": denominator}


def _validate_report(report: Mapping[str, object]) -> dict[str, object]:
    if set(report) != _REPORT_FIELDS:
        raise ValueError("invalid evaluation report fields")
    if report["schema_version"] != 1 or isinstance(report["schema_version"], bool):
        raise ValueError("unsupported evaluation report schema")
    evaluation_id = _uuid("evaluation_id", report["evaluation_id"])
    tenant_id = _uuid("tenant_id", report["tenant_id"])
    dataset_id = _text("dataset_id", report["dataset_id"])
    split = report["split"]
    if split not in {"development", "validation", "holdout"}:
        raise ValueError("invalid evaluation split")
    status = report["status"]
    if status not in {"queued", "completed"}:
        raise ValueError("invalid evaluation status")
    synthetic_fixture = report["synthetic_fixture"]
    if type(synthetic_fixture) is not bool:
        raise ValueError("synthetic_fixture must be a strict boolean")
    evaluator_version = _text("evaluator_version", report["evaluator_version"])
    hashes = report["input_sha256"]
    if not isinstance(hashes, dict) or set(hashes) != {"predictions", "gold", "manifest"}:
        raise ValueError("invalid evaluation input hashes")
    if any(not isinstance(item, str) or _HEX64.fullmatch(item) is None for item in hashes.values()):
        raise ValueError("invalid evaluation input hash")
    metrics = report["metrics"]
    if not isinstance(metrics, list) or len(metrics) > 256:
        raise ValueError("invalid evaluation metrics")
    normalized_metrics = [_metric(item) for item in metrics]
    if len({item["name"] for item in normalized_metrics}) != len(normalized_metrics):
        raise ValueError("duplicate evaluation metric")
    return {
        "schema_version": 1,
        "evaluation_id": evaluation_id,
        "tenant_id": tenant_id,
        "dataset_id": dataset_id,
        "split": split,
        "status": status,
        "synthetic_fixture": synthetic_fixture,
        "evaluator_version": evaluator_version,
        "input_sha256": dict(hashes),
        "metrics": normalized_metrics,
        "ordinal_confusion": _confusion(report["ordinal_confusion"]),
    }


class LocalEvaluationStore:
    """Local-only immutable evaluation artifact store; no evaluation-package import."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path == ":memory:":
            raise ValueError("durable evaluation storage requires a file")
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS evaluation_schema (version INTEGER PRIMARY KEY)"
            )
            versions = connection.execute("SELECT version FROM evaluation_schema").fetchall()
            if versions and versions != [(1,)]:
                raise RuntimeError("unsupported local evaluation schema")
            connection.execute("INSERT OR IGNORE INTO evaluation_schema VALUES (1)")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS evaluation_artifacts (
                    tenant_id TEXT NOT NULL,
                    evaluation_id TEXT NOT NULL,
                    dataset_id TEXT NOT NULL,
                    split TEXT NOT NULL,
                    status TEXT NOT NULL,
                    report_json TEXT NOT NULL,
                    report_sha256 TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, evaluation_id)
                )"""
            )
            for operation in ("UPDATE", "DELETE"):
                connection.execute(
                    f"""CREATE TRIGGER IF NOT EXISTS evaluation_artifacts_no_{operation.lower()}
                    BEFORE {operation} ON evaluation_artifacts BEGIN
                    SELECT RAISE(ABORT, 'immutable evaluation artifact'); END"""
                )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def insert(self, report: Mapping[str, object]) -> dict[str, object]:
        normalized = _validate_report(report)
        report_json = _canonical(normalized)
        report_sha256 = hashlib.sha256(report_json.encode()).hexdigest()
        with self._transaction() as connection:
            try:
                connection.execute(
                    "INSERT INTO evaluation_artifacts VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        normalized["tenant_id"],
                        normalized["evaluation_id"],
                        normalized["dataset_id"],
                        normalized["split"],
                        normalized["status"],
                        report_json,
                        report_sha256,
                    ),
                )
            except sqlite3.IntegrityError:
                row = connection.execute(
                    """SELECT report_json, report_sha256 FROM evaluation_artifacts
                    WHERE tenant_id=? AND evaluation_id=?""",
                    (normalized["tenant_id"], normalized["evaluation_id"]),
                ).fetchone()
                if row != (report_json, report_sha256):
                    raise EvaluationConflict("immutable evaluation cannot be overwritten") from None
        return {**normalized, "report_sha256": report_sha256}

    def get(self, tenant_id: str, evaluation_id: str) -> dict[str, object]:
        tenant = _uuid("tenant_id", tenant_id)
        identifier = _uuid("evaluation_id", evaluation_id)
        with sqlite3.connect(self.path, timeout=10) as connection:
            row = connection.execute(
                """SELECT dataset_id, split, status, report_json, report_sha256
                FROM evaluation_artifacts WHERE tenant_id=? AND evaluation_id=?""",
                (tenant, identifier),
            ).fetchone()
        if row is None:
            raise EvaluationNotFound("evaluation not found")
        report = _load_report(row[3])
        canonical = _canonical(report)
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        if (
            digest != row[4]
            or (report["tenant_id"], report["evaluation_id"]) != (tenant, identifier)
            or (report["dataset_id"], report["split"], report["status"]) != row[:3]
        ):
            raise EvaluationCorrupt("evaluation report corruption")
        return {**report, "report_sha256": digest}
