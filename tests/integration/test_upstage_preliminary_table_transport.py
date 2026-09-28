"""R12 transport boundary for the table-source preliminary profile. No paid calls.

Reuses the existing preliminary transport harness; only the wire contract is
under test. The point is that a table-role numbered source exists on exactly one
profile and nowhere else, and that a malformed role can never reach a provider.
"""

import json
from dataclasses import asdict
from uuid import UUID

import pytest
from proofops.adapters.local.upstage import MODEL_PRO4, UpstageProbe
from proofops.application.ports.models import ModelBinding
from proofops.application.tagging.preliminary import (
    TABLE_SCHEMA,
    preliminary_request,
    preliminary_table_request,
)
from proofops.application.tagging.service import TaggingSettings
from proofops.domain.provenance import canonical_hash
from proofops_agent.upstage_preliminary import (
    TABLE_MODEL_PROFILE,
    TABLE_SYSTEM_PROMPT,
    TABLE_TRANSPORT_VERSION,
    UpstagePreliminaryTransport,
)

from tests.acceptance.test_citations import TENANT
from tests.acceptance.test_preliminary_table_sources import table_corpus
from tests.integration.test_upstage_preliminary_transport import (  # noqa: F401
    _freeze_upstage_price_clock,
)


def configured_with_table(tmp_path, monkeypatch, *, verified_axes=True):
    graph, claim, _ = table_corpus(verified_axes=verified_axes)
    envelope = preliminary_table_request(claim, graph, tenant_id=TENANT)
    settings = TaggingSettings(
        ModelBinding("00000000-0000-4000-8000-000000000001", "tagger", False),
        MODEL_PRO4,
        TABLE_MODEL_PROFILE,
        "provider-managed-unverified",
        TABLE_SYSTEM_PROMPT,
        json.dumps({"type": "object"}),
        max_tokens=1024,
    )
    probe = UpstageProbe("test-not-a-key", tmp_path / "budget.sqlite3", model=MODEL_PRO4)
    calls = []

    def post(body):
        calls.append(body)
        return dict(
            id="fixture-provider",
            model=MODEL_PRO4,
            usage=dict(prompt_tokens=20, completion_tokens=10),
            choices=[dict(finish_reason="stop", message=dict(content="{}"))],
        )

    monkeypatch.setattr(probe, "_post", post)
    from proofops.application.preflight import check_local_upstage_tagger

    from tests.integration.test_upstage_tagger_preflight import configured as approvals

    authorization = approvals()
    authorization.pop("settings")
    authorization["binding"].update(
        model_id=settings.model_id, tagging_settings_sha256=canonical_hash(asdict(settings))
    )
    adapter = UpstagePreliminaryTransport(
        probe,
        tmp_path / "receipts",
        settings=settings,
        tenant_id=TENANT,
        authorize=lambda selected, request: check_local_upstage_tagger(
            settings=selected, **authorization
        ),
    )
    request = dict(
        tenant_id=TENANT,
        claim_id=claim.claim_id,
        packet_sha256=canonical_hash(envelope),
        replicate_id=1,
        request_id=str(UUID(int=991)),
        request_signature=canonical_hash("fixture"),
        binding=asdict(settings.binding),
        model_id=settings.model_id,
        model_profile=settings.model_profile,
        region=settings.region,
        system_prompt=settings.rendered_system,
        temperature=0,
        max_tokens=100,
    )
    request["user_json"] = json.dumps(envelope, ensure_ascii=False)
    return adapter, request, envelope, claim, graph


def test_the_table_profile_accepts_its_own_envelope_and_pins_its_transport_version(
    tmp_path, monkeypatch
):
    adapter, request, envelope, _, _ = configured_with_table(tmp_path, monkeypatch)
    assert adapter.TRANSPORT_VERSION == TABLE_TRANSPORT_VERSION
    assert envelope["schema"] == TABLE_SCHEMA
    system, wire_user, _, _ = adapter._wire_request(request)
    assert system == adapter._settings.rendered_system
    assert TABLE_SYSTEM_PROMPT in system
    # tuple bboxes become lists on the wire; compare the JSON projection.
    assert json.loads(wire_user) == json.loads(json.dumps(envelope))


