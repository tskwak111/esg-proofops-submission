"""Pilot config must create distinct real bindings without granting rule authority."""

import pytest
from proofops.application.ports.models import ModelBinding
from proofops.application.tagging.service import TaggingSettings

from evaluation.local_upstage_pilot import live_tagging_settings


def test_pilot_company_identity_is_explicit_and_new_run_only():
    from evaluation.local_upstage_pilot import pilot_company_body

    assert pilot_company_body(None, None, existing=False) == dict(
        legal_name="실제 보고서 검토 시험", aliases=[], registration_identifier=None
    )
    assert pilot_company_body("네이버 주식회사", "DART:00266961", existing=False) == dict(
        legal_name="네이버 주식회사", aliases=[], registration_identifier="DART:00266961"
    )
    for name, identifier, existing in (
        ("네이버 주식회사", None, False),
        (None, "DART:00266961", False),
        (" ", "DART:00266961", False),
        ("네이버 주식회사", " ", False),
        ("네이버 주식회사", "DART:00266961", True),
    ):
        with pytest.raises(ValueError):
            pilot_company_body(name, identifier, existing=existing)


def test_live_pilot_settings_have_independent_real_profiles():
    result = live_tagging_settings(12)
    preliminary, tagging = (
        TaggingSettings(**(result[key] | {"binding": ModelBinding(**result[key]["binding"])}))
        for key in ("preliminary_settings", "tagging_settings")
    )
    assert preliminary.binding.binding_id != tagging.binding.binding_id
    assert preliminary.binding.synthetic is tagging.binding.synthetic is False
    assert preliminary.model_id == tagging.model_id == "solar-pro4"
    assert preliminary.model_profile == "upstage-preliminary-source-quotes-v1"
    assert tagging.model_profile == "upstage-compact-source-quotes-v4"
    assert set(result) == {"preliminary_settings", "tagging_settings", "input_reservation_policy"}


def test_compact_element_wire_is_explicit_and_pinned_on_resume():
    from evaluation.local_upstage_pilot import apply_resume_metadata
    from tests.unit.test_extraction_source_id_wiring import _args

    old = live_tagging_settings(12)["tagging_settings"]
    new = live_tagging_settings(12, compact_element_wire=True)["tagging_settings"]
    assert old["model_profile"] == "upstage-compact-source-quotes-v4"
    assert new["model_profile"] == "upstage-compact-source-quotes-v5"
    assert old["system_prompt"] == new["system_prompt"]
    args = _args()
    apply_resume_metadata(args, {"source_path": "/tmp/example.pdf", "compact_element_wire": True})
    assert args.compact_element_wire is True


def test_resume_cannot_add_compact_element_wire(tmp_path, monkeypatch, capsys):
    import json
    import sys

    from evaluation import local_upstage_pilot as pilot

    state = tmp_path / "legacy"
    state.mkdir()
    (state / "pilot.json").write_text(json.dumps({"source_path": "/tmp/example.pdf"}))
    monkeypatch.setattr(
        sys, "argv", ["pilot", "--resume", "--state", str(state), "--compact-element-wire"]
    )
    with pytest.raises(SystemExit):
        pilot.main()
    assert "--resume cannot add compact element wire" in capsys.readouterr().err


@pytest.mark.parametrize("calls", [True, 0, 5, 2001, 12.5])
def test_live_pilot_rejects_invalid_call_limit(calls):
    with pytest.raises(ValueError):
        live_tagging_settings(calls)


@pytest.mark.parametrize("calls", [6, 48, 2000])
def test_live_pilot_accepts_full_report_call_limit(calls):
    assert live_tagging_settings(calls)["tagging_settings"]["max_tokens"] == 4096


