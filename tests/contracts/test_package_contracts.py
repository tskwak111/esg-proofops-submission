"""TASK-000 contract bootstrap acceptance tests.

Fixed baseline: contracts/jsonschema/*.schema.json and contracts/openapi.yaml
are authoritative and read-only here. These tests assert the minimum Python
baseline:

- typed immutable domain values import and validate (source-less present rejected)
- LLM tag boundary rejects grade/label fields (rules-engine-only grading)
- trust-boundary negative matrix: fixed-schema fixtures mutated once each;
  the fixed contract and our boundary must both refuse the bypass
- explicit local synthetic composition runs; non-local fails closed for ALL
  adapters, not just the synthetic string
- API DTO boundary (Pydantic v2 strict) mirrors the fixed JSONSchema scope
  rules and grade/label map
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "fixtures"
SCHEMAS = ROOT / "contracts" / "jsonschema"


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _fixed_schema_rejects(payload: Any) -> bool:
    from jsonschema import Draft202012Validator, FormatChecker

    schema = _load_json(SCHEMAS / "llm_tags.schema.json")
    return (
        len(list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(payload)))
        > 0
    )


def _valid_llm_fixture() -> dict[str, Any]:
    return _load_json(FIXTURES / "llm_tags_example.json")


def test_domain_values_import() -> None:
    from proofops.domain import values

    assert values.ELEMENT_STATES == ("present", "absent", "unknown", "conflict", "not_applicable")
    assert values.TRACKS == ("goal", "performance", "management")
    assert values.GRADE_LABEL_MAP == {
        "E0": "UNSUBSTANTIATED",
        "E1": "INCOMPLETE",
        "E2": "INCOMPLETE",
        "E3": "SUBSTANTIATED",
    }


def test_domain_import_does_not_load_application_or_adapters() -> None:
    import subprocess
    import sys

    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import proofops.domain.values; "
            "assert not any(name.startswith(('proofops.adapters', "
            "'proofops.application', 'proofops.composition')) for name in sys.modules)",
        ],
        check=True,
    )


def test_wheel_includes_new_domain_subpackages(tmp_path: Path) -> None:
    import shutil
    import subprocess
    import zipfile

    source = ROOT / "packages" / "proofops"
    project = tmp_path / "project"
    project.mkdir()
    for name in ("pyproject.toml", "__init__.py", "composition.py"):
        shutil.copy2(source / name, project / name)
    for name in ("domain", "application", "adapters"):
        shutil.copytree(source / name, project / name, ignore=shutil.ignore_patterns("__pycache__"))
    probe = project / "domain" / "packaging_probe"
    probe.mkdir()
    (probe / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    output = tmp_path / "dist"
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(output)],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    )
    with zipfile.ZipFile(next(output.glob("*.whl"))) as wheel:
        assert "proofops/domain/packaging_probe/__init__.py" in wheel.namelist()


def test_source_less_present_rejected() -> None:
    """present with zero evidence_refs must never validate (contract negative)."""
    from proofops.domain import values

    ref = values.SourceRef(
        source_id="00000000-0000-4000-8000-000000000001",
        document_version_id="00000000-0000-4000-8000-000000000002",
        parse_manifest_id="00000000-0000-4000-8000-000000000003",
        page_num=28,
        printed_page_label="28",
        bbox=(120.0, 340.0, 180.0, 372.0),
        raw_text_sha256="d" * 64,
        quote="2030년까지",
        char_start=0,
        char_end=7,
        location_quality="located",
        verification_state="verified",
    )
    ok = values.LlmElement(
        element_id="G1",
        state="present",
        evidence_refs=(ref,),
        normalized_value="2030",
        credited_from=None,
        reason_code=None,
    )
    assert ok.state == "present"

    with pytest.raises(ValueError):
        values.LlmElement(
            element_id="G1",
            state="present",
            evidence_refs=(),
            normalized_value="2030",
            credited_from=None,
            reason_code=None,
        )


def test_llm_grade_fields_rejected() -> None:
    """LLM tagging boundary must reject grade/label: grading is rules-engine only."""
    from proofops.domain import values

    payload = _valid_llm_fixture()
    payload["label"] = "SUBSTANTIATED"
    with pytest.raises((ValueError, TypeError, KeyError)):
        values.llm_tags_from_dict(payload)

    payload = _valid_llm_fixture()
    payload["evidence_grade"] = "E3"
    with pytest.raises((ValueError, TypeError, KeyError)):
        values.llm_tags_from_dict(payload)


def test_valid_fixture_accepted_by_both() -> None:
    """Control case: the fixed fixture passes the schema and our boundary."""
    from proofops.domain import values

    payload = _valid_llm_fixture()
    assert not _fixed_schema_rejects(payload)
    tags = values.llm_tags_from_dict(payload)
    assert tags.replicate_id == 1


def _mutate(payload: dict[str, Any], path: list[Any], value: Any) -> dict[str, Any]:
    mutated = copy.deepcopy(payload)
    node = mutated
    for key in path[:-1]:
        node = node[key]
    if value is _REMOVE:
        del node[path[-1]]
    else:
        node[path[-1]] = value
    return mutated


class _Remove:
    pass


_REMOVE = _Remove()

# Mutations the fixed contract AND our boundary must both refuse.
BOTH_REJECT = {
    "unknown-root-key": (lambda p: {**p, "zzz_unknown": 1}),
    "unknown-element-key": (lambda p: _mutate(p, ["elements", 0, "zzz_unknown"], 1)),
    "unknown-source-key": (
        lambda p: _mutate(p, ["elements", 0, "evidence_refs", 0, "zzz_unknown"], 1)
    ),
    "missing-required-track": (lambda p: _mutate(p, ["track"], _REMOVE)),
    "missing-required-quote": (
        lambda p: _mutate(p, ["elements", 0, "evidence_refs", 0, "quote"], _REMOVE)
    ),
    "source-less-present": (lambda p: _mutate(p, ["elements", 0, "evidence_refs"], [])),
    "llm-grade-label": (lambda p: {**p, "label": "SUBSTANTIATED"}),
    "replicate-out-of-range": (lambda p: {**p, "replicate_id": 4}),
    "nonhex-packet-hash": (lambda p: {**p, "packet_sha256": "Z" * 64}),
    "malformed-claim-uuid": (lambda p: {**p, "claim_id": "not-a-uuid"}),
}

# Mutations the fixed JSONSchema cannot express (bool==int, Infinity) but our
# boundary must still refuse.
STRICTER_REJECT = {
    "bool-page-num": (lambda p: _mutate(p, ["elements", 0, "evidence_refs", 0, "page_num"], True)),
    "bool-replicate-id": (lambda p: {**p, "replicate_id": True}),
    "infinite-bbox": (
        lambda p: _mutate(
            p, ["elements", 0, "evidence_refs", 0, "bbox"], [0.0, 0.0, float("inf"), 1.0]
        )
    ),
}


@pytest.mark.parametrize("name", sorted(BOTH_REJECT))
def test_boundary_negative_matrix_fixed_contract(name: str) -> None:
    from proofops.domain import values

    mutated = BOTH_REJECT[name](_valid_llm_fixture())
    assert _fixed_schema_rejects(mutated), f"fixed schema accepted {name}"
    with pytest.raises((ValueError, TypeError, KeyError)):
        values.llm_tags_from_dict(mutated)


@pytest.mark.parametrize("name", sorted(STRICTER_REJECT))
def test_boundary_negative_matrix_stricter(name: str) -> None:
    from proofops.domain import values

    mutated = STRICTER_REJECT[name](_valid_llm_fixture())
    with pytest.raises((ValueError, TypeError, KeyError)):
        values.llm_tags_from_dict(mutated)


def test_mutable_inputs_defensively_copied() -> None:
    """Frozen values must never alias caller-mutable lists."""
    from proofops.domain import values

    ref_dict: dict[str, Any] = _valid_llm_fixture()["elements"][0]["evidence_refs"][0]
    tags = values.llm_tags_from_dict(_valid_llm_fixture())
    assert isinstance(tags.elements, tuple)
    assert isinstance(tags.warnings, tuple)
    element = tags.elements[0]
    assert isinstance(element.evidence_refs, tuple)
    assert element.evidence_refs[0].quote == ref_dict["quote"]


def test_local_synthetic_composition_runs() -> None:
    from proofops.composition import build_composition, build_local_composition

    comp = build_local_composition(app_env="local", model_adapter="synthetic")
    assert comp.profile == "local-synthetic"
    assert comp.model_adapter.kind == "synthetic"
    assert comp.binding.synthetic is True

    comp2 = build_composition(app_env="local", model_adapter="synthetic")
    assert comp2.profile == "local-synthetic"


def test_nonlocal_composition_fails_closed_for_all_adapters() -> None:
    """Staging/production refuse every local adapter, not just a string."""
    from proofops.composition import (
        AdapterRejectedError,
        build_composition,
        build_local_composition,
    )

    for env in ("staging", "production"):
        for adapter in ("synthetic", "bedrock"):
            with pytest.raises(AdapterRejectedError):
                build_local_composition(app_env=env, model_adapter=adapter)  # type: ignore[arg-type]
            with pytest.raises(AdapterRejectedError):
                build_composition(app_env=env, model_adapter=adapter)


def test_dto_run_scope_boundary() -> None:
    from proofops_api.dto import DtoValidationError, RunCreate

    base = {
        "document_version_id": "00000000-0000-4000-8000-000000000002",
        "mode": "disclosure",
        "rule_pack_id": "00000000-0000-4000-8000-000000000005",
        "consent_profile_id": "00000000-0000-4000-8000-000000000006",
        "runtime_binding_id": "00000000-0000-4000-8000-000000000007",
    }
    RunCreate(scope="full", selected_pages=None, **base)  # type: ignore[arg-type]
    RunCreate(
        scope="declared_subset",
        selected_pages=[1, 2],
        **base,  # type: ignore[arg-type]
    )
    with pytest.raises(DtoValidationError):
        RunCreate(scope="full", selected_pages=[1, 2], **base)  # type: ignore[arg-type]
    with pytest.raises(DtoValidationError):
        RunCreate(scope="declared_subset", selected_pages=None, **base)  # type: ignore[arg-type]
    with pytest.raises(DtoValidationError):
        RunCreate(scope="declared_subset", selected_pages=[2, 2], **base)  # type: ignore[arg-type]
    with pytest.raises(DtoValidationError):
        RunCreate(scope="full", selected_pages=None, extra="nope", **base)  # type: ignore[arg-type]
    with pytest.raises(DtoValidationError):
        RunCreate(scope="declared_subset", selected_pages=[True], **base)  # type: ignore[arg-type]


def test_dto_accepts_real_json_shapes() -> None:
    """model_validate must accept FastAPI-decoded JSON: UUID strings + arrays."""
    from proofops_api.dto import CompanyCreate, Decision, Health, RunCreate

    run = RunCreate.model_validate(
        {
            "document_version_id": "00000000-0000-4000-8000-000000000002",
            "mode": "disclosure",
            "scope": "declared_subset",
            "selected_pages": [1, 2],
            "rule_pack_id": "00000000-0000-4000-8000-000000000005",
            "consent_profile_id": "00000000-0000-4000-8000-000000000006",
            "runtime_binding_id": "00000000-0000-4000-8000-000000000007",
        }
    )
    assert run.selected_pages == (1, 2)
    assert str(run.document_version_id) == "00000000-0000-4000-8000-000000000002"

    decision = Decision.model_validate(
        {
            "decision_revision": 1,
            "tag_revision": 1,
            "decision_status": "decided",
            "evidence_grade": "E1",
            "label": "INCOMPLETE",
            "sublabel": "IMPL",
            "review_status": "auto_confirmed",
            "missing_elements": ["G3"],
            "rule_ids": ["GOAL_E1"],
            "rule_pack_sha256": "2" * 64,
            "semantic_hash": "3" * 64,
            "gap_ids": [],
        }
    )
    assert decision.missing_elements == ("G3",)

    company = CompanyCreate.model_validate({"legal_name": "합성 예시 기업", "aliases": ["예시"]})
    assert company.aliases == ("예시",)
    health = Health.model_validate({"status": "ok", "version": "0.0.0", "checks": ["baseline"]})
    assert health.checks == ("baseline",)


def test_run_create_real_http_post() -> None:
    """Real POST with JSON UUID strings + page list validates through HTTP."""
    from fastapi import HTTPException
    from fastapi.testclient import TestClient
    from proofops_api.dto import DtoValidationError, RunCreate
    from proofops_api.main import create_app

    app = create_app()

    @app.post("/__test__/runs/validate")
    def validate_run(body: dict) -> dict:  # type: ignore[no-untyped-def]
        try:
            validated = RunCreate.model_validate(body)
        except DtoValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return validated.model_dump(mode="json")

    client = TestClient(app)
    good = {
        "document_version_id": "00000000-0000-4000-8000-000000000002",
        "mode": "disclosure",
        "scope": "declared_subset",
        "selected_pages": [3, 4],
        "rule_pack_id": "00000000-0000-4000-8000-000000000005",
        "consent_profile_id": "00000000-0000-4000-8000-000000000006",
        "runtime_binding_id": "00000000-0000-4000-8000-000000000007",
    }
    response = client.post("/__test__/runs/validate", json=good)
    assert response.status_code == 200
    assert response.json()["selected_pages"] == [3, 4]

    bad = dict(good, scope="full")
    assert client.post("/__test__/runs/validate", json=bad).status_code == 422


def test_create_app_executes_composition_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-local startup fails fast instead of serving with local adapters."""
    from proofops.composition import AdapterRejectedError
    from proofops_api.main import create_app

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("MODEL_ADAPTER", "synthetic")
    with pytest.raises(AdapterRejectedError):
        create_app()

    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("MODEL_ADAPTER", "bedrock")
    with pytest.raises(AdapterRejectedError):
        create_app()


