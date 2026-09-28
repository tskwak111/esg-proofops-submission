"""Raster egress preparation rechecks current authority and original selected pixels."""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from proofops.adapters.local import source_verification
from proofops.adapters.local.raster_visibility import raster_ocr_policy
from proofops.adapters.local.run_artifacts import load_run_inputs
from proofops.application.ingest.graph_fusion import fuse_candidates
from proofops.application.ports.jobs import JobMessage
from proofops.domain.provenance import canonical_hash

from tests.acceptance.test_parsing import candidate, pdf
from tests.integration.test_live_tagging_runtime_config import _register
from tests.integration.test_raster_runtime_config import AUTH, configured


def setup_request(tmp_path, monkeypatch):
    from proofops.adapters.local.upstage_parse import UpstageParseProbe

    from tests.integration import test_run_lifecycle

    monkeypatch.setattr(test_run_lifecycle, "pdf", lambda _: pdf())
    monkeypatch.setattr(
        UpstageParseProbe, "parse", lambda *a, **k: pytest.fail("no provider calls")
    )
    service, body = configured(tmp_path)
    service.raster_policy = raster_ocr_policy()
    grant = dict(
        service.registry.resolve_profile(AUTH, "runtime", service.raster_runtime_binding_id)
    )
    grant["runtime_binding_id"] = str(uuid4())
    grant["raster_policy_sha256"] = canonical_hash(service.raster_policy)
    service.raster_runtime_binding_id = _register(service, "runtime", grant, "runtime_binding_id")
    run_id = service.create(AUTH, body, str(uuid4()))["run_id"]
    snapshot, source, profile = load_run_inputs(
        service.store, service.uploads, tenant_id=AUTH.tenant_id, run_id=run_id
    )
    batch = candidate(
        "fixture", [("p", "paragraph", "Page 1 emissions 1234 tCO2e", (70, 710, 300, 740), ())]
    )
    batch = replace(
        batch,
        document_version_id=source.document_version_id,
        parse_manifest_id=profile.parse_manifest_id,
        source_sha256=source.sha256,
        blocks=tuple(
            replace(
                b,
                source=replace(
                    b.source,
                    document_version_id=source.document_version_id,
                    parse_manifest_id=profile.parse_manifest_id,
                ),
            )
            for b in batch.blocks
        ),
    )
    graph = fuse_candidates((batch,), tenant_id=AUTH.tenant_id)
    monkeypatch.setattr(
        source_verification,
        "_rendered_text",
        lambda *a, **k: dict(status="read", text="unresolved OCR"),
    )
    native = source_verification.attest_native_sources(
        graph, source.content, tenant_id=AUTH.tenant_id, geometry_mode="glyph"
    )
    message = JobMessage(
        **service.store.jobs.pending_outbox(AUTH.tenant_id, run_id, now=int(service.clock()))[0][
            "message"
        ]
    )
    lease = service.store.jobs.claim_job(
        message, owner="test", now=int(service.clock()), lease_seconds=60
    )
    runner = SimpleNamespace(store=service.store, uploads=service.uploads, clock=service.clock)
    return service, runner, lease, graph, native, snapshot


def prepare(runner, lease, graph, native):
    from proofops_worker.raster_runtime import prepare_authorized_raster

    return prepare_authorized_raster(runner, lease, graph, native, (graph.blocks[0].source_id,))


def test_request_binds_run_job_scope_and_exact_pixels(tmp_path, monkeypatch):
    from hashlib import sha256

    service, runner, lease, graph, native, snapshot = setup_request(tmp_path, monkeypatch)
    data, request = prepare(runner, lease, graph, native)
    assert prepare(runner, lease, graph, native) == (data, request)
    assert request["tenant_id"] == AUTH.tenant_id
    assert request["run_id"] == snapshot["run_id"]
    assert request["job_id"] == lease.message.job_id
    assert request["parse_manifest_id"] == graph.parse_manifest_id
    assert request["selected_pages"] == snapshot["selected_pages"]
    assert request["correspondence"]["input_pdf_sha256"] == sha256(data).hexdigest()
    assert request["policy_sha256"] == snapshot["raster_ocr_policy_hash"]
    assert request["mode"] == "standard"
    assert request["max_pages"] == snapshot["raster_ocr_policy"]["max_pages"]
    assert request["max_calls"] == snapshot["raster_ocr_policy"]["max_calls"]
    assert request["submitted_pages"] == 1
    assert request["eligible_source_ids"] == [graph.blocks[0].source_id]
    assert request["requested_source_ids"] == [graph.blocks[0].source_id]
    from uuid import UUID, uuid5

    unsigned = {k: v for k, v in request.items() if k != "request_id"}
    assert request["request_id"] == str(uuid5(UUID(lease.message.job_id), canonical_hash(unsigned)))
    for field in (
        "max_pages",
        "max_calls",
        "submitted_pages",
        "eligible_source_ids",
        "requested_source_ids",
    ):
        assert request["request_id"] != str(
            uuid5(UUID(lease.message.job_id), canonical_hash(unsigned | {field: None}))
        )
    assert service.store.jobs.list_usage(AUTH.tenant_id, snapshot["run_id"]) == []


