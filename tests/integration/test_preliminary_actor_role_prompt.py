"""R34-day1: opt-in preliminary actor-role prompt. No paid calls.

Bounded extension of the goal-role profile for the observed assurance-provider
misread (provider visit tagged as company M1/M2). The 9-call trial
(ROOT/tests/fixtures/pipeline/preliminary-actor-role-probe) showed
provider track/confidence/dimensions null 3/3, company management 3/3, and
explicit goal metric 3/3 under the tested system:

  GOAL_ROLE_SYSTEM_PROMPT + ACTOR_ROLE_SYSTEM_SUFFIX (832 bytes, sha b1aceb...)

rendered before the existing Output JSON schema. This file pins those exact
bytes and the profile wiring; it changes no schema, no validator, no grade.
"""

import hashlib
import json
from dataclasses import asdict
from uuid import UUID

import pytest
from proofops.adapters.local.upstage import MODEL_PRO4, UpstageProbe
from proofops.application.ports.models import ModelBinding
from proofops.application.tagging.preliminary import (
    CONTEXT_SYSTEM_SUFFIX,
    GOAL_ROLE_SYSTEM_SUFFIX,
    SYSTEM_PROMPT,
    TABLE_ROLE_SYSTEM_SUFFIX,
    TABLE_SCHEMA,
    TABLE_SYSTEM_SUFFIX,
    preliminary_table_request,
    validate_preliminary_table_sources,
)
from proofops.application.tagging.service import TaggingSettings
from proofops.domain.errors import DomainValidationError
from proofops.domain.provenance import canonical_hash

from tests.acceptance.test_citations import TENANT
from tests.acceptance.test_preliminary_table_sources import table_corpus
from tests.integration.test_upstage_preliminary_transport import (  # noqa: F401
    _freeze_upstage_price_clock,
)

# Existing chains stay byte-identical (goal hash pinned from the table corpus run).
PLAIN_SHA = "7237cccf4035a56d5cdf4fe0133d254e2fe54c65fc8f7f5552c9c4c9ec395429"
CONTEXT_SHA = "aa152297b41f4759ada2fad035b0c1892ab8310324c0b260b6b9bed25e74ea65"
TABLE_SHA = "54131ba6b4c48cbc9c47db8f77ca1ae3a0de946b0f7732809c051b447f233611"
ROLE_SHA = "19eeb93ef1d1067832f2dd96d5bdd3047d73cb6dbc200cd9b5b5296bbf848263"
GOAL_SHA = "4eed11be505daf17cec21c27e715325540b32c94398cbad3d0f3f84d1bff89a0"

ACTOR_MODEL_PROFILE = "upstage-preliminary-source-quotes-actor-role-v1"
ACTOR_TRANSPORT_VERSION = "preliminary-source-quotes-actor-role-v1"
GOAL_MODEL_PROFILE = "upstage-preliminary-source-quotes-goal-role-v1"


def _actor():
    from proofops.application.tagging.preliminary import ACTOR_ROLE_SYSTEM_SUFFIX
    from proofops_agent.upstage_preliminary import (
        ACTOR_ROLE_SYSTEM_PROMPT,
        GOAL_ROLE_SYSTEM_PROMPT,
    )

    return ACTOR_ROLE_SYSTEM_SUFFIX, ACTOR_ROLE_SYSTEM_PROMPT, GOAL_ROLE_SYSTEM_PROMPT


def test_existing_prompt_chains_keep_pinned_bytes():
    assert canonical_hash(SYSTEM_PROMPT) == PLAIN_SHA
    assert canonical_hash(SYSTEM_PROMPT + CONTEXT_SYSTEM_SUFFIX) == CONTEXT_SHA
    assert canonical_hash(SYSTEM_PROMPT + CONTEXT_SYSTEM_SUFFIX + TABLE_SYSTEM_SUFFIX) == TABLE_SHA
    assert (
        canonical_hash(
            SYSTEM_PROMPT + CONTEXT_SYSTEM_SUFFIX + TABLE_SYSTEM_SUFFIX + TABLE_ROLE_SYSTEM_SUFFIX
        )
        == ROLE_SHA
    )
    assert (
        canonical_hash(
            SYSTEM_PROMPT
            + CONTEXT_SYSTEM_SUFFIX
            + TABLE_SYSTEM_SUFFIX
            + TABLE_ROLE_SYSTEM_SUFFIX
            + GOAL_ROLE_SYSTEM_SUFFIX
        )
        == GOAL_SHA
    )