def test_dto_decision_grade_label_contract() -> None:
    from proofops_api.dto import Decision, DtoValidationError

    base = {
        "decision_revision": 1,
        "tag_revision": 1,
        "review_status": "auto_confirmed",
        "missing_elements": [],
        "rule_ids": ["PERF_E3"],
        "rule_pack_sha256": "2" * 64,
        "semantic_hash": "3" * 64,
        "gap_ids": [],
    }
    Decision(decision_status="decided", evidence_grade="E3", label="SUBSTANTIATED", **base)  # type: ignore[arg-type]
    for grade, label in (("E3", "INCOMPLETE"), ("E0", "SUBSTANTIATED")):
        with pytest.raises(DtoValidationError):
            Decision(decision_status="decided", evidence_grade=grade, label=label, **base)  # type: ignore[arg-type]
    with pytest.raises(DtoValidationError):
        Decision(
            decision_status="blocked_rule_gap",
            evidence_grade="E0",
            label="UNSUBSTANTIATED",
            review_status="needs_review",
            gap_ids=["GAP-007"],
            rule_pack_sha256="2" * 64,
            semantic_hash="3" * 64,
            decision_revision=1,
            tag_revision=1,
        )
    with pytest.raises(DtoValidationError):
        Decision(
            decision_status="decided",
            evidence_grade="E3",
            label="SUBSTANTIATED",
            rule_pack_sha256="Z" * 64,
            semantic_hash="3" * 64,
            decision_revision=1,
            tag_revision=1,
        )
    with pytest.raises(DtoValidationError):
        Decision(
            decision_status="decided",
            evidence_grade="E2",
            label="INCOMPLETE",
            sublabel="BOGUS",
            rule_pack_sha256="2" * 64,
            semantic_hash="3" * 64,
            decision_revision=1,
            tag_revision=1,
        )


def test_api_health_contract() -> None:
    """Live endpoint shape matches the fixed Health schema."""
    from fastapi.testclient import TestClient
    from proofops_api.main import create_app

    client = TestClient(create_app())
    response = client.get("/v1/health/live")
    assert response.status_code == 200
    body = response.json()

    from jsonschema import Draft202012Validator, FormatChecker

    api_schema = _load_json(SCHEMAS / "api_models.schema.json")
    schema = {"$ref": "#/$defs/Health", "$defs": api_schema["$defs"]}
    assert not list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(body))
    assert body["status"] in ("ok", "degraded", "not_ready")


def test_worker_agent_entrypoints_import() -> None:
    import proofops_agent.entrypoint
    import proofops_worker.main

    assert callable(proofops_agent.entrypoint.main)
    assert callable(proofops_worker.main.main)
