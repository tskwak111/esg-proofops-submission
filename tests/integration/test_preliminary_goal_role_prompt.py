"""R34: the opt-in preliminary goal-role prompt profile. No paid calls.

Three things are under test and nothing else:

* the four existing prompt chains keep their exact bytes, so every stored
  receipt still replays under its own prompt hash;
* the new goal-role profile differs from the table-role profile ONLY in
  ``prompt_sha256``, and the wire shape / source list / both policies are
  byte-identical to the table-role envelope;
* the profile pair is pinned so neither the table-role prompt nor any older
  prompt can be sent under the goal-role profile and vice versa.

No real model calls, no grades, no accuracy claims.
"""

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
from proofops_agent.upstage_preliminary import (
    GOAL_ROLE_MODEL_PROFILE,
    GOAL_ROLE_SYSTEM_PROMPT,
    GOAL_ROLE_TRANSPORT_VERSION,
    TABLE_ROLE_MODEL_PROFILE,
    TABLE_ROLE_SYSTEM_PROMPT,
    TABLE_ROLE_TRANSPORT_VERSION,
    UpstagePreliminaryTransport,
)

from tests.acceptance.test_citations import TENANT
from tests.acceptance.test_preliminary_table_sources import table_corpus
from tests.integration.test_upstage_preliminary_transport import (  # noqa: F401
    _freeze_upstage_price_clock,
)

# Pinned so a later prompt edit fails here instead of silently reinterpreting a
# stored receipt under a prompt it was never sent with.
PLAIN_SHA = "7237cccf4035a56d5cdf4fe0133d254e2fe54c65fc8f7f5552c9c4c9ec395429"
CONTEXT_SHA = "aa152297b41f4759ada2fad035b0c1892ab8310324c0b260b6b9bed25e74ea65"
TABLE_SHA = "54131ba6b4c48cbc9c47db8f77ca1ae3a0de946b0f7732809c051b447f233611"
ROLE_SHA = "19eeb93ef1d1067832f2dd96d5bdd3047d73cb6dbc200cd9b5b5296bbf848263"
# The pinned hash for the goal-role prompt (table-role + GOAL_ROLE_SYSTEM_SUFFIX).
GOAL_ROLE_SHA = canonical_hash(
    SYSTEM_PROMPT
    + CONTEXT_SYSTEM_SUFFIX
    + TABLE_SYSTEM_SUFFIX
    + TABLE_ROLE_SYSTEM_SUFFIX
    + GOAL_ROLE_SYSTEM_SUFFIX
)


def test_the_existing_prompt_chains_keep_their_pinned_bytes():
    assert canonical_hash(SYSTEM_PROMPT) == PLAIN_SHA
    assert canonical_hash(SYSTEM_PROMPT + CONTEXT_SYSTEM_SUFFIX) == CONTEXT_SHA
    assert canonical_hash(SYSTEM_PROMPT + CONTEXT_SYSTEM_SUFFIX + TABLE_SYSTEM_SUFFIX) == TABLE_SHA
    assert canonical_hash(TABLE_ROLE_SYSTEM_PROMPT) == ROLE_SHA


def test_goal_role_suffix_bytes_match_the_pinned_file():
    """The suffix constant must match the 729-byte file the coordinator approved."""
    import hashlib

    suffix_bytes = GOAL_ROLE_SYSTEM_SUFFIX.encode("utf-8")
    assert len(suffix_bytes) == 729
    assert (
        hashlib.sha256(suffix_bytes).hexdigest()
        == "725cf2b65001209cde2586b8f2ff16e2c2cae2eaf724c56b898b19d0befc7d13"
    )


def test_goal_role_prompt_is_table_role_plus_suffix():
    assert GOAL_ROLE_SYSTEM_PROMPT == TABLE_ROLE_SYSTEM_PROMPT + GOAL_ROLE_SYSTEM_SUFFIX
    assert GOAL_ROLE_SYSTEM_PROMPT.startswith(TABLE_ROLE_SYSTEM_PROMPT)


def test_goal_role_prompt_hash():
    assert canonical_hash(GOAL_ROLE_SYSTEM_PROMPT) == GOAL_ROLE_SHA