def test_actor_suffix_is_exact_tested_bytes():
    suffix, _, _ = _actor()
    raw = suffix.encode("utf-8")
    assert len(raw) == 832
    assert \
        hashlib.sha256(raw).hexdigest() == (
            "b1acebbb3d47b5d96d76ec04afa8ec79dc2d3d96476ea0c62d37fd6b68ca1782"
        )
    assert suffix.startswith(" First distinguish the actor of the asserted action.")
    assert suffix.endswith("All source-index, exact quote and no-grade rules remain unchanged.")


def test_actor_prompt_is_goal_plus_suffix():
    _, actor_prompt, goal_prompt = _actor()
    suffix, _, _ = _actor()
    assert actor_prompt == goal_prompt + suffix
    assert actor_prompt.startswith(goal_prompt)


def test_actor_differs_from_goal_only_in_prompt_sha256():
    graph, claim, _ = table_corpus()
    goal = preliminary_table_request(
        claim, graph, tenant_id=TENANT, role_resolution=True, goal_role=True
    )
    actor = preliminary_table_request(
        claim, graph, tenant_id=TENANT, role_resolution=True, goal_role=True, actor_role=True
    )
    assert set(goal) == set(actor)
    assert [k for k in goal if goal[k] != actor[k]] == ["prompt_sha256"]
    assert goal["prompt_sha256"] == GOAL_SHA
    assert actor["prompt_sha256"] == canonical_hash(_actor()[1])
    assert actor["schema"] == TABLE_SCHEMA


def test_actor_false_yields_exact_goal_hash():
    graph, claim, _ = table_corpus()
    base = preliminary_table_request(
        claim, graph, tenant_id=TENANT, role_resolution=True, goal_role=True
    )
    with_false = preliminary_table_request(
        claim, graph, tenant_id=TENANT, role_resolution=True, goal_role=True, actor_role=False
    )
    assert base == with_false
    assert with_false["prompt_sha256"] == GOAL_SHA


def test_actor_requires_goal_role():
    graph, claim, _ = table_corpus()
    with pytest.raises(DomainValidationError):
        preliminary_table_request(
            claim, graph, tenant_id=TENANT, role_resolution=True, goal_role=False, actor_role=True
        )
    with pytest.raises(DomainValidationError):
        preliminary_table_request(
            claim, graph, tenant_id=TENANT, role_resolution=True, goal_role="yes", actor_role=True
        )
    with pytest.raises(DomainValidationError):
        preliminary_table_request(
            claim, graph, tenant_id=TENANT, role_resolution=True, goal_role=True, actor_role="yes"
        )


