"""TASK-032 local CLI -> immutable SQLite -> admin-only HTTP read."""

from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from proofops.adapters.local.auth_store import InMemoryMembershipStore, InMemorySessionStore
from proofops.application.authorization import MembershipRecord, SessionRecord
from proofops_api.auth import AuthStore

from tests.integration.test_run_lifecycle import validate

ROOT = Path(__file__).parents[2]
TENANT = "11111111-1111-4111-8111-111111111111"
FOREIGN = "22222222-2222-4222-8222-222222222222"


def test_cli_bounds_read_when_file_grows_after_metadata_check(tmp_path, monkeypatch):
    from evaluation.cli import MAX_INPUT_BYTES, InputRejected, _load

    path = tmp_path / "growing.json"
    path.write_text("{}")

    class GrowingFile(io.BytesIO):
        def read(self, size=-1):
            assert 0 <= size <= MAX_INPUT_BYTES + 1, "unbounded input read"
            return super().read(size)

    monkeypatch.setattr(Path, "open", lambda *_a, **_k: GrowingFile(b" " * (MAX_INPUT_BYTES + 2)))
    with pytest.raises(InputRejected):
        _load(path)


def _inputs(directory: Path) -> tuple[Path, Path, Path]:
    predictions = {
        "tenant_id": TENANT,
        "dataset_id": "synthetic-holdout-v1",
        "split": "holdout",
        "elements": [
            {
                "claim_id": "c1",
                "element_id": "P1",
                "normalized_value": "10 tCO2e",
                "valid_source_binding": False,
            }
        ],
        "grades": [["c1", "E1"], ["c2", None]],
        "labels": [["c1", "INCOMPLETE"], ["c2", None]],
    }
    gold = {
        "tenant_id": TENANT,
        "dataset_id": "synthetic-holdout-v1",
        "split": "holdout",
        "elements": [
            {
                "claim_id": "c1",
                "element_id": "P1",
                "normalized_value": "10 tCO2e",
                "valid_source_binding": True,
            }
        ],
        "grades": [["c1", "E1"], ["c2", None]],
        "labels": [["c1", "INCOMPLETE"], ["c2", None]],
    }
    manifest = {
        "tenant_id": TENANT,
        "items": [
            {
                "tenant_id": TENANT,
                "item_id": claim_id,
                "company_id": f"company-{claim_id}",
                "reporting_period": "2025",
                "split": "holdout",
                "origin": "independent_gold",
            }
            for claim_id in ("c1", "c2")
        ],
    }
    paths = tuple(directory / name for name in ("predictions.json", "gold.json", "manifest.json"))
    for path, payload in zip(paths, (predictions, gold, manifest), strict=True):
        path.write_text(json.dumps(payload), encoding="utf-8")
    return paths


def _run_cli(database: Path, inputs: tuple[Path, Path, Path], *extra: str):
    environment = {**os.environ, "LOCAL_DATABASE_PATH": str(database)}
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "evaluation.cli",
            "--predictions",
            str(inputs[0]),
            "--gold",
            str(inputs[1]),
            "--manifest",
            str(inputs[2]),
            *extra,
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def _client(store, *, tenant_id: str = TENANT, role: str = "admin") -> TestClient:
    from proofops_api.routers.evaluations import build_evaluations_router

    auth = AuthStore(InMemorySessionStore(), InMemoryMembershipStore())
    expires = time.time() + 10_000
    auth.sessions.put_with_token(
        SessionRecord(
            "evaluation-session",
            "evaluation-user",
            tenant_id,
            auth.hash_csrf("unused"),
            expires,
            expires,
            False,
        ),
        "unused",
    )
    auth.memberships.put(MembershipRecord(tenant_id, "evaluation-user", role, "active"))
    app = FastAPI()
    app.include_router(build_evaluations_router(store, auth, clock=lambda: time.time()))
    client = TestClient(app)
    client.cookies.set("__Host-proofops_session", "evaluation-session")
    return client