@pytest.mark.parametrize(
    "kind,key", [("runtime", "raster_ocr_runtime"), ("consent", "consent"), ("rights", "rights")]
)
def test_revoked_profile_prevents_preparation(tmp_path, monkeypatch, kind, key):
    service, runner, lease, graph, native, snapshot = setup_request(tmp_path, monkeypatch)
    identifier = {
        "runtime": "runtime_binding_id",
        "consent": "consent_profile_id",
        "rights": "rights_profile_id",
    }[kind]
    from proofops.application.registry import RegistryNotFound

    resolve = service.registry.resolve_profile

    def unavailable(auth, requested_kind, requested_id):
        if (requested_kind, requested_id) == (kind, snapshot[key][identifier]):
            raise RegistryNotFound("revoked")
        return resolve(auth, requested_kind, requested_id)

    monkeypatch.setattr(service.registry, "resolve_profile", unavailable)
    with pytest.raises(ValueError, match="RASTER_AUTHORIZATION_REVOKED"):
        prepare(runner, lease, graph, native)


@pytest.mark.parametrize(
    "fault",
    ["lease", "expiry", "policy", "native", "document", "manifest", "page", "runtime_changed"],
)
def test_changed_inputs_do_not_prepare_egress(tmp_path, monkeypatch, fault):
    service, runner, lease, graph, native, snapshot = setup_request(tmp_path, monkeypatch)
    if fault == "lease":
        runner.clock = lambda: service.clock() + 61
    elif fault == "expiry":
        # A current lease does not override expired image consent/runtime.
        runner.clock = lambda: service.clock() + 10 * 86400
        monkeypatch.setattr(service.store.jobs, "can_call", lambda *a, **k: True)
    elif fault == "policy":
        from proofops_worker import raster_runtime

        monkeypatch.setattr(raster_runtime, "raster_ocr_policy", lambda **k: {})
    elif fault == "native":
        native = deepcopy(native)
        native["records"][0]["reason"] = "made_up"
    elif fault == "document":
        graph = replace(graph, document_version_id=str(uuid4()))
    elif fault == "manifest":
        graph = replace(graph, parse_manifest_id=str(uuid4()))
    elif fault == "page":
        graph = replace(
            graph,
            blocks=tuple(
                replace(
                    b,
                    candidates=tuple(
                        replace(c, source=replace(c.source, physical_page=2)) for c in b.candidates
                    ),
                )
                for b in graph.blocks
            ),
        )
    else:
        grant = dict(
            service.registry.resolve_profile(AUTH, "runtime", service.raster_runtime_binding_id)
        )
        grant["max_calls"] = 2
        resolve = service.registry.resolve_profile
        monkeypatch.setattr(
            service.registry,
            "resolve_profile",
            lambda auth, kind, identifier: grant
            if kind == "runtime"
            else resolve(auth, kind, identifier),
        )
    with pytest.raises(ValueError):
        prepare(runner, lease, graph, native)


def test_consent_revoked_during_render_is_rechecked(tmp_path, monkeypatch):
    from proofops.application.registry import RegistryNotFound
    from proofops_worker import raster_runtime

    service, runner, lease, graph, native, _ = setup_request(tmp_path, monkeypatch)
    original = raster_runtime.prepare_raster_ocr
    resolve = service.registry.resolve_profile

    def render_then_revoke(*args, **kwargs):
        result = original(*args, **kwargs)

        def revoked(auth, kind, identifier):
            if kind == "consent":
                raise RegistryNotFound("revoked while rendering")
            return resolve(auth, kind, identifier)

        monkeypatch.setattr(service.registry, "resolve_profile", revoked)
        return result

    monkeypatch.setattr(raster_runtime, "prepare_raster_ocr", render_then_revoke)
    with pytest.raises(ValueError, match="RASTER_AUTHORIZATION_REVOKED"):
        prepare(runner, lease, graph, native)
