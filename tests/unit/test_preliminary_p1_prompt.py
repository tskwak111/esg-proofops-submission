"""R63: P1 changes only the explicitly selected preliminary prompt."""

import json

import pytest
from proofops.application.tagging.preliminary import (
    P1_SYSTEM_PROMPT,
    preliminary_table_request,
)
from proofops.domain.errors import DomainValidationError
from proofops.domain.provenance import canonical_hash
from proofops_agent.upstage_preliminary import (
    P1_MODEL_PROFILE,
    P1_TRANSPORT_VERSION,
    TABLE_ROLE_MODEL_PROFILE,
    TABLE_ROLE_SYSTEM_PROMPT,
)

from tests.acceptance.test_citations import TENANT
from tests.acceptance.test_preliminary_table_sources import table_corpus
from tests.integration.test_preliminary_table_role_prompt import _configured


def test_p1_is_explicit_and_changes_only_prompt_hash():
    graph, claim, _ = table_corpus()
    baseline = preliminary_table_request(claim, graph, tenant_id=TENANT, role_resolution=True)
    p1 = preliminary_table_request(claim, graph, tenant_id=TENANT, role_resolution=True, p1=True)
    assert (
        baseline["prompt_sha256"]
        == "19eeb93ef1d1067832f2dd96d5bdd3047d73cb6dbc200cd9b5b5296bbf848263"
    )
    assert baseline["prompt_sha256"] == canonical_hash(TABLE_ROLE_SYSTEM_PROMPT)
    assert p1["prompt_sha256"] == canonical_hash(P1_SYSTEM_PROMPT)
    assert p1["prompt_sha256"] != baseline["prompt_sha256"]
    assert {key for key in p1 if p1[key] != baseline[key]} == {"prompt_sha256"}
    assert "A stated completed certification is performance" in P1_SYSTEM_PROMPT
    assert "A goal deadline is never reporting_period" in P1_SYSTEM_PROMPT
    assert "Track is goal (future intention)" not in P1_SYSTEM_PROMPT
    with pytest.raises(DomainValidationError):
        preliminary_table_request(claim, graph, tenant_id=TENANT, p1=True)


def test_p1_transport_accepts_only_its_pinned_packet(tmp_path, monkeypatch):
    adapter, request, baseline, claim, graph = _configured(
        tmp_path,
        monkeypatch,
        profile=P1_MODEL_PROFILE,
        prompt=P1_SYSTEM_PROMPT,
        role_resolution=True,
    )
    assert adapter.TRANSPORT_VERSION == P1_TRANSPORT_VERSION
    with pytest.raises(ValueError, match="UPSTAGE_PRELIMINARY_PROMPT_INVALID"):
        adapter._wire_request(request)
    packet = preliminary_table_request(
        claim, graph, tenant_id=TENANT, role_resolution=True, p1=True
    )
    request.update(user_json=json.dumps(packet), packet_sha256=canonical_hash(packet))
    assert adapter._wire_request(request)[1] == json.dumps(
        packet, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    old_adapter, old_request, _, _, _ = _configured(
        tmp_path / "old",
        monkeypatch,
        profile=TABLE_ROLE_MODEL_PROFILE,
        prompt=TABLE_ROLE_SYSTEM_PROMPT,
        role_resolution=True,
    )
    old_request.update(user_json=json.dumps(packet), packet_sha256=canonical_hash(packet))
    with pytest.raises(ValueError, match="UPSTAGE_PRELIMINARY_PROMPT_INVALID"):
        old_adapter._wire_request(old_request)
    assert baseline["prompt_sha256"] != packet["prompt_sha256"]
