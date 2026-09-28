import json
from dataclasses import asdict, replace
from uuid import UUID

from proofops.adapters.local.upstage import MODEL_PRO4, UpstageProbe
from proofops.application.ports.models import ModelBinding
from proofops.domain.provenance import canonical_hash
from proofops_agent.upstage_tagging import QUOTE_PROFILE, QUOTE_V4_PROFILE, UpstageTaggingTransport

from tests.acceptance.test_tagging import setup


def configured(tmp_path, monkeypatch, model_profile=QUOTE_V4_PROFILE):
    inputs = setup(tmp_path)
    settings = replace(
        inputs["settings"],
        binding=ModelBinding("00000000-0000-4000-8000-000000000001", "tagger", False),
        model_id=MODEL_PRO4,
        model_profile=model_profile,
        region="provider-managed-unverified",
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

    adapter = UpstageTaggingTransport(
        probe,
        tmp_path / "receipts",
        settings=settings,
        tenant_id=inputs["tenant_id"],
        authorize=lambda selected, req: check_local_upstage_tagger(
            settings=selected, **authorization
        ),
    )
    request = dict(
        tenant_id=inputs["tenant_id"],
        claim_id=inputs["context"].claim.claim_id,
        packet_sha256=inputs["packet"].packet_sha256,
        replicate_id=1,
        request_id=str(UUID(int=987)),
        request_signature=canonical_hash("fixture"),
        binding=asdict(settings.binding),
        model_id=settings.model_id,
        model_profile=settings.model_profile,
        region=settings.region,
        system_prompt=settings.rendered_system
        + "\nValidated classification; tag only its elements: "
        + '{"track":"performance","safe_harbor_category":null}',
        temperature=0,
        max_tokens=100,
    )
    request["user_json"] = json.dumps(
        dict(
            claim_id=request["claim_id"],
            packet_sha256=request["packet_sha256"],
            replicate_id=1,
            untrusted_document_data=dict(allowed_elements=["G1", "P2"]),
        )
    )
    user = json.loads(request["user_json"])
    original = setup(tmp_path)["packet"].to_dict()["evidence_candidates"][0]["source_refs"][0]
    user["untrusted_document_data"]["evidence_candidates"] = [dict(source_refs=[original])]
    # also we need G1 element allowed
    user["untrusted_document_data"]["allowed_elements"] = ["G1", "P2"]
    request["user_json"] = json.dumps(user)
    return adapter, probe, calls, request


def _override_quote(request, quote: str, char_start: int):
    user = json.loads(request["user_json"])
    user["untrusted_document_data"]["evidence_candidates"][0]["source_refs"][0]["quote"] = quote
    user["untrusted_document_data"]["evidence_candidates"][0]["source_refs"][0]["char_start"] = (
        char_start
    )
    request["user_json"] = json.dumps(user)


def test_v4_refinement_exact_match(tmp_path, monkeypatch):
    adapter, probe, calls, request = configured(tmp_path, monkeypatch)
    _override_quote(request, "목표는 2050년까지입니다.", 10)

    def post(body):
        payload = {
            "elements": [
                {
                    "element_id": "G1",
                    "state": "present",
                    "normalized_value": "2050년",
                    "evidence_refs": [{"id": "e0", "quote": "목표는 2050년까지입니다."}],
                }
            ]
        }
        return dict(
            id="fixture-provider",
            model=MODEL_PRO4,
            usage=dict(prompt_tokens=20, completion_tokens=10),
            choices=[dict(finish_reason="stop", message=dict(content=json.dumps(payload)))],
        )

    monkeypatch.setattr(probe, "_post", post)
    response = adapter.invoke(request)
    data = json.loads(response.raw_response_json)
    refs = data["elements"][0]["evidence_refs"]
    assert len(refs) == 2

    derived = refs[1]
    assert derived["quote"] == "2050년"
    assert derived["char_start"] == 14
    assert derived["char_end"] == 19


def test_v4_refinement_rejects_longer_digits(tmp_path, monkeypatch):
    adapter, probe, calls, request = configured(tmp_path, monkeypatch)
    _override_quote(request, "목표는 12050년입니다.", 10)

    def post(body):
        payload = {
            "elements": [
                {
                    "element_id": "G1",
                    "state": "present",
                    "normalized_value": "2050년",
                    "evidence_refs": [{"id": "e0", "quote": "목표는 12050년입니다."}],
                }
            ]
        }
        return dict(
            id="fixture-provider",
            model=MODEL_PRO4,
            usage=dict(prompt_tokens=20, completion_tokens=10),
            choices=[dict(finish_reason="stop", message=dict(content=json.dumps(payload)))],
        )

    monkeypatch.setattr(probe, "_post", post)
    response = adapter.invoke(request)
    data = json.loads(response.raw_response_json)
    refs = data["elements"][0]["evidence_refs"]
    assert len(refs) == 1  # Unchanged


def test_v4_refinement_rejects_ambiguous(tmp_path, monkeypatch):
    adapter, probe, calls, request = configured(tmp_path, monkeypatch)
    _override_quote(request, "This is ambiguous 2050년 and 2050년 again.", 0)

    def post(body):
        payload = {
            "elements": [
                {
                    "element_id": "G1",
                    "state": "present",
                    "normalized_value": "2050년",
                    "evidence_refs": [
                        {"id": "e0", "quote": "This is ambiguous 2050년 and 2050년 again."}
                    ],
                }
            ]
        }
        return dict(
            id="fixture-provider",
            model=MODEL_PRO4,
            usage=dict(prompt_tokens=20, completion_tokens=10),
            choices=[dict(finish_reason="stop", message=dict(content=json.dumps(payload)))],
        )

    monkeypatch.setattr(probe, "_post", post)
    response = adapter.invoke(request)
    data = json.loads(response.raw_response_json)
    refs = data["elements"][0]["evidence_refs"]
    assert len(refs) == 1  # Unchanged


def test_v3_profile_remains_unchanged(tmp_path, monkeypatch):
    adapter, probe, calls, request = configured(tmp_path, monkeypatch, model_profile=QUOTE_PROFILE)
    _override_quote(request, "목표는 2050년까지입니다.", 10)

    def post(body):
        payload = {
            "elements": [
                {
                    "element_id": "G1",
                    "state": "present",
                    "normalized_value": "2050년",
                    "evidence_refs": [{"id": "e0", "quote": "목표는 2050년까지입니다."}],
                }
            ]
        }
        return dict(
            id="fixture-provider",
            model=MODEL_PRO4,
            usage=dict(prompt_tokens=20, completion_tokens=10),
            choices=[dict(finish_reason="stop", message=dict(content=json.dumps(payload)))],
        )

    monkeypatch.setattr(probe, "_post", post)
    response = adapter.invoke(request)
    data = json.loads(response.raw_response_json)
    refs = data["elements"][0]["evidence_refs"]
    assert len(refs) == 1  # Unchanged, v4 logic not applied


def test_refinement_preserves_qualifiers_identity_and_rejects_unsafe_candidates():
    from copy import deepcopy

    original = dict(quote="2050년까지", char_start=30, char_end=37,
                    document_version_id="source-a", page=22, bbox=[1, 2, 3, 4],
                    verification_state="verified", source_hash="unchanged")
    element = dict(element_id="G1", state="present", normalized_value="2050년",
                   evidence_refs=[original])
    UpstageTaggingTransport._refine_g1_year(element)
    assert element["evidence_refs"] == [original, dict(original, quote="2050년", char_end=35)]
    before = deepcopy(element)
    UpstageTaggingTransport._refine_g1_year(element)
    assert element == before  # Idempotent; retain the original deadline qualifier.
    for changes in (
        dict(state="unknown"), dict(state="absent"), dict(element_id="G2"),
        dict(normalized_value="２０５０년"), dict(normalized_value="0000년"),
        dict(normalized_value="2050"), dict(normalized_value="2050년까지"),
        dict(evidence_refs=[dict(original, quote="12050년")]),
        dict(evidence_refs=[dict(original, quote="2040년")]),
        dict(evidence_refs=[original, dict(original, document_version_id="source-b")]),
        dict(evidence_refs=[original, dict(original, quote="2050년부터 2050년까지")]),
    ):
        candidate = dict(element_id="G1", state="present", normalized_value="2050년",
                         evidence_refs=[original])
        candidate.update(changes)
        before = deepcopy(candidate)
        UpstageTaggingTransport._refine_g1_year(candidate)
        assert candidate == before