def test_goal_role_differs_from_table_role_only_in_prompt_sha256():
    graph, claim, _ = table_corpus()
    table_role = preliminary_table_request(claim, graph, tenant_id=TENANT, role_resolution=True)
    goal_role = preliminary_table_request(
        claim, graph, tenant_id=TENANT, role_resolution=True, goal_role=True
    )
    assert set(table_role) == set(goal_role)
    differing = [key for key in table_role if table_role[key] != goal_role[key]]
    assert differing == ["prompt_sha256"]
    assert table_role["prompt_sha256"] == ROLE_SHA
    assert goal_role["prompt_sha256"] == GOAL_ROLE_SHA
    assert goal_role["schema"] == TABLE_SCHEMA  # same wire schema, no new envelope


def test_goal_role_false_yields_same_hash_as_plain_table_role():
    graph, claim, _ = table_corpus()
    with_false = preliminary_table_request(
        claim, graph, tenant_id=TENANT, role_resolution=True, goal_role=False
    )
    without = preliminary_table_request(claim, graph, tenant_id=TENANT, role_resolution=True)
    assert with_false == without


def test_goal_role_requires_role_resolution():
    graph, claim, _ = table_corpus()
    with pytest.raises(DomainValidationError):
        preliminary_table_request(
            claim, graph, tenant_id=TENANT, role_resolution=False, goal_role=True
        )


def test_goal_role_must_be_boolean():
    graph, claim, _ = table_corpus()
    with pytest.raises(DomainValidationError):
        preliminary_table_request(
            claim, graph, tenant_id=TENANT, role_resolution=True, goal_role="yes"
        )