def _configured(tmp_path, monkeypatch, *, profile, prompt, actor_role, period_role=False):
    from proofops_agent.upstage_preliminary import ACTOR_ROLE_TRANSPORT_VERSION

    graph, claim, _ = table_corpus()
    envelope = preliminary_table_request(
        claim,
        graph,
        tenant_id=TENANT,
        role_resolution=True,
        goal_role=True,
        actor_role=actor_role,
        period_role=period_role,
    )
    settings = TaggingSettings(
        ModelBinding("00000000-0000-4000-8000-000000000001", "tagger", False),
        MODEL_PRO4,
        profile,
        "provider-managed-unverified",
        prompt,
        json.dumps({"type": "object"}),
        max_tokens=1024,
    )
    probe = UpstageProbe("test-not-a-key", tmp_path / "budget.sqlite3", model=MODEL_PRO4)
    monkeypatch.setattr(probe, "_post", lambda body: pytest.fail("no provider call in this test"))
    from proofops.application.preflight import check_local_upstage_tagger

    from tests.integration.test_upstage_tagger_preflight import configured as approvals

    authorization = approvals()
    authorization.pop("settings")
    authorization["binding"].update(
        model_id=settings.model_id, tagging_settings_sha256=canonical_hash(asdict(settings))
    )
    from proofops_agent.upstage_preliminary import UpstagePreliminaryTransport

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
        request_id=str(UUID(int=1034)),
        request_signature=canonical_hash("fixture"),
        binding=asdict(settings.binding),
        model_id=settings.model_id,
        model_profile=settings.model_profile,
        region=settings.region,
        system_prompt=settings.rendered_system,
        temperature=0,
        max_tokens=100,
        user_json=json.dumps(envelope, ensure_ascii=False),
    )
    assert ACTOR_ROLE_TRANSPORT_VERSION == ACTOR_TRANSPORT_VERSION
    return adapter, request, envelope, claim, graph


def test_actor_profile_accepts_own_envelope_and_pins_transport_version(tmp_path, monkeypatch):
    from proofops_agent.upstage_preliminary import ACTOR_ROLE_SYSTEM_PROMPT

    adapter, request, envelope, _, _ = _configured(
        tmp_path,
        monkeypatch,
        profile=ACTOR_MODEL_PROFILE,
        prompt=ACTOR_ROLE_SYSTEM_PROMPT,
        actor_role=True,
    )
    assert adapter.TRANSPORT_VERSION == ACTOR_TRANSPORT_VERSION
    system, wire_user, _, _ = adapter._wire_request(request)
    assert ACTOR_ROLE_SYSTEM_PROMPT in system
    assert json.loads(wire_user) == json.loads(json.dumps(envelope))


def test_actor_and_goal_prompts_can_never_be_swapped(tmp_path, monkeypatch):
    from proofops_agent.upstage_preliminary import (
        ACTOR_ROLE_SYSTEM_PROMPT,
        GOAL_ROLE_SYSTEM_PROMPT,
    )

    adapter, request, _, _, _ = _configured(
        tmp_path, monkeypatch, profile=GOAL_MODEL_PROFILE, prompt=GOAL_ROLE_SYSTEM_PROMPT,
        actor_role=True,
    )
    with pytest.raises(ValueError, match="UPSTAGE_PRELIMINARY_PROMPT_INVALID"):
        adapter._wire_request(request)
    adapter, request, _, _, _ = _configured(
        tmp_path / "b", monkeypatch, profile=ACTOR_MODEL_PROFILE,
        prompt=ACTOR_ROLE_SYSTEM_PROMPT, actor_role=False,
    )
    with pytest.raises(ValueError, match="UPSTAGE_PRELIMINARY_PROMPT_INVALID"):
        adapter._wire_request(request)


def test_wrong_prompt_for_actor_profile_refused_before_spend(tmp_path, monkeypatch):
    from proofops_agent.upstage_preliminary import GOAL_ROLE_SYSTEM_PROMPT

    with pytest.raises(ValueError, match="UPSTAGE_TAGGING_BINDING_INVALID"):
        _configured(
            tmp_path, monkeypatch, profile=ACTOR_MODEL_PROFILE,
            prompt=GOAL_ROLE_SYSTEM_PROMPT, actor_role=True,
        )


def test_null_track_with_all_null_dimensions_validates_no_grade(tmp_path, monkeypatch):
    """Observed provider-visit shape: track null, confidence null, all dims null."""
    _, _, envelope, claim, graph = _configured(
        tmp_path, monkeypatch, profile=ACTOR_MODEL_PROFILE,
        prompt=_actor()[1], actor_role=True,
    )
    assert envelope["schema"] == TABLE_SCHEMA
    response = dict(
        claim_id=claim.claim_id,
        track=None,
        safe_harbor_category=None,
        track_confidence=None,
        dimensions=dict(entity=None, metric=None, reporting_period=None),
    )
    result = validate_preliminary_table_sources(claim, graph, response, tenant_id=TENANT)
    assert result.track is None and result.track_confidence is None
    assert all(v is None for v in result.context.dimensions.values())
    with pytest.raises(DomainValidationError):
        validate_preliminary_table_sources(
            claim, graph, dict(response, track_confidence=0.5), tenant_id=TENANT
        )