def test_preliminary_schema_rejects_the_observed_live_string_dimension():
    import json

    import jsonschema

    schema = json.loads(live_tagging_settings(12)["preliminary_settings"]["schema_json"])
    jsonschema.Draft202012Validator.check_schema(schema)
    response = dict(
        claim_id="11111111-1111-4111-8111-111111111111",
        track="management",
        safe_harbor_category=None,
        track_confidence=0.8,
        dimensions=dict(entity=None, metric="project description", reporting_period=None),
    )
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(response, schema)
    response["dimensions"]["metric"] = dict(source_index=0, quote="emissions")
    jsonschema.validate(response, schema)
    response["dimensions"]["metric"] = None
    jsonschema.validate(response, schema)


def test_element_prompt_contains_rule_names_and_field_semantics():
    import yaml

    from evaluation.local_upstage_pilot import ROOT

    result = live_tagging_settings(12)
    prompt = result["tagging_settings"]["system_prompt"]
    elements = yaml.safe_load((ROOT / "config/rubric/elements.yaml").read_text())["elements"]
    for element in elements:
        assert element["id"] in prompt
        assert element["name"] in prompt
    assert "credited_from must be null" in prompt
    assert "not an approval" in prompt


def test_relation_pilot_settings_are_opt_in_and_have_their_own_output_schema():
    import json

    import jsonschema
    from proofops.application.tagging.relations import SYSTEM_PROMPT

    assert "relation_settings" not in live_tagging_settings(12)
    result = live_tagging_settings(18, relations=True)
    relation = result["relation_settings"]
    assert relation["model_profile"] == "upstage-relation-source-quotes-v1"
    assert relation["system_prompt"] == SYSTEM_PROMPT
    assert (
        len(
            {
                result[p + "_settings"]["binding"]["binding_id"]
                for p in ("preliminary", "tagging", "relation")
            }
        )
        == 3
    )
    schema = json.loads(relation["schema_json"])
    jsonschema.Draft202012Validator.check_schema(schema)
    sample = {
        "relations": [
            {
                "source_index": 0,
                "dimensions": {
                    "entity": None,
                    "metric": None,
                    "reporting_period": None,
                },
            }
        ]
    }
    jsonschema.validate(sample, schema)
    sample["relations"][0]["dimensions"]["grade"] = None
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(sample, schema)


def test_preliminary_context_pilot_settings_are_opt_in_with_distinct_profile_and_prompt():
    from proofops.application.tagging.preliminary import CONTEXT_SYSTEM_SUFFIX, SYSTEM_PROMPT

    default = live_tagging_settings(12)
    assert default["preliminary_settings"]["model_profile"] == (
        "upstage-preliminary-source-quotes-v1"
    )
    assert default["preliminary_settings"]["system_prompt"] == SYSTEM_PROMPT

    result = live_tagging_settings(12, preliminary_context=True)
    preliminary = result["preliminary_settings"]
    assert preliminary["model_profile"] == "upstage-preliminary-source-quotes-context-v1"
    assert preliminary["system_prompt"] == SYSTEM_PROMPT + CONTEXT_SYSTEM_SUFFIX
    # Everything else (tagging profile, binding independence) stays unaffected.
    assert result["tagging_settings"]["model_profile"] == "upstage-compact-source-quotes-v4"
    assert (
        preliminary["binding"]["binding_id"] != result["tagging_settings"]["binding"]["binding_id"]
    )


@pytest.mark.parametrize("value", [1, "yes", None])
def test_preliminary_context_flag_must_be_a_real_boolean(value):
    with pytest.raises(ValueError):
        live_tagging_settings(12, preliminary_context=value)


def test_raster_pilot_settings_pin_explicit_policy():
    from proofops.adapters.local.raster_visibility import raster_ocr_policy

    from evaluation.local_upstage_pilot import raster_settings

    settings = raster_settings(max_pages=4, max_calls=1)
    assert set(settings) == {"raster_runtime_binding_id", "raster_policy"}
    from uuid import UUID

    assert str(UUID(settings["raster_runtime_binding_id"])) == settings["raster_runtime_binding_id"]
    assert settings["raster_policy"] == raster_ocr_policy(max_pages=4, max_calls=1)


