"""Regression: unsettled transport failures halt further billable calls.

Real discover_atomic_claims + real UpstageClaimExtractor with a caller-owned
fake probe. No self-mirroring run logic: assertions count actual probe calls.
"""

import pytest
from proofops.application.claims import ClaimScope, discover_atomic_claims
from proofops_agent.upstage_extraction import UpstageClaimExtractor

from tests.acceptance.test_claims import TENANT, graph_of


def _scope_for(graph):
    return ClaimScope(TENANT, graph.document_version_id, graph.parse_manifest_id, "full", ())


class _FakeProbe:
    def __init__(self, error):
        self.error = error
        self.calls = []

    def complete(self, system, user_json, *, request_id, max_tokens=1024, json_mode=False):
        self.calls.append(request_id)
        raise self.error


@pytest.mark.parametrize("error", [ValueError("UPSTAGE_HTTP_503"), OSError("timeout boom")])
def test_discover_halts_after_first_unsettled_transport_failure(error, tmp_path):
    graph = graph_of("첫 번째 문단이 여기에 있다.", "두 번째 문단이 여기에 있다.")
    assert len(graph.blocks) == 2
    probe = _FakeProbe(error)
    extractor = UpstageClaimExtractor(probe, tmp_path / "receipts")
    with pytest.raises(ValueError, match="UPSTAGE_HTTP_503|UPSTREAM_UNAVAILABLE"):
        discover_atomic_claims(graph, _scope_for(graph), extractor=extractor)
    assert len(probe.calls) == 1, "must not make second paid call after transport failure"
