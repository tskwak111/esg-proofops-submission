import json

import pytest
from proofops.application.claims import ClaimScope, discover_atomic_claims
from proofops_agent.upstage_extraction import UpstageClaimExtractor

from tests.acceptance.test_claims import MANIFEST, TENANT, VERSION, graph_of
from tests.integration.test_upstage_extraction import FakeProbe


def test_sentence_index_content_bounds():
    text = "This is a regular sentence. Another one follows."
    # Without bounds
    spans = UpstageClaimExtractor._sentence_index(text, "doc-1", False)
    assert spans[0][0] == "doc-1:0"
    assert spans[0][1] == (0, 27)  # "This is a regular sentence." length is 27

    # With bounds
    spans_bounded = UpstageClaimExtractor._sentence_index(text, "doc-1", True)
    assert spans_bounded[0][0] == "doc-1:0:0:26"
    assert spans_bounded[0][1] == (0, 26)  # "This is a regular sentence" length is 26


def test_sentence_index_preserves_numeric_bounds():
    text = "Version 3.14. Next sentence."
    # Without bounds
    spans = UpstageClaimExtractor._sentence_index(text, "doc-1", False)
    assert spans[0][0] == "doc-1:0"
    assert spans[0][1] == (0, 13)  # "Version 3.14." length is 13

    # With bounds
    spans_bounded = UpstageClaimExtractor._sentence_index(text, "doc-1", True)
    assert spans_bounded[0][0] == "doc-1:0:0:13"
    assert spans_bounded[0][1] == (0, 13)  # "Version 3.14." should not be stripped


@pytest.mark.parametrize(
    "text",
    [
        "수치 3.14",
        "수치 12.",
        "수치 -3.14.",
        "비율 40%.",
        "진행합니다...",
        '"진행합니다."',
        "진행합니다。",
    ],
)
def test_content_bounds_preserves_numbers_ellipsis_and_closing_marks(text):
    legacy = UpstageClaimExtractor._sentence_index(text, "source")
    bounded = UpstageClaimExtractor._sentence_index(text, "source", True)
    assert [bounds for _, bounds in bounded] == [bounds for _, bounds in legacy]


def test_content_bounds_live_replay_keeps_words_and_unknown_stop(tmp_path):
    text = "당사는 2050년 Scope 1, 2 탄소중립 목표를 수립하고 감축 활동을 강화합니다."
    graph = graph_of(text)
    source_id = graph.blocks[0].source_id
    bounded_id = f"{source_id}:0:0:{len(text)-1}"
    probe = FakeProbe(json.dumps({"sentence_ids": [bounded_id]}))
    extractor = UpstageClaimExtractor(
        probe, tmp_path / "receipts", extraction_source_ids=True, extraction_content_bounds=True
    )
    scope = ClaimScope(TENANT, VERSION, MANIFEST)
    first = discover_atomic_claims(graph, scope, extractor=extractor)
    second = discover_atomic_claims(graph, scope, extractor=extractor)
    assert first == second
    assert len(probe.calls) == 1
    assert [claim.quote for claim in first.claims] == [text[:-1]]
    ref = first.claims[0].source_refs[0]
    assert (ref.char_start, ref.char_end) == (0, len(text) - 1)
    assert [(e.state, e.reason, e.source_ref.quote) for e in first.exclusions] == [
        ("unknown", "unprocessed_span", ".")
    ]
    sent = json.loads(probe.calls[0]["user_json"])["untrusted_document_data"]["source_sentences"]
    assert sent == [{"sentence_id": bounded_id, "text": text[:-1]}]
    with pytest.raises(ValueError, match="unknown or repeated sentence id"):
        extractor._resolve_sentence_ids({"sentence_ids": [f"{source_id}:0"]}, text, source_id)
