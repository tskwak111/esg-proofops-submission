"""Freeze raster grants independently; legacy runs do not acquire image egress."""

from copy import deepcopy
from uuid import uuid4

import pytest
from proofops.application.runs import RunRejected
from proofops.domain.provenance import canonical_hash

from tests.integration.test_live_tagging_runtime_config import (
    AUTH,
    _draft_pack,
    _live_service,
    _register,
)


def configured(tmp_path):
    service, body, *_ = _live_service(tmp_path)
    policy = dict(
        schema="local_raster_ocr_policy_v1",
        mode="standard",
        max_pages=10,
        max_calls=1,
        native_policy_sha256="a" * 64,
        raster_helper_sha256="b" * 64,
        composition_helper_sha256="c" * 64,
        checkpoint_helper_sha256="d" * 64,
        reader_versions={name: "test" for name in ("pypdfium2", "pdfplumber", "pypdf", "Pillow")},
    )
    consent = dict(service.registry.resolve_profile(AUTH, "consent", body["consent_profile_id"]))
    consent.pop("consent_profile_id")
    consent["allow_raster_upload"] = True
    body["consent_profile_id"] = _register(service, "consent", consent, "consent_profile_id")
    grant = dict(service.registry.resolve_profile(AUTH, "runtime", body["runtime_binding_id"]))
    grant.update(
        runtime_binding_id=str(uuid4()),
        schema="local_upstage_raster_binding_v1",
        role="vision",
        model_id="document-parse-260128",
        endpoint="https://api.upstage.ai/v1/document-digitization",
        mode="standard",
        max_pages=10,
        max_calls=1,
        accepts_images=True,
        image_input_verified=True,
        raster_policy_sha256=canonical_hash(policy),
    )
    service.raster_runtime_binding_id = _register(service, "runtime", grant, "runtime_binding_id")
    service.raster_policy = policy
    body["rule_pack_id"] = _draft_pack(service, AUTH.tenant_id).rule_pack_id
    return service, body


def test_raster_group_is_frozen_with_existing_document_scope(tmp_path):
    service, body = configured(tmp_path)
    result = service.create(AUTH, body, str(uuid4()))
    snapshot = service.store.snapshot(AUTH.tenant_id, result["run_id"])
    assert snapshot["raster_ocr_policy"] == service.raster_policy
    assert snapshot["raster_ocr_policy_hash"] == canonical_hash(service.raster_policy)
    assert snapshot["raster_ocr_runtime"]["runtime_binding_id"] == service.raster_runtime_binding_id
    assert snapshot["raster_ocr_runtime_artifact_hash"]
    assert snapshot["selected_pages"] == body["selected_pages"]
    service.raster_policy["mode"] = "enhanced"
    assert service.store.snapshot(AUTH.tenant_id, result["run_id"]) == snapshot


@pytest.mark.parametrize("fault", ["partial", "policy", "mode", "grant", "calls", "scope"])
def test_invalid_raster_configuration_does_not_create_run(tmp_path, fault):
    service, body = configured(tmp_path)
    if fault == "partial":
        service.raster_policy = None
    elif fault == "policy":
        service.raster_policy["native_policy_sha256"] = "wrong"
    elif fault == "mode":
        service.raster_policy["mode"] = "enhanced"
    elif fault == "grant":
        service.raster_runtime_binding_id = str(uuid4())
    elif fault == "calls":
        service.raster_policy["max_calls"] = True
    else:
        body["scope"] = "full"
        body.pop("selected_pages")
    with pytest.raises(RunRejected, match="CONFIG_GATE_BLOCKED"):
        service.create(AUTH, body, str(uuid4()))


@pytest.mark.parametrize(
    "field",
    [
        "raster_ocr_policy_hash",
        "raster_ocr_runtime_artifact_hash",
        "raster_ocr_runtime",
        "raster_ocr_policy",
    ],
)
def test_store_rejects_partial_group(tmp_path, field):
    service, body = configured(tmp_path)
    original = service.store.create

    def tamper(auth, payload, key, snapshot, limits, *, now):
        snapshot = deepcopy(snapshot)
        snapshot.pop(field)
        return original(auth, payload, key, snapshot, limits, now=now)

    service.store.create = tamper
    with pytest.raises(RunRejected, match="CONFIG_GATE_BLOCKED"):
        service.create(AUTH, body, str(uuid4()))


def test_legacy_run_has_no_raster_group(tmp_path):
    service, body, *_ = _live_service(tmp_path)
    body["rule_pack_id"] = _draft_pack(service, AUTH.tenant_id).rule_pack_id
    result = service.create(AUTH, body, str(uuid4()))
    assert not any(
        k.startswith("raster_ocr_")
        for k in service.store.snapshot(AUTH.tenant_id, result["run_id"])
    )


@pytest.mark.parametrize("fault", ["policy_hash", "grant_hash", "grant_shape", "extra"])
def test_store_rejects_tampered_group(tmp_path, fault):
    service, body = configured(tmp_path)
    original = service.store.create

    def tamper(auth, payload, key, snapshot, limits, *, now):
        snapshot = deepcopy(snapshot)
        if fault == "policy_hash":
            snapshot["raster_ocr_policy_hash"] = "0" * 64
        elif fault == "grant_hash":
            snapshot["raster_ocr_runtime_artifact_hash"] = "0" * 64
        elif fault == "grant_shape":
            from proofops.application.registry import artifact_sha256

            snapshot["raster_ocr_runtime"] = []
            snapshot["raster_ocr_runtime_artifact_hash"] = artifact_sha256([])
        else:
            snapshot["raster_ocr_unrecognized"] = True
        return original(auth, payload, key, snapshot, limits, now=now)

    service.store.create = tamper
    with pytest.raises(RunRejected, match="CONFIG_GATE_BLOCKED"):
        service.create(AUTH, body, str(uuid4()))


def test_legacy_worker_rejects_raster_before_job_access(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from proofops.application.ingest.graph_fusion import ParserProfile
    from proofops_worker.local_runner import LocalParserRunner

    from tests.acceptance.test_parsing import JAVA, MANIFEST

    service, body = configured(tmp_path)
    result = service.create(AUTH, body, str(uuid4()))

    def forbidden(*args, **kwargs):
        pytest.fail("unsupported raster run must not access jobs or parse")

    monkeypatch.setattr(service.store.jobs, "get_run", forbidden)
    runner = LocalParserRunner(
        service.store,
        service.uploads,
        SimpleNamespace(parse=forbidden),
        profile=ParserProfile(MANIFEST, java_executable=JAVA),
        telemetry=SimpleNamespace(),
    )
    with pytest.raises(ValueError, match="RASTER_OCR_RUNTIME_NOT_SUPPORTED"):
        runner.run_once(tenant_id=AUTH.tenant_id, run_id=result["run_id"])