def _configured(tmp_path, monkeypatch, *, profile, prompt, goal_role, role_resolution=True):
    graph, claim, _ = table_corpus()
    envelope = preliminary_table_request(
        claim, graph, tenant_id=TENANT, role_resolution=role_resolution, goal_role=goal_role
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
    return adapter, request, envelope, claim, graph


def test_goal_role_profile_accepts_its_own_envelope_and_pins_transport_version(
    tmp_path, monkeypatch
):
    adapter, request, envelope, _, _ = _configured(
        tmp_path,
        monkeypatch,
        profile=GOAL_ROLE_MODEL_PROFILE,
        prompt=GOAL_ROLE_SYSTEM_PROMPT,
        goal_role=True,
    )
    assert adapter.TRANSPORT_VERSION == GOAL_ROLE_TRANSPORT_VERSION
    system, wire_user, _, _, _ = adapter._wire_request(request)
    assert GOAL_ROLE_SYSTEM_PROMPT in system
    assert json.loads(wire_user) == json.loads(json.dumps(envelope))


def test_goal_role_and_table_role_prompts_can_never_be_swapped(tmp_path, monkeypatch):
    """A goal-role packet under the table-role profile, and vice versa."""
    # goal_role packet under table-role profile: prompt hash mismatch
    adapter, request, _, _, _ = _configured(
        tmp_path,
        monkeypatch,
        profile=TABLE_ROLE_MODEL_PROFILE,
        prompt=TABLE_ROLE_SYSTEM_PROMPT,
        goal_role=True,
    )
    with pytest.raises(ValueError, match="UPSTAGE_PRELIMINARY_PROMPT_INVALID"):
        adapter._wire_request(request)
    # table-role packet under goal-role profile: prompt hash mismatch
    adapter, request, _, _, _ = _configured(
        tmp_path / "b",
        monkeypatch,
        profile=GOAL_ROLE_MODEL_PROFILE,
        prompt=GOAL_ROLE_SYSTEM_PROMPT,
        goal_role=False,
    )
    with pytest.raises(ValueError, match="UPSTAGE_PRELIMINARY_PROMPT_INVALID"):
        adapter._wire_request(request)


def test_wrong_prompt_for_goal_role_profile_is_refused_before_any_spend(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="UPSTAGE_TAGGING_BINDING_INVALID"):
        _configured(
            tmp_path,
            monkeypatch,
            profile=GOAL_ROLE_MODEL_PROFILE,
            prompt=TABLE_ROLE_SYSTEM_PROMPT,  # wrong: table-role, not goal-role
            goal_role=True,
        )


def test_goal_role_profile_reuses_the_unchanged_table_validator(tmp_path, monkeypatch):
    """The goal-role profile uses the same source-bound table validator."""
    _, _, envelope, claim, graph = _configured(
        tmp_path,
        monkeypatch,
        profile=GOAL_ROLE_MODEL_PROFILE,
        prompt=GOAL_ROLE_SYSTEM_PROMPT,
        goal_role=True,
    )
    offered = envelope["untrusted_document_data"]["sources"]
    # Find any table-role source that has text "배출량" (from the table_corpus fixture).
    measure_idx = next(
        (entry["source_index"] for entry in offered if entry.get("text") == "배출량"), None
    )
    if measure_idx is not None:
        response = dict(
            claim_id=claim.claim_id,
            track=None,
            safe_harbor_category=None,
            track_confidence=None,
            dimensions=dict(
                entity=None,
                metric={"source_index": measure_idx, "quote": "배출량"},
                reporting_period=None,
            ),
        )
        result = validate_preliminary_table_sources(claim, graph, response, tenant_id=TENANT)
        metric = result.context.dimensions["metric"]
        assert metric.verification_state == "verified" and metric.quote == "배출량"
        assert result.track is None  # a resolved metric never buys a track or a grade


def test_goal_role_transport_version_differs_from_table_role(tmp_path, monkeypatch):
    """Different transport versions prevent cross-replay of receipts."""
    assert GOAL_ROLE_TRANSPORT_VERSION != TABLE_ROLE_TRANSPORT_VERSION
    adapter_goal, _, _, _, _ = _configured(
        tmp_path,
        monkeypatch,
        profile=GOAL_ROLE_MODEL_PROFILE,
        prompt=GOAL_ROLE_SYSTEM_PROMPT,
        goal_role=True,
    )
    assert adapter_goal.TRANSPORT_VERSION == GOAL_ROLE_TRANSPORT_VERSION


def test_preflight_allowlist_accepts_goal_role_profile(tmp_path):
    """check_local_upstage_tagger accepts the new goal-role profile pair."""
    from proofops.application.preflight import check_local_upstage_tagger

    from tests.integration.test_upstage_tagger_preflight import configured as approvals

    settings = TaggingSettings(
        ModelBinding("00000000-0000-4000-8000-000000000001", "tagger", False),
        MODEL_PRO4,
        GOAL_ROLE_MODEL_PROFILE,
        "provider-managed-unverified",
        GOAL_ROLE_SYSTEM_PROMPT,
        json.dumps({"type": "object"}),
        max_tokens=1024,
    )
    authorization = approvals()
    authorization.pop("settings")
    authorization["binding"].update(
        model_id=settings.model_id,
        tagging_settings_sha256=canonical_hash(asdict(settings)),
    )
    result = check_local_upstage_tagger(settings=settings, **authorization)
    assert result.ready


def test_preflight_allowlist_rejects_goal_role_with_wrong_prompt(tmp_path):
    """A goal-role profile with a mismatched prompt must be refused."""
    from proofops.application.preflight import check_local_upstage_tagger

    from tests.integration.test_upstage_tagger_preflight import configured as approvals

    # Use table-role prompt under goal-role profile name: must fail.
    # NOTE: binding id matches the fixture approval (000...0001) so the
    # refusal is due to the prompt/profile mismatch, not a binding mismatch.
    settings = TaggingSettings(
        ModelBinding("00000000-0000-4000-8000-000000000001", "tagger", False),
        MODEL_PRO4,
        GOAL_ROLE_MODEL_PROFILE,
        "provider-managed-unverified",
        TABLE_ROLE_SYSTEM_PROMPT,  # wrong prompt for this profile
        json.dumps({"type": "object"}),
        max_tokens=1024,
    )
    authorization = approvals()
    authorization.pop("settings")
    authorization["binding"].update(
        model_id=settings.model_id,
        tagging_settings_sha256=canonical_hash(asdict(settings)),
    )
    result = check_local_upstage_tagger(settings=settings, **authorization)
    assert not result.ready


def test_default_false_yields_exact_old_hashes():
    """goal_role=False must produce the same hash as the prior table-role call."""
    graph, claim, _ = table_corpus()
    base = preliminary_table_request(claim, graph, tenant_id=TENANT, role_resolution=True)
    with_false = preliminary_table_request(
        claim, graph, tenant_id=TENANT, role_resolution=True, goal_role=False
    )
    assert base["prompt_sha256"] == with_false["prompt_sha256"] == ROLE_SHA


# ------------------------------------------------- pilot settings/resume/pin
def test_pilot_goal_role_settings_select_own_profile_and_prompt():
    """NEW-run opt-in: goal flag selects its own profile/prompt pair."""
    from evaluation.local_upstage_pilot import live_tagging_settings

    default = live_tagging_settings(
        12, preliminary_context=True, preliminary_table_context=True, preliminary_table_role=True
    )
    assert default["preliminary_settings"]["model_profile"] == TABLE_ROLE_MODEL_PROFILE
    assert default["preliminary_settings"]["system_prompt"] == TABLE_ROLE_SYSTEM_PROMPT
    enabled = live_tagging_settings(
        12,
        preliminary_context=True,
        preliminary_table_context=True,
        preliminary_table_role=True,
        preliminary_goal_role=True,
    )
    assert enabled["preliminary_settings"]["model_profile"] == GOAL_ROLE_MODEL_PROFILE
    assert enabled["preliminary_settings"]["system_prompt"] == GOAL_ROLE_SYSTEM_PROMPT
    assert enabled["preliminary_settings"]["system_prompt"].startswith(
        default["preliminary_settings"]["system_prompt"]
    )


def test_pilot_goal_role_flag_must_be_boolean_and_require_table_role():
    from evaluation.local_upstage_pilot import live_tagging_settings

    for bad in (1, "yes", None):
        with pytest.raises(ValueError):
            live_tagging_settings(
                12,
                preliminary_context=True,
                preliminary_table_context=True,
                preliminary_table_role=True,
                preliminary_goal_role=bad,
            )
    with pytest.raises(ValueError):
        live_tagging_settings(
            12,
            preliminary_context=True,
            preliminary_table_context=True,
            preliminary_table_role=False,
            preliminary_goal_role=True,
        )


def test_pilot_resume_restores_and_defaults_the_goal_role_flag():
    from argparse import Namespace

    from evaluation.local_upstage_pilot import apply_resume_metadata

    def _resume_args(**overrides):
        base = dict(
            pdf=None,
            report_year=None,
            period_start=None,
            period_end=None,
            pages="1",
            claim_pages=None,
            model="solar-pro3",
            verify_paragraphs=False,
            verify_tables=False,
            verify_merged_tables=False,
            verify_selected_cells=False,
            native_quote_typography=False,
            repair_table_headers=False,
            verify_claim_spans=False,
            raster_ocr=False,
            live_tagging=False,
            live_relations=False,
            preliminary_context=False,
            preliminary_table_context=False,
            preliminary_table_role=False,
            preliminary_goal_role=False,
            extraction_year_notation=False,
            extraction_context=False,
            extraction_table_context=False,
            extraction_source_ids=False,
            extraction_assertion_prompt=False,
            extraction_complete_selection=False,
            claim_span_render_resolution=False,
            claim_span_bullet_spacing=False,
            claim_span_typography=False,
            tagging_max_calls=12,
            extraction_total_calls=None,
            max_calls=8,
        )
        base.update(overrides)
        return Namespace(**base)

    restored = _resume_args()
    apply_resume_metadata(
        restored,
        {
            "source_path": "/tmp/elsewhere.pdf",
            "preliminary_context": True,
            "preliminary_table_context": True,
            "preliminary_table_role": True,
            "preliminary_goal_role": True,
        },
    )
    assert restored.preliminary_goal_role is True
    legacy = _resume_args()
    apply_resume_metadata(legacy, {"source_path": "/tmp/elsewhere.pdf"})
    assert legacy.preliminary_goal_role is False


def test_pilot_resume_cannot_add_goal_role_to_a_legacy_run(tmp_path, monkeypatch, capsys):
    """The flag is NEW-run only: adding it on --resume exits before any state work."""
    import json
    import sys

    import evaluation.local_upstage_pilot as pilot

    state = tmp_path / "legacy-run"
    state.mkdir()
    (state / "pilot.json").write_text(
        json.dumps(
            {
                "source_path": "/tmp/elsewhere.pdf",
                "live_tagging": True,
                "preliminary_context": True,
                "preliminary_table_context": True,
                "preliminary_table_role": True,
            }
        )
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "local_upstage_pilot",
            "--resume",
            "--state",
            str(state),
            "--live-tagging",
            "--preliminary-context",
            "--preliminary-table-context",
            "--preliminary-table-role",
            "--preliminary-goal-role",
            "--key-file",
            str(tmp_path / "absent.key"),
        ],
    )
    with pytest.raises(SystemExit):
        pilot.main()
    assert "--resume cannot add preliminary goal role" in capsys.readouterr().err