def test_a_table_role_source_is_refused_on_the_context_profile(tmp_path, monkeypatch):
    """The older profiles have no table-role source; the wire must say so."""
    adapter, request, envelope, claim, graph = configured_with_table(tmp_path, monkeypatch)
    context_envelope = preliminary_request(claim, graph, tenant_id=TENANT, include_context=True)
    context_envelope["untrusted_document_data"]["sources"] = envelope["untrusted_document_data"][
        "sources"
    ]
    adapter._settings = adapter._settings.__class__(
        **{**asdict(adapter._settings), "binding": adapter._settings.binding}
    )
    request["user_json"] = json.dumps(context_envelope, ensure_ascii=False)
    request["packet_sha256"] = canonical_hash(context_envelope)
    with pytest.raises(ValueError, match="UPSTAGE_PRELIMINARY"):
        adapter._wire_request(request)


def test_a_table_role_source_may_never_take_index_zero(tmp_path, monkeypatch):
    adapter, request, envelope, _, _ = configured_with_table(tmp_path, monkeypatch)
    sources = envelope["untrusted_document_data"]["sources"]
    swapped = [{**sources[1], "source_index": 0}, {**sources[0], "source_index": 1}]
    envelope["untrusted_document_data"]["sources"] = swapped
    request["user_json"] = json.dumps(envelope, ensure_ascii=False)
    request["packet_sha256"] = canonical_hash(envelope)
    with pytest.raises(ValueError, match="UPSTAGE_PRELIMINARY_SOURCES_REQUIRED"):
        adapter._wire_request(request)


@pytest.mark.parametrize(
    "mutation",
    [
        {"table_role": "metric"},
        {"table_association": "page_covered"},
        {"table_row_number": -1},
        {"table_column_number": "3"},
    ],
)
def test_a_malformed_table_role_source_never_reaches_a_provider(tmp_path, monkeypatch, mutation):
    adapter, request, envelope, _, _ = configured_with_table(tmp_path, monkeypatch)
    sources = envelope["untrusted_document_data"]["sources"]
    sources[1] = {**sources[1], **mutation}
    request["user_json"] = json.dumps(envelope, ensure_ascii=False)
    request["packet_sha256"] = canonical_hash(envelope)
    with pytest.raises(ValueError, match="UPSTAGE_PRELIMINARY"):
        adapter._wire_request(request)


def test_candidate_only_axes_travel_as_prefixed_context_roles(tmp_path, monkeypatch):
    adapter, request, envelope, _, _ = configured_with_table(
        tmp_path, monkeypatch, verified_axes=False
    )
    blocks = envelope["untrusted_document_data"]["context_blocks"]
    roles = [block["role"] for block in blocks if block["role"].startswith("table_")]
    assert len(roles) >= 2 and set(roles) <= {
        "table_row_header",
        "table_column_header",
        "table_row_qualifier",
    }
    assert envelope["untrusted_document_data"]["sources"] == [{"source_index": 0, "text": "900"}]
    system, wire_user, _, _ = adapter._wire_request(request)
    # tuple bboxes become lists on the wire; compare the JSON projection.
    assert json.loads(wire_user) == json.loads(json.dumps(envelope))


def test_an_unknown_table_policy_shape_is_refused(tmp_path, monkeypatch):
    adapter, request, envelope, _, _ = configured_with_table(tmp_path, monkeypatch)
    envelope["table_policy"]["role_basis"] = "semantic_attestation"
    request["user_json"] = json.dumps(envelope, ensure_ascii=False)
    request["packet_sha256"] = canonical_hash(envelope)
    with pytest.raises(ValueError, match="UPSTAGE_PRELIMINARY_TABLE_POLICY_INVALID"):
        adapter._wire_request(request)
