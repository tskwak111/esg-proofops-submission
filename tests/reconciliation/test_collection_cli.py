import hashlib
import json
from pathlib import Path

import jsonschema
from proofops.adapters.dart import ArtifactStore, DartClient

from evaluation.reconciliation_collect import collect, main


def test_pinned_collection_preserves_wire_bytes(tmp_path: Path):
    raw = (
        b'{ "status": "000", "list": [{"corp_code":"00126380", "bsns_year":"2024", '
        b'"reprt_code":"11011", "rcept_no":"20250314000123", "fs_div":"CFS"}] }'
    )
    client = DartClient(api_key="offline", transport=lambda *a, **kw: (200, {}, raw))
    store = ArtifactStore(tmp_path)
    manifest = collect(
        client,
        store,
        corp_code="00126380",
        fy=2024,
        rcept_no="20250314000123",
        package_id="pkg",
        manifest_id="m",
        document_version_id="v1",
        kinds=["statements"],
        synthetic=True,
    )
    entry = manifest["artifacts"][0]
    assert entry["status"] == "retrieved"
    assert entry["artifact_sha256"] == hashlib.sha256(raw).hexdigest()
    assert store.get(entry["artifact_sha256"], ext="json") == raw
    schema = json.loads(
        Path("contracts/reconciliation/collection_manifest.schema.json").read_text()
    )
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(
        manifest
    )


def test_auth_failure_is_failed_not_absent(tmp_path: Path):
    client = DartClient(api_key="SECRET", transport=lambda *a, **kw: (403, {}, b"SECRET"))
    manifest = collect(
        client,
        ArtifactStore(tmp_path),
        corp_code="00126380",
        fy=2024,
        rcept_no="20250314000123",
        package_id="pkg",
        manifest_id="m",
        document_version_id="v1",
        kinds=["statements"],
        synthetic=True,
    )
    entry = manifest["artifacts"][0]
    assert entry["status"] == "failed"
    assert entry["error_code"] == "authentication_failed"
    assert entry["artifact_sha256"] is None
    assert "SECRET" not in json.dumps(manifest)


def test_no_data_is_not_available(tmp_path: Path):
    client = DartClient(
        api_key="offline", transport=lambda *a, **kw: (200, {}, b'{"status":"013"}')
    )
    manifest = collect(
        client,
        ArtifactStore(tmp_path),
        corp_code="00126380",
        fy=2024,
        rcept_no="20250314000123",
        package_id="pkg",
        manifest_id="m",
        document_version_id="v1",
        kinds=["statements"],
        synthetic=True,
    )
    assert manifest["artifacts"][0]["status"] == "not_available"


def test_cli_without_credentials_fails_before_creating_store(tmp_path, monkeypatch):
    monkeypatch.delenv("DART_API_KEY", raising=False)
    assert (
        main(
            [
                "--corp-code",
                "00126380",
                "--fy",
                "2024",
                "--rcept-no",
                "20250314000123",
                "--package-id",
                "p",
                "--manifest-id",
                "m",
                "--document-version-id",
                "v1",
                "--store",
                str(tmp_path / "store"),
                "--output",
                str(tmp_path / "result.json"),
            ]
        )
        == 2
    )
    assert not (tmp_path / "store").exists()
    assert not (tmp_path / "result.json").exists()