def test_null_consensus_returns_none_without_element_dispatch(tmp_path, monkeypatch):
    """A null validated consensus must block before any element tagging call."""
    from tests.integration.test_live_tagging_worker import configured as live_configured

    runtime, claim, graph, calls, _, _ = live_configured(tmp_path, monkeypatch)
    _, _, envelope, _, _ = _configured(
        tmp_path / "actor", monkeypatch, profile=ACTOR_MODEL_PROFILE,
        prompt=_actor()[1], actor_role=True,
    )
    assert envelope["schema"] == TABLE_SCHEMA
    null_response = dict(
        claim_id=claim.claim_id,
        track=None,
        safe_harbor_category=None,
        track_confidence=None,
        dimensions=dict(entity=None, metric=None, reporting_period=None),
    )
    # The shared table validator accepts the null consensus for any table envelope.
    from tests.acceptance.test_preliminary_table_sources import table_corpus as corpus

    g2, c2, _ = corpus()
    null_result = validate_preliminary_table_sources(
        c2, g2,
        dict(null_response, claim_id=c2.claim_id),
        tenant_id=TENANT,
    )
    assert null_result.track is None
    element_calls = []
    monkeypatch.setattr(
        runtime.element_transport, "invoke",
        lambda request: element_calls.append(request) or pytest.fail("no element dispatch on null"),
    )
    monkeypatch.setattr(runtime, "_source_replicas", lambda *a, **k: [null_result])
    assert runtime.preliminary(claim, graph) is None
    assert element_calls == []


def test_preflight_allowlist_accepts_actor_pair_and_rejects_mismatch():
    from proofops.application.preflight import check_local_upstage_tagger
    from proofops_agent.upstage_preliminary import (
        ACTOR_ROLE_SYSTEM_PROMPT,
        GOAL_ROLE_SYSTEM_PROMPT,
    )

    from tests.integration.test_upstage_tagger_preflight import configured as approvals

    def _settings(profile, prompt):
        return TaggingSettings(
            ModelBinding("00000000-0000-4000-8000-000000000001", "tagger", False),
            MODEL_PRO4, profile, "provider-managed-unverified", prompt,
            json.dumps({"type": "object"}), max_tokens=1024,
        )

    settings = _settings(ACTOR_MODEL_PROFILE, ACTOR_ROLE_SYSTEM_PROMPT)
    authorization = approvals()
    authorization.pop("settings")
    authorization["binding"].update(
        model_id=settings.model_id, tagging_settings_sha256=canonical_hash(asdict(settings))
    )
    assert check_local_upstage_tagger(settings=settings, **authorization).ready
    wrong = _settings(ACTOR_MODEL_PROFILE, GOAL_ROLE_SYSTEM_PROMPT)
    authorization2 = approvals()
    authorization2.pop("settings")
    authorization2["binding"].update(
        model_id=wrong.model_id, tagging_settings_sha256=canonical_hash(asdict(wrong))
    )
    assert not check_local_upstage_tagger(settings=wrong, **authorization2).ready