@pytest.mark.parametrize("pages,calls", [(True, 1), (0, 1), (4, 0), (4, True)])
def test_raster_pilot_settings_reject_invalid_limits(pages, calls):
    from evaluation.local_upstage_pilot import raster_settings

    with pytest.raises(ValueError):
        raster_settings(max_pages=pages, max_calls=calls)


def test_extraction_batch_and_run_budget_are_separate_and_bounded():
    from evaluation.local_upstage_pilot import extraction_budget_settings

    legacy = extraction_budget_settings(8)
    assert legacy["roles"][0]["max_calls"] == 8
    assert legacy["input_tokens"] == 100000
    assert legacy["output_tokens"] == 30000
    continued = extraction_budget_settings(8, 40)
    assert continued["roles"][0]["max_calls"] == 40
    assert continued["input_tokens"] == 4000000
    assert continued["output_tokens"] == 40960
    for invalid in (True, 0, 7, 2001, 8.5):
        with pytest.raises(ValueError, match="extraction total"):
            extraction_budget_settings(8, invalid)


def test_element_prompt_requires_literal_value_quotes_without_changing_wire_schema():
    result = live_tagging_settings(12)
    settings = TaggingSettings(
        **(
            result["tagging_settings"]
            | {"binding": ModelBinding(**result["tagging_settings"]["binding"])}
        )
    )
    assert "at least one selected quote MUST be exactly that value" in settings.system_prompt
    assert "citing the whole sentence alone is invalid" in settings.system_prompt
    assert settings.model_profile == "upstage-compact-source-quotes-v4"
    import json

    assert json.loads(settings.schema_json)["$defs"]["SourceRef"]["type"] == "object"


def test_element_prompt_distinguishes_target_deadline_from_event_date():
    prompt = live_tagging_settings(6)["tagging_settings"]["system_prompt"]
    assert "G1 requires a deadline for the claimed goal" in prompt
    assert "designation, registration, publication or reporting year" in prompt
    assert "a goal track assignment does not establish a target deadline" in prompt


def test_pilot_oversized_pdf_rejected_before_state_creation(tmp_path, monkeypatch, capsys):
    import sys

    from proofops.application.uploads_security import PdfLimits

    from evaluation import local_upstage_pilot as pilot

    source = tmp_path / "oversized.pdf"
    with source.open("wb") as stream:
        stream.truncate(PdfLimits().max_bytes + 1)
    state = tmp_path / "new-state"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "pilot",
            "--pdf",
            str(source),
            "--state",
            str(state),
            "--pages",
            "1",
            "--report-year",
            "2025",
            "--period-start",
            "2024-01-01",
            "--period-end",
            "2024-12-31",
        ],
    )
    with pytest.raises(SystemExit) as error:
        pilot.main()
    assert error.value.code == 2
    assert "104857600 bytes" in capsys.readouterr().err
    assert not state.exists()


