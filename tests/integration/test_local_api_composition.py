"""Exercise the composed local API, including separate app instances on one DB."""

import json
import time
from pathlib import Path

import yaml
from fastapi.testclient import TestClient
from proofops.application.authorization import MembershipRecord, SessionRecord

TENANT = "11111111-1111-4111-8111-111111111111"
MISSING = "22222222-2222-4222-8222-222222222222"
ORIGIN = "https://testserver"
PACK_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def test_composed_api_loads_explicit_runtime_and_recomputes_build_gate(tmp_path, monkeypatch):
    from tests.integration.test_local_runtime_config import _budget_limits, _parser_snapshot

    parser = _parser_snapshot()
    (tmp_path / "parser.json").write_text(json.dumps(parser))
    (tmp_path / "settings.json").write_text(
        json.dumps(
            {
                "build_root": str(tmp_path),
                "budget_limits": _budget_limits(),
            }
        )
    )
    for name, value in {
        "APP_ENV": "local",
        "MODEL_ADAPTER": "synthetic",
        "APP_ORIGIN": ORIGIN,
        "LOCAL_DATABASE_PATH": str(tmp_path / "state.sqlite3"),
        "LOCAL_PARSER_PROFILE_PATH": str(tmp_path / "parser.json"),
        "LOCAL_RUN_SETTINGS_PATH": str(tmp_path / "settings.json"),
        "LOCAL_EXTRACTION_MODE": "",
        "LOCAL_TAGGING_MODE": "",
    }.items():
        monkeypatch.setenv(name, value)
    from proofops_api.main import create_app

    with TestClient(create_app(), base_url=ORIGIN) as client:
        runs = client.app.state.composition.runs
        assert runs.parser_profile == parser
        assert runs.budget_limits.input_tokens == 1000
        assert runs.build_result.passed is False
        headers = sign_in(client)
        from proofops.application.registry import artifact_sha256

        from tests.acceptance.test_preflight import binding, consent

        registry = client.app.state.composition.registry
        for kind, artifact, identifier in (
            ("runtime", binding(), "runtime_binding_id"),
            ("consent", consent(), "consent_profile_id"),
        ):
            registry.with_option(
                TENANT,
                kind,
                artifact[identifier],
                "Synthetic configuration test",
                status="approved",
                version=artifact["version"],
                artifact=artifact,
                sha256=artifact_sha256(artifact),
                approved_by="synthetic-fixture",
                approved_at=artifact["approved_at"],
                local_synthetic=True,
            )
        response = client.post(
            "/v1/preflight",
            json={
                "runtime_binding_id": binding()["runtime_binding_id"],
                "consent_profile_id": consent()["consent_profile_id"],
                "include_live_model_probe": False,
            },
            headers=headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["ready"] is False
        assert (
            next(c for c in response.json()["checks"] if c["name"] == "supply_chain")["status"]
            == "fail"
        )


def synthetic_pack():
    from proofops.application.rulepacks import RulePackRecord, compute_pack_sha256

    metadata = {
        "version": "local-synthetic-v1",
        "effective_date": "2026-09-09",
        "source_document_sha256": "1" * 64,
    }
    files = {
        "regulatory/safe_harbor.yaml": {
            **metadata,
            "grade_mapping": None,
            "reasonable_basis_boolean_mapping": None,
        },
        "regulatory/timeline.yaml": {
            **metadata,
            "automatic_legal_applicability_enabled": False,
        },
    }
    record = {
        **metadata,
        "rule_pack_id": PACK_A,
        "tenant_id": TENANT,
        "mode": "disclosure",
        "status": "validated",
        "ontology_version": "local-synthetic-v1",
        "files": list(files),
        "unresolved_gap_ids": [f"GAP-{number:03}" for number in range(1, 11)],
        "approved_by": "local-synthetic-fixture",
        "approved_at": "2026-09-09T00:00:00Z",
    }
    record["sha256"] = compute_pack_sha256(record, files)
    return RulePackRecord.from_dict(record), files


def sign_in(client):
    store = client.app.state.composition.auth_store
    store.sessions.put(
        SessionRecord(
            "local-test-session",
            "local-admin",
            TENANT,
            "unused",
            time.time() + 600,
            time.time() + 600,
            False,
        )
    )
    store.memberships.put(MembershipRecord(TENANT, "local-admin", "admin", "active"))
    client.cookies.set("__Host-proofops_session", "local-test-session")
    response = client.get("/v1/session")
    assert response.status_code == 200
    return {
        "Origin": ORIGIN,
        "X-CSRF-Token": response.json()["csrf_token"],
        "Idempotency-Key": "local-composition-request",
    }


def test_composed_registry_preflight_and_rulepacks_share_durable_local_database(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("MODEL_ADAPTER", "synthetic")
    monkeypatch.setenv("APP_ORIGIN", ORIGIN)
    monkeypatch.setenv("LOCAL_DATABASE_PATH", str(tmp_path / "state.sqlite3"))
    from proofops_api.main import create_app

    with (
        TestClient(create_app(), base_url=ORIGIN) as first,
        TestClient(create_app(), base_url=ORIGIN) as second,
    ):
        unavailable_login = first.get("/auth/login")
        declared = yaml.safe_load(Path("contracts/openapi.yaml").read_text())["paths"]
        mounted = first.app.openapi()["paths"]
        for path, methods in declared.items():
            for method, operation in methods.items():
                if isinstance(operation, dict) and "operationId" in operation:
                    assert (
                        mounted.get(path, {}).get(method, {}).get("operationId")
                        == operation["operationId"]
                    )
        assert unavailable_login.status_code == 503
        assert unavailable_login.headers["cache-control"] == "no-store"
        assert (
            unavailable_login.json()["error"]["request_id"]
            == unavailable_login.headers["x-request-id"]
        )
        assert first.get(f"/v1/runs/{MISSING}").status_code == 401
        assert first.get(f"/v1/runs/{MISSING}/quality").status_code == 401
        assert first.get(f"/v1/runs/{MISSING}/claims").status_code == 401
        assert first.get(f"/v1/runs/{MISSING}/claims/{MISSING}").status_code == 401
        assert first.get(f"/v1/runs/{MISSING}/summary").status_code == 401
        assert first.get(f"/v1/runs/{MISSING}/reviews").status_code == 401
        assert first.get(f"/v1/runs/{MISSING}/rescores/{MISSING}").status_code == 401
        assert first.get(f"/v1/evaluations/{MISSING}").status_code == 401
        assert first.post(f"/v1/runs/{MISSING}/exports").status_code == 401
        assert first.get(f"/v1/exports/{MISSING}").status_code == 401
        assert first.post(f"/v1/exports/{MISSING}/download").status_code == 401
        assert first.post(f"/v1/documents/{MISSING}/deletion-requests").status_code == 401
        assert first.post(f"/v1/runs/{MISSING}/comparisons").status_code == 401
        assert first.get(f"/v1/comparisons/{MISSING}").status_code == 401
        for endpoint in ("observations", "assurance", "safe-harbor"):
            assert first.get(f"/v1/runs/{MISSING}/{endpoint}").status_code == 401
        assert first.get(f"/v1/runs/{MISSING}/sources/{MISSING}").status_code == 401
        assert first.post(f"/v1/runs/{MISSING}/sources/{MISSING}/view").status_code == 401
        assert first.get(f"/local/sources/{MISSING}/{MISSING}?ticket=invalid").status_code == 401
        headers = sign_in(first)
        sign_in(second)
        for path in (
            f"/v1/runs/{MISSING}/summary",
            f"/v1/runs/{MISSING}/rescores/{MISSING}",
            f"/v1/evaluations/{MISSING}",
            f"/v1/exports/{MISSING}",
            f"/v1/comparisons/{MISSING}",
            *(
                f"/v1/runs/{MISSING}/{endpoint}"
                for endpoint in ("observations", "assurance", "safe-harbor")
            ),
        ):
            response = first.get(path)
            assert response.status_code == 404
            assert response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert first.get(f"/v1/runs/{MISSING}").json()["error"]["code"] == "RESOURCE_NOT_FOUND"
        created = first.post(
            "/v1/companies",
            json={"legal_name": "Local synthetic company", "aliases": []},
            headers=headers,
        )
        assert created.status_code == 201
        listed = second.get("/v1/companies")
        assert listed.status_code == 200
        assert [company["company_id"] for company in listed.json()["items"]] == [
            created.json()["company_id"]
        ]
        document = first.post(
            "/v1/documents",
            json={
                "company_id": created.json()["company_id"],
                "title": "Synthetic integration report",
                "document_type": "sustainability_report",
            },
            headers={**headers, "Idempotency-Key": "create-document-request"},
        )
        assert document.status_code == 201
        assert (
            second.get(f"/v1/documents/{document.json()['document_id']}").json() == document.json()
        )
        options = first.get("/v1/runtime-options")
        assert options.status_code == 200
        assert options.json()["runtime_bindings"] == []
        blocked = first.post(
            "/v1/preflight",
            json={
                "runtime_binding_id": MISSING,
                "consent_profile_id": MISSING,
                "include_live_model_probe": False,
            },
            headers=headers,
        )
        assert blocked.status_code == 404
        activation = first.post(
            f"/v1/rule-packs/{MISSING}/activate",
            json={"reason": "Local synthetic check"},
            headers={**headers, "If-Match": '"1"'},
        )
        assert activation.status_code == 404

        # Fixtures are local synthetic approvals, never repository draft promotion.
        first.app.state.composition.rulepack_store.add_pack(*synthetic_pack())
        activated = first.post(
            f"/v1/rule-packs/{PACK_A}/activate",
            json={"reason": "Activate synthetic integration fixture"},
            headers={**headers, "If-Match": '"1"', "Idempotency-Key": "activate-fixture-request"},
        )
        assert activated.status_code == 200
        assert second.get("/v1/runtime-options").json()["rule_packs"] == [activated.json()]

    with TestClient(create_app(), base_url=ORIGIN) as reopened:
        headers = sign_in(reopened)
        assert (
            reopened.get("/v1/companies").json()["items"][0]["company_id"]
            == created.json()["company_id"]
        )
        assert reopened.get("/v1/runtime-options").json()["rule_packs"] == [activated.json()]
        assert (
            reopened.get(f"/v1/documents/{document.json()['document_id']}").json()
            == document.json()
        )
        deletion = reopened.post(
            f"/v1/documents/{document.json()['document_id']}/deletion-requests",
            json={"reason": "Delete the generated local composition fixture"},
            headers=headers,
        )
        assert deletion.status_code == 202, deletion.text
        assert deletion.json()["status"] == "blocked_retention"
        assert reopened.get(f"/v1/documents/{document.json()['document_id']}").status_code == 404
        assert reopened.get("/v1/documents").json()["items"] == []