def test_pilot_actor_settings_select_own_pair_and_require_goal_role():
    from proofops_agent.upstage_preliminary import (
        ACTOR_ROLE_MODEL_PROFILE_V2,
        ACTOR_ROLE_SYSTEM_PROMPT_V2,
        GOAL_ROLE_SYSTEM_PROMPT,
    )

    from evaluation.local_upstage_pilot import live_tagging_settings

    goal = live_tagging_settings(
        12, preliminary_context=True, preliminary_table_context=True,
        preliminary_table_role=True, preliminary_goal_role=True,
    )
    assert goal["preliminary_settings"]["model_profile"] == GOAL_MODEL_PROFILE
    enabled = live_tagging_settings(
        12, preliminary_context=True, preliminary_table_context=True,
        preliminary_table_role=True, preliminary_goal_role=True, preliminary_actor_role=True,
    )
    assert enabled["preliminary_settings"]["model_profile"] == ACTOR_ROLE_MODEL_PROFILE_V2
    assert enabled["preliminary_settings"]["system_prompt"] == ACTOR_ROLE_SYSTEM_PROMPT_V2
    assert enabled["preliminary_settings"]["system_prompt"].startswith(
        goal["preliminary_settings"]["system_prompt"]
    )
    assert GOAL_ROLE_SYSTEM_PROMPT in enabled["preliminary_settings"]["system_prompt"]
    for bad in (1, "yes", None):
        with pytest.raises(ValueError):
            live_tagging_settings(
                12, preliminary_context=True, preliminary_table_context=True,
                preliminary_table_role=True, preliminary_goal_role=True,
                preliminary_actor_role=bad,
            )
    with pytest.raises(ValueError):
        live_tagging_settings(
            12, preliminary_context=True, preliminary_table_context=True,
            preliminary_table_role=True, preliminary_goal_role=False,
            preliminary_actor_role=True,
        )


def test_pilot_resume_restores_and_defaults_actor_flag():
    from argparse import Namespace

    from evaluation.local_upstage_pilot import apply_resume_metadata

    def _resume_args(**overrides):
        base = dict(
            pdf=None, report_year=None, period_start=None, period_end=None, pages="1",
            claim_pages=None, model="solar-pro3", verify_paragraphs=False, verify_tables=False,
            verify_merged_tables=False, verify_selected_cells=False,
            native_quote_typography=False, repair_table_headers=False, verify_claim_spans=False,
            raster_ocr=False, live_tagging=False, live_relations=False, preliminary_context=False,
            preliminary_table_context=False, preliminary_table_role=False,
            preliminary_goal_role=False, preliminary_actor_role=False,
            extraction_year_notation=False, extraction_context=False,
            extraction_table_context=False, extraction_source_ids=False,
            extraction_assertion_prompt=False, extraction_complete_selection=False,
            claim_span_render_resolution=False, claim_span_bullet_spacing=False,
            claim_span_typography=False, tagging_max_calls=12, extraction_total_calls=None,
            max_calls=8,
        )
        base.update(overrides)
        return Namespace(**base)

    restored = _resume_args()
    apply_resume_metadata(
        restored,
        {"source_path": "/tmp/elsewhere.pdf", "preliminary_context": True,
         "preliminary_table_context": True, "preliminary_table_role": True,
         "preliminary_goal_role": True, "preliminary_actor_role": True},
    )
    assert restored.preliminary_actor_role is True
    legacy = _resume_args()
    apply_resume_metadata(legacy, {"source_path": "/tmp/elsewhere.pdf"})
    assert legacy.preliminary_actor_role is False


def test_pilot_resume_cannot_add_actor_to_legacy_run(tmp_path, monkeypatch, capsys):
    import sys

    import evaluation.local_upstage_pilot as pilot

    state = tmp_path / "legacy-run"
    state.mkdir()
    (state / "pilot.json").write_text(
        json.dumps({"source_path": "/tmp/elsewhere.pdf", "live_tagging": True,
                    "preliminary_context": True, "preliminary_table_context": True,
                    "preliminary_table_role": True, "preliminary_goal_role": True})
    )
    monkeypatch.setattr(
        sys, "argv",
        ["local_upstage_pilot", "--resume", "--state", str(state), "--live-tagging",
         "--preliminary-context", "--preliminary-table-context", "--preliminary-table-role",
         "--preliminary-goal-role", "--preliminary-actor-role",
         "--key-file", str(tmp_path / "absent.key")],
    )
    with pytest.raises(SystemExit):
        pilot.main()
    assert "--resume cannot add preliminary actor role" in capsys.readouterr().err

