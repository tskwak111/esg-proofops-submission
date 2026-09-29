"""R16: the opt-in table role-resolution prompt profile. No paid calls.

Two things are under test and nothing else:

* the three existing prompt chains and the existing table envelope keep their
  exact bytes, so every stored receipt still replays under its own prompt hash;
* the new profile differs from the table profile ONLY in ``prompt_sha256``, and
  the four real solar-pro4 responses the coordinator bought under that exact
  prompt hash satisfy the source-bound checks that authorised the integration.

The real responses are replayed from disk. They are model output on real (case
A/B/C) or synthetic (case D) sources, reviewed against the coordinator's
adjudication; they are not human gold, not an accuracy measurement, and they
approve no grade.
"""

import json
from dataclasses import asdict
from pathlib import Path
from uuid import UUID

import pytest
from proofops.adapters.local.upstage import MODEL_PRO4, UpstageProbe
from proofops.application.ports.models import ModelBinding
from proofops.application.tagging.preliminary import (
    CONTEXT_SYSTEM_SUFFIX,
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
    TABLE_MODEL_PROFILE,
    TABLE_ROLE_MODEL_PROFILE,
    TABLE_ROLE_SYSTEM_PROMPT,
    TABLE_ROLE_TRANSPORT_VERSION,
    TABLE_SYSTEM_PROMPT,
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
# The prompt hash the coordinator's four real solar-pro4 calls actually used.
ROLE_SHA = "19eeb93ef1d1067832f2dd96d5bdd3047d73cb6dbc200cd9b5b5296bbf848263"

LIVE = Path(__file__).resolve().parents[2] / "tests/fixtures/pipeline/table-role-resolution"


def test_the_existing_prompt_chains_keep_their_pinned_bytes():
    assert canonical_hash(SYSTEM_PROMPT) == PLAIN_SHA
    assert canonical_hash(SYSTEM_PROMPT + CONTEXT_SYSTEM_SUFFIX) == CONTEXT_SHA
    assert canonical_hash(SYSTEM_PROMPT + CONTEXT_SYSTEM_SUFFIX + TABLE_SYSTEM_SUFFIX) == TABLE_SHA


def test_the_role_resolution_prompt_pins_the_hash_the_real_calls_used():
    assert canonical_hash(TABLE_ROLE_SYSTEM_PROMPT) == ROLE_SHA
    assert TABLE_ROLE_SYSTEM_PROMPT == TABLE_SYSTEM_PROMPT + TABLE_ROLE_SYSTEM_SUFFIX
    # Additive only: the new text is appended, never interleaved.
    assert TABLE_ROLE_SYSTEM_PROMPT.startswith(TABLE_SYSTEM_PROMPT)


def test_role_resolution_changes_only_the_pinned_prompt_hash():
    graph, claim, _ = table_corpus()
    base = preliminary_table_request(claim, graph, tenant_id=TENANT)
    role = preliminary_table_request(claim, graph, tenant_id=TENANT, role_resolution=True)
    assert set(base) == set(role)
    assert [key for key in base if base[key] != role[key]] == ["prompt_sha256"]
    assert base["prompt_sha256"] == TABLE_SHA
    assert role["prompt_sha256"] == ROLE_SHA
    assert role["schema"] == TABLE_SCHEMA  # same wire schema, no new envelope


def test_the_default_table_envelope_is_unchanged_and_the_flag_must_be_boolean():
    graph, claim, _ = table_corpus()
    assert preliminary_table_request(claim, graph, tenant_id=TENANT) == preliminary_table_request(
        claim, graph, tenant_id=TENANT, role_resolution=False
    )
    with pytest.raises(DomainValidationError):
        preliminary_table_request(claim, graph, tenant_id=TENANT, role_resolution="yes")


def _configured(tmp_path, monkeypatch, *, profile, prompt, role_resolution):
    graph, claim, _ = table_corpus()
    envelope = preliminary_table_request(
        claim, graph, tenant_id=TENANT, role_resolution=role_resolution
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
        request_id=str(UUID(int=1016)),
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


def test_the_role_profile_accepts_its_own_envelope_and_pins_its_transport_version(
    tmp_path, monkeypatch
):
    adapter, request, envelope, _, _ = _configured(
        tmp_path,
        monkeypatch,
        profile=TABLE_ROLE_MODEL_PROFILE,
        prompt=TABLE_ROLE_SYSTEM_PROMPT,
        role_resolution=True,
    )
    assert adapter.TRANSPORT_VERSION == TABLE_ROLE_TRANSPORT_VERSION
    system, wire_user, _, _, _ = adapter._wire_request(request)
    assert TABLE_ROLE_SYSTEM_PROMPT in system
    assert json.loads(wire_user) == json.loads(json.dumps(envelope))


def test_the_two_table_prompts_can_never_be_swapped_under_one_profile(tmp_path, monkeypatch):
    """A role packet on the table profile, and a table packet on the role profile."""
    adapter, request, _, _, _ = _configured(
        tmp_path,
        monkeypatch,
        profile=TABLE_MODEL_PROFILE,
        prompt=TABLE_SYSTEM_PROMPT,
        role_resolution=True,
    )
    with pytest.raises(ValueError, match="UPSTAGE_PRELIMINARY_PROMPT_INVALID"):
        adapter._wire_request(request)
    adapter, request, _, _, _ = _configured(
        tmp_path / "b",
        monkeypatch,
        profile=TABLE_ROLE_MODEL_PROFILE,
        prompt=TABLE_ROLE_SYSTEM_PROMPT,
        role_resolution=False,
    )
    with pytest.raises(ValueError, match="UPSTAGE_PRELIMINARY_PROMPT_INVALID"):
        adapter._wire_request(request)


def test_a_wrong_prompt_for_the_role_profile_is_refused_before_any_spend(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="UPSTAGE_TAGGING_BINDING_INVALID"):
        _configured(
            tmp_path,
            monkeypatch,
            profile=TABLE_ROLE_MODEL_PROFILE,
            prompt=TABLE_SYSTEM_PROMPT,
            role_resolution=True,
        )


def test_the_role_profile_reuses_the_unchanged_table_validator(tmp_path, monkeypatch):
    """Case A's real shape: metric from a column header that names a measure."""
    _, _, envelope, claim, graph = _configured(
        tmp_path,
        monkeypatch,
        profile=TABLE_ROLE_MODEL_PROFILE,
        prompt=TABLE_ROLE_SYSTEM_PROMPT,
        role_resolution=True,
    )
    offered = envelope["untrusted_document_data"]["sources"]
    measure = next(entry["source_index"] for entry in offered if entry["text"] == "배출량")
    response = dict(
        claim_id=claim.claim_id,
        track=None,
        safe_harbor_category=None,
        track_confidence=None,
        dimensions=dict(
            entity=None,
            metric={"source_index": measure, "quote": "배출량"},
            reporting_period=None,
        ),
    )
    result = validate_preliminary_table_sources(claim, graph, response, tenant_id=TENANT)
    metric = result.context.dimensions["metric"]
    assert metric.verification_state == "verified" and metric.quote == "배출량"
    assert result.track is None  # a resolved metric never buys a track or a grade


# --------------------------------------------------------------- real responses
def _live_cases():
    summary = json.loads((LIVE / "summary.json").read_text())
    proposal = json.loads((LIVE / "proposal-used.json").read_text())
    packets = {case["case_id"]: case["packet"] for case in proposal["cases"]}
    return [
        (item["case_id"], item["response"], packets[item["case_id"]]) for item in summary["results"]
    ]


def test_the_real_run_used_exactly_this_prompt():
    manifest = json.loads((LIVE / "manifest.json").read_text())
    assert manifest["prompt_sha256"] == ROLE_SHA == canonical_hash(TABLE_ROLE_SYSTEM_PROMPT)
    for case_id, _, packet in _live_cases():
        assert packet["prompt_sha256"] == ROLE_SHA, case_id
        assert packet["schema"] == TABLE_SCHEMA, case_id


@pytest.mark.parametrize("case_id, response, packet", _live_cases())
def test_every_real_dimension_quote_is_source_bound_and_unique(case_id, response, packet):
    sources = packet["untrusted_document_data"]["sources"]
    for name, span in response["dimensions"].items():
        if span is None:
            continue
        text = sources[span["source_index"]]["text"]
        quote = span["quote"]
        assert text.find(quote) >= 0, (case_id, name)
        assert text.find(quote) == text.rfind(quote), (case_id, name)
        # A dimension may never be read out of a context block.
        assert span["source_index"] < len(sources)


def test_no_real_response_produced_a_refused_counterpart():
    """The counterpart checks that authorised this integration, replayed."""
    by_case = {case_id: response for case_id, response, _ in _live_cases()}
    assert set(by_case) == {
        "A-kb-p30-investment-amount-real",
        "B-kia-p35-hev-2024-actual",
        "C-kia-p35-hev-2025-plan",
        "D-synthetic-activity-row-and-plan-column",
    }
    # Never a track invented from a number, a past year or a resolved metric.
    assert {response["track"] for response in by_case.values()} == {None}
    assert {response["track_confidence"] for response in by_case.values()} == {None}
    assert {response["safe_harbor_category"] for response in by_case.values()} == {None}
    # No entity anywhere: no source names an organization.
    assert all(response["dimensions"]["entity"] is None for response in by_case.values())
    # A: the improvement, a column header that literally names a measure.
    a = by_case["A-kb-p30-investment-amount-real"]["dimensions"]
    assert a["metric"] == {"source_index": 3, "quote": "투자 금액"}
    # A: 2025 is a literal candidate only; it is neither credited as the actual
    # reporting period nor allowed to imply performance (coordinator ruling).
    assert a["reporting_period"] in (None, {"source_index": 4, "quote": "2025"})
    # B: a plain year column header is readable as the period.
    b = by_case["B-kia-p35-hev-2024-actual"]["dimensions"]
    assert b["reporting_period"] == {"source_index": 3, "quote": "2024"}
    assert b["metric"] is None  # 대 is a unit and HEV is a subject, not an indicator
    # C: the same row and table, plan column: the target year is excluded.
    c = by_case["C-kia-p35-hev-2025-plan"]["dimensions"]
    assert c["reporting_period"] is None
    # D: no cell names a measured quantity, so nothing is resolved.
    assert by_case["D-synthetic-activity-row-and-plan-column"]["dimensions"] == dict(
        entity=None, metric=None, reporting_period=None
    )