def test_local_login_link_mints_fresh_session_after_expiry(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from proofops.adapters.local.auth_store import InMemorySessionStore
    from proofops_api.auth import SESSION_COOKIE_NAME

    from evaluation import local_upstage_pilot as pilot

    sessions = InMemorySessionStore()
    app = FastAPI()
    app.get("/__local/test-secret")(
        lambda: pilot.local_login_response(sessions, "user", "tenant", "run")
    )
    client = TestClient(app, base_url="https://localhost", follow_redirects=False)
    monkeypatch.setattr(pilot.time, "time", lambda: 1000.0)
    first = client.get("/__local/test-secret")
    first_id = client.cookies[SESSION_COOKIE_NAME]
    first_record = sessions.get(first_id)
    assert first.status_code == 303 and first.headers["location"] == "/runs/run/claims"
    monkeypatch.setattr(pilot.time, "time", lambda: 5000.0)
    second = client.get("/__local/test-secret")
    second_id = client.cookies[SESSION_COOKIE_NAME]
    second_record = sessions.get(second_id)
    assert first_id != second_id
    assert first_record.expires_at == 4600.0
    assert second_record.expires_at == second_record.idle_deadline == 8600.0
    assert second_record.user_sub == "user" and second_record.active_tenant_id == "tenant"
    assert sessions.csrf_token_for(first_id) != sessions.csrf_token_for(second_id)
    assert all(
        flag in second.headers["set-cookie"] for flag in ("Secure", "HttpOnly", "SameSite=strict")
    )


def test_live_pilot_settings_capacity_refresh_opt_in():
    default = live_tagging_settings(12)
    assert default["input_reservation_policy"]["captured_at"] == "2026-09-18T16:53:00Z"
    assert default["input_reservation_policy"]["expires_at"] == "2026-09-25T00:00:00Z"

    opted = live_tagging_settings(12, capacity_refresh=True)
    assert opted["input_reservation_policy"]["captured_at"] == "2026-09-25T10:57:00Z"
    assert opted["input_reservation_policy"]["expires_at"] == "2026-10-02T00:00:00Z"

    for invalid in ("yes", 1, None, 0):
        with pytest.raises(ValueError):
            live_tagging_settings(12, capacity_refresh=invalid)


def test_new_local_approval_profiles_use_bounded_24h_lifetime(tmp_path, monkeypatch):
    import json
    import sqlite3
    import sys
    from datetime import datetime, timedelta

    from pypdf import PdfWriter

    from evaluation import local_upstage_pilot as pilot

    pdf_path = tmp_path / "sample.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with pdf_path.open("wb") as stream:
        writer.write(stream)

    state = tmp_path / "pilot-state"
    argv = [
        "pilot",
        "--pdf",
        str(pdf_path),
        "--state",
        str(state),
        "--pages",
        "1",
        "--report-year",
        "2025",
        "--period-start",
        "2024-01-01",
        "--period-end",
        "2024-12-31",
    ]
    # A prior API import must not bind this pilot to another composition/origin.
    import os

    from proofops_api import main as api_main

    monkeypatch.setattr(os, "environ", os.environ.copy())
    monkeypatch.setattr(api_main, "app", object())
    monkeypatch.setattr(sys, "argv", argv)
    exit_code = pilot.main()
    assert exit_code == 0

    # Inspect registered profiles in state.sqlite3
    db_path = state / "state.sqlite3"
    assert db_path.exists()
    conn = sqlite3.connect(str(db_path))
    row = conn.execute("SELECT value FROM registry_state WHERE key = 'state'").fetchone()
    conn.close()

    assert row is not None
    data = json.loads(row[0])
    options = data["options"]
    assert len(options) > 0
    checked_profiles = 0
    for opt in options:
        artifact = opt.get("artifact") or {}
        if "approved_at" in artifact and "expires_at" in artifact:
            approved_dt = datetime.fromisoformat(artifact["approved_at"].replace("Z", "+00:00"))
            expires_dt = datetime.fromisoformat(artifact["expires_at"].replace("Z", "+00:00"))
            # Bounded 24h lifetime from creation instant
            diff = expires_dt - approved_dt
            assert diff == timedelta(
                hours=24
            ), f"Expected 24h difference, got {diff} on {opt.get('kind')}/{opt.get('id')}"
            checked_profiles += 1

    assert checked_profiles >= 3  # rights, runtime, consent (+ tagger runtime)

    # Resume must never renew stored/resumed profiles
    resume_argv = ["pilot", "--state", str(state), "--resume"]
    monkeypatch.setattr(sys, "argv", resume_argv)
    exit_code_resume = pilot.main()
    assert exit_code_resume == 0

    conn = sqlite3.connect(str(db_path))
    resumed_row = conn.execute("SELECT value FROM registry_state WHERE key = 'state'").fetchone()
    conn.close()
    assert resumed_row == row  # Exact match, untouched