def test_preflight_allowlist_accepts_actor_v2():
    from proofops.application.preflight import check_local_upstage_tagger
    from proofops_agent.upstage_preliminary import (
        ACTOR_ROLE_MODEL_PROFILE_V2,
        ACTOR_ROLE_SYSTEM_PROMPT_V2,
    )

    from tests.integration.test_upstage_tagger_preflight import configured as approvals

    def _settings(profile, prompt):
        return TaggingSettings(
            ModelBinding("00000000-0000-4000-8000-000000000001", "tagger", False),
            MODEL_PRO4, profile, "provider-managed-unverified", prompt,
            json.dumps({"type": "object"}), max_tokens=1024,
        )

    settings = _settings(ACTOR_ROLE_MODEL_PROFILE_V2, ACTOR_ROLE_SYSTEM_PROMPT_V2)
    authorization = approvals()
    authorization.pop("settings")
    authorization["binding"].update(
        model_id=settings.model_id, tagging_settings_sha256=canonical_hash(asdict(settings))
    )
    assert check_local_upstage_tagger(settings=settings, **authorization).ready


def test_actor_v2_transport_accepts_exact_v2_and_rejects_v1_packet(tmp_path, monkeypatch):
    from proofops_agent.upstage_preliminary import (
        ACTOR_ROLE_MODEL_PROFILE_V2,
        ACTOR_ROLE_SYSTEM_PROMPT_V2,
        ACTOR_ROLE_TRANSPORT_VERSION_V2,
    )
    adapter, request, packet, claim, graph = _configured(
        tmp_path, monkeypatch, profile=ACTOR_ROLE_MODEL_PROFILE_V2,
        prompt=ACTOR_ROLE_SYSTEM_PROMPT_V2, actor_role=True, period_role=True,
    )
    assert adapter.TRANSPORT_VERSION == ACTOR_ROLE_TRANSPORT_VERSION_V2
    assert json.loads(adapter._wire_request(request)[1]) == json.loads(json.dumps(packet))
    legacy = preliminary_table_request(claim, graph, tenant_id=TENANT,
        role_resolution=True, goal_role=True, actor_role=True)
    assert [key for key in packet if packet[key] != legacy[key]] == ['prompt_sha256']
    with pytest.raises(ValueError):
        adapter._wire_request(dict(request, user_json=json.dumps(legacy)))


def test_worker_builds_the_pinned_actor_v2_packet():
    from types import SimpleNamespace

    from proofops_agent.upstage_preliminary import ACTOR_ROLE_SYSTEM_PROMPT_V2
    from proofops_worker.live_tagging import LiveTaggingRuntime
    graph, claim, _ = table_corpus()
    seen=[]
    def replicas(role, claim, packet, *args):
        seen.append(packet)
        assert packet['prompt_sha256'] == canonical_hash(ACTOR_ROLE_SYSTEM_PROMPT_V2)
        return None
    runtime=SimpleNamespace(
        auth=SimpleNamespace(tenant_id=TENANT),
        preliminary_settings=SimpleNamespace(model_profile='upstage-preliminary-source-quotes-actor-role-v2'),
        preliminary_transport=SimpleNamespace(bound_context=lambda packet:packet),
        preliminary_records={}, _source_replicas=replicas,
    )
    assert LiveTaggingRuntime.preliminary(runtime,claim,graph) is None
    assert len(seen)==1


def test_actor_v2_suffix_is_the_evaluated_period_role_prompt():
    from hashlib import sha256

    from proofops.application.tagging.preliminary import PERIOD_ROLE_SYSTEM_SUFFIX
    assert sha256(PERIOD_ROLE_SYSTEM_SUFFIX.encode()).hexdigest() == (
        'b6bde180178474df6e8939816563730f759c91d7f00087467c7f5fc9621a141a'
    )