def test_cli_creates_reopenable_immutable_report_and_api_projects_fixed_dto(tmp_path: Path):
    from proofops.adapters.local.evaluation_store import LocalEvaluationStore

    database = tmp_path / "state.sqlite3"
    inputs = _inputs(tmp_path)
    completed = _run_cli(database, inputs, "--synthetic-fixture")
    assert completed.returncode == 0, completed.stderr
    receipt = json.loads(completed.stdout)
    store = LocalEvaluationStore(database)
    rich = store.get(TENANT, receipt["evaluation_id"])

    assert rich["status"] == "completed"
    assert rich["synthetic_fixture"] is True
    assert rich["evaluator_version"] == "task-032-local-v1"
    assert set(rich["input_sha256"]) == {"predictions", "gold", "manifest"}
    for name, path in zip(("predictions", "gold", "manifest"), inputs, strict=True):
        assert rich["input_sha256"][name] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert rich["report_sha256"] == receipt["report_sha256"]
    assert rich["ordinal_confusion"]["denominator"] == 1
    assert any(metric["status"] == "not_run" for metric in rich["metrics"])
    assert "10 tCO2e" not in json.dumps(rich)

    response = _client(store).get(f"/v1/evaluations/{receipt['evaluation_id']}")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    validate("Evaluation", response.json())
    assert response.json() == {
        "evaluation_id": receipt["evaluation_id"],
        "dataset_id": "synthetic-holdout-v1",
        "split": "holdout",
        "status": "completed",
        "metrics": [
            {
                "name": metric["name"],
                "value": metric["value"],
                "denominator": metric["denominator"],
            }
            for metric in rich["metrics"]
        ],
        "report_sha256": receipt["report_sha256"],
    }
    assert (
        _client(store, role="viewer").get(f"/v1/evaluations/{receipt['evaluation_id']}").status_code
        == 403
    )
    assert (
        _client(store, tenant_id=FOREIGN)
        .get(f"/v1/evaluations/{receipt['evaluation_id']}")
        .status_code
        == 404
    )
    assert (
        _client(store)
        .post(f"/v1/evaluations/{receipt['evaluation_id']}", json={"gold": "forbidden"})
        .status_code
        == 405
    )

    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name='local_tag_cache_v1'"
            ).fetchone()
            is None
        )
        with pytest.raises(sqlite3.IntegrityError, match="immutable evaluation artifact"):
            connection.execute(
                "UPDATE evaluation_artifacts SET report_json='{}' WHERE evaluation_id=?",
                (receipt["evaluation_id"],),
            )

    limited = _client(store)
    path = f"/v1/evaluations/{receipt['evaluation_id']}"
    for _ in range(120):
        assert limited.get(path).status_code == 200
    assert limited.get(path).status_code == 429


def test_hash_corruption_returns_safe_conflict_without_report_data(tmp_path: Path):
    from proofops.adapters.local.evaluation_store import LocalEvaluationStore

    database = tmp_path / "state.sqlite3"
    completed = _run_cli(database, _inputs(tmp_path), "--synthetic-fixture")
    assert completed.returncode == 0, completed.stderr
    receipt = json.loads(completed.stdout)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER evaluation_artifacts_no_update")
        connection.execute(
            "UPDATE evaluation_artifacts SET report_json='{}' WHERE evaluation_id=?",
            (receipt["evaluation_id"],),
        )

    response = _client(LocalEvaluationStore(database)).get(
        f"/v1/evaluations/{receipt['evaluation_id']}"
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "EVALUATION_CORRUPT"
    assert "10 tCO2e" not in response.text


@pytest.mark.parametrize(
    "failure",
    [
        "unknown",
        "nonfinite",
        "type",
        "tenant_identity",
        "dataset_identity",
        "split_identity",
        "claim_identity",
        "oversize",
    ],
)
def test_cli_rejects_untrusted_json_without_persisting_an_evaluation(tmp_path: Path, failure: str):
    inputs = _inputs(tmp_path)
    if failure == "unknown":
        payload = json.loads(inputs[0].read_text())
        payload["unexpected"] = True
        inputs[0].write_text(json.dumps(payload))
    elif failure == "nonfinite":
        inputs[0].write_text(inputs[0].read_text().replace("null", "NaN", 1))
    elif failure == "type":
        payload = json.loads(inputs[0].read_text())
        payload["elements"][0]["valid_source_binding"] = 1
        inputs[0].write_text(json.dumps(payload))
    elif failure == "tenant_identity":
        payload = json.loads(inputs[0].read_text())
        payload["tenant_id"] = FOREIGN
        inputs[0].write_text(json.dumps(payload))
    elif failure == "dataset_identity":
        payload = json.loads(inputs[0].read_text())
        payload["dataset_id"] = "other-dataset"
        inputs[0].write_text(json.dumps(payload))
    elif failure == "split_identity":
        payload = json.loads(inputs[0].read_text())
        payload["split"] = "validation"
        inputs[0].write_text(json.dumps(payload))
    elif failure == "claim_identity":
        payload = json.loads(inputs[0].read_text())
        payload["elements"][0]["claim_id"] = "outside-dataset"
        inputs[0].write_text(json.dumps(payload))
    else:
        inputs[0].write_bytes(b"{" + b" " * (1024 * 1024) + b"}")

    rejected = _run_cli(tmp_path / "state.sqlite3", inputs, "--synthetic-fixture")

    assert rejected.returncode != 0
    assert rejected.stdout == ""
    assert "evaluation input rejected:" in rejected.stderr
    database = tmp_path / "state.sqlite3"
    if database.exists():
        with sqlite3.connect(database) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert (
                "evaluation_artifacts" not in tables
                or connection.execute("SELECT COUNT(*) FROM evaluation_artifacts").fetchone()[0]
                == 0
            )


def test_store_rejects_future_schema_without_touching_other_tables(tmp_path: Path):
    from proofops.adapters.local.evaluation_store import LocalEvaluationStore

    database = tmp_path / "future.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE unrelated (value TEXT)")
        connection.execute("CREATE TABLE evaluation_schema (version INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO evaluation_schema VALUES (999)")

    with pytest.raises(RuntimeError, match="unsupported local evaluation schema"):
        LocalEvaluationStore(database)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='unrelated'"
        ).fetchone() == ("unrelated",)
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name='evaluation_artifacts'"
            ).fetchone()
            is None
        )
