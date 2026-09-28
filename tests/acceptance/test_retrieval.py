"""AT-011: synthetic local search corpus, real retrieval/graph/rule contracts."""

import json
from dataclasses import FrozenInstanceError, asdict, replace
from hashlib import sha256

import pytest
from proofops.application.claims import Claim, ExtractionProfile, ExtractionReceipt
from proofops.application.ingest.graph_fusion import (
    CandidateBatch,
    CandidateBlock,
    CandidateEdge,
    fuse_candidates,
)
from proofops.domain.documents import NativeSource, PageGeometry
from proofops.domain.errors import DomainValidationError

from tests.acceptance.test_citations import MANIFEST, OTHER, RUN, TENANT, VERSION
from tests.acceptance.test_rules import pack


def corpus(table=False, count=3):
    texts = ["We reduce emissions", "2030 emissions target 40%", "emissions methodology scope"]
    texts += [f"emissions methodology {i}" for i in range(count - 3)]
    candidates = tuple(
        CandidateBlock(
            "table_row" if table and i < 2 else "paragraph",
            NativeSource(
                VERSION,
                MANIFEST,
                RUN,
                str(i),
                1 if i == 0 else 90 + i,
                None,
                (10, 10, 300, 40),
                "pdf_bottom_left_points",
                text,
                0,
                len(text),
            ),
            PageGeometry(600, 800, 0, (0, 0, 600, 800)),
        )
        for i, text in enumerate(texts)
    )
    if table:
        candidates += (
            replace(
                candidates[0],
                kind="table",
                source=replace(
                    candidates[0].source,
                    source_native_id="table",
                    raw_text="emissions year unit %",
                    char_end=21,
                ),
            ),
        )
    edges = (
        (CandidateEdge("0", "table", "table_parent"), CandidateEdge("1", "table", "table_parent"))
        if table
        else ()
    )
    batch = CandidateBatch(
        TENANT,
        VERSION,
        MANIFEST,
        "a" * 64,
        RUN,
        "synthetic",
        "1",
        "synthetic",
        "b" * 64,
        candidates,
        edges=edges,
        synthetic=True,
    )
    graph = fuse_candidates((batch,), tenant_id=TENANT)
    graph = replace(graph, blocks=tuple(replace(b, quality="verified") for b in graph.blocks))
    block = next(b for b in graph.blocks if b.raw_text == texts[0])
    profile = ExtractionProfile("c" * 64, "d" * 64, "e" * 64, True)
    claim = Claim(
        OTHER,
        TENANT,
        VERSION,
        MANIFEST,
        graph.source_sha256,
        block.raw_text,
        (block.source_ref(),),
        "verified",
        (),
        ExtractionReceipt(block.source_id, "f" * 64, None, None, profile, "ok"),
    )
    return graph, claim


class SyntheticSearch:
    """Explicit test-only lexical retrieval; no fabricated production/model output."""

    synthetic = True

    def __init__(self, graph):
        self.graph = graph
        self.calls = []

    def search(self, scope, query, *, vector=None):
        from proofops.application.evidence.retrieval import SearchHit, SearchResult

        self.calls.append("vector" if vector else "lexical")
        # Synthetic vector space is one binary dimension: 'emissions' occurrence.
        ranked = sorted(
            self.graph.blocks,
            key=lambda b: (
                -sum(word.lower() in b.raw_text.lower() for word in query.split()),
                b.source_id,
            ),
        )
        return SearchResult(
            tuple(
                SearchHit(scope, b.source_id, sha256(b.raw_text.encode()).hexdigest())
                for b in ranked[:20]
            )
        )


def retrieve(graph, claim, search=None, **kwargs):
    from proofops.application.evidence.retrieval import retrieve_evidence

    return retrieve_evidence(
        claim,
        graph,
        search or SyntheticSearch(graph),
        tenant_id=kwargs.pop("tenant_id", TENANT),
        run_id=RUN,
        index_generation="generation-1",
        rulepack=pack(),
        document_context={"company": "synthetic company", "period": "2025"},
        query_vector=(1.0,),
        token_counter=lambda text: len(text.encode()),
        **kwargs,
    )


def test_far_year_is_candidate_only_for_global_elements_and_claim_is_direct():
    graph, claim = corpus()
    data = retrieve(graph, claim).to_dict()
    local = next(c for c in data["evidence_candidates"] if c["source_scope"] == "local_claim")
    far = next(c for c in data["evidence_candidates"] if "2030" in c["source_refs"][0]["quote"])
    assert "G1" in local["allowed_elements"]
    assert far["source_scope"] == "global_bound"
    assert "G1" not in far["allowed_elements"] and "P1" not in far["allowed_elements"]
    assert "G4" in far["allowed_elements"]
    assert all(b["state"] == "undetermined" for b in data["candidate_bindings"])
    assert data["synthetic"] is True
    assert data["search_coverage"]["not_found_state"] == "unknown"


@pytest.mark.parametrize("field", ["tenant_id", "document_version_id", "parse_manifest_id"])
def test_cross_identity_is_denied_before_search(field):
    graph, claim = corpus()
    search = SyntheticSearch(graph)
    with pytest.raises(DomainValidationError):
        retrieve(graph, replace(claim, **{field: RUN}), search)
    assert search.calls == []


def test_wrong_hash_and_forged_claim_quote_fail_closed():
    graph, claim = corpus()
    for bad in (
        replace(claim, source_sha256="0" * 64),
        replace(claim, quote="2030 40%"),
        replace(claim, source_refs=()),
    ):
        with pytest.raises(DomainValidationError):
            retrieve(graph, bad)


def test_same_page_does_not_expand_atomic_span():
    graph, claim = corpus()
    text = claim.quote
    short = replace(claim.source_refs[0], quote=text[:9], char_end=9)
    claim = replace(claim, quote=short.quote, source_refs=(short,))
    data = retrieve(graph, claim).to_dict()
    local = next(c for c in data["evidence_candidates"] if c["source_scope"] == "local_claim")
    assert local["source_refs"][0]["quote"] == "We reduce"


def test_same_table_keeps_parent_header_and_year_but_binding_is_unconfirmed():
    graph, claim = corpus(table=True)
    data = retrieve(graph, claim).to_dict()
    candidate = next(
        c for c in data["evidence_candidates"] if c["source_refs"][0]["quote"].startswith("2030")
    )
    assert candidate["source_scope"] == "same_table"
    assert "G1" in candidate["allowed_elements"]
    assert any("unit %" in ref["quote"] for ref in candidate["source_refs"])
    assert all(b["state"] == "undetermined" for b in data["candidate_bindings"])


@pytest.mark.parametrize("quality", ["conflicted", "unreadable", "unlocated", "unverified"])
def test_unresolved_sources_remain_visible_in_coverage_not_absent(quality):
    graph, claim = corpus()
    far = next(b for b in graph.blocks if "2030" in b.raw_text)
    graph = replace(
        graph, blocks=tuple(replace(b, quality=quality) if b == far else b for b in graph.blocks)
    )
    data = retrieve(graph, claim).to_dict()
    assert far.source_id in data["search_coverage"]["unprocessed_source_ids"]
    assert data["search_coverage"]["source_quality"][far.source_id] == quality
    assert data["search_coverage"]["not_found_state"] == "unknown"
    assert all(c["source_id"] != far.source_id for c in data["evidence_candidates"])


def test_packet_is_deterministic_detached_and_changes_with_index_or_budget():
    graph, claim = corpus(count=18)
    packet = retrieve(graph, claim)
    data = packet.to_dict()
    original_hash = packet.packet_sha256
    assert original_hash == retrieve(graph, claim).packet_sha256
    data["document_context"]["company"] = "changed"
    assert packet.to_dict()["document_context"]["company"] == "synthetic company"
    with pytest.raises(FrozenInstanceError):
        packet.packet_sha256 = "0" * 64
    assert len(data["evidence_candidates"]) <= 12
    assert data["search_coverage"]["omitted_source_ids"]
    assert retrieve(graph, claim, max_tokens=1).to_dict()["status"] == "blocked_evidence"
    assert packet.packet_sha256 == original_hash
    assert retrieve(graph, claim, max_tokens=1).packet_sha256 != original_hash
    assert data["extraction_provenance"] == json.loads(json.dumps(asdict(claim.receipt)))


def test_opensearch_forces_filters_for_both_queries_and_rejects_bad_hits():
    from proofops.adapters.aws.opensearch import OpenSearchEvidenceSearch
    from proofops.application.evidence.retrieval import SearchScope

    scope = SearchScope(TENANT, VERSION, MANIFEST, "generation-1")
    # Query generation is a public adapter boundary, independent of any SDK mock.
    lexical = OpenSearchEvidenceSearch.query_body(scope, "emissions")
    vector = OpenSearchEvidenceSearch.query_body(scope, "emissions", vector=(1.0,))
    filters = lexical["query"]["bool"]["filter"]
    assert filters == [{"term": {k: v}} for k, v in asdict(scope).items()]
    assert vector["query"]["knn"]["embedding"]["filter"]["bool"]["filter"] == filters
    assert lexical["size"] == vector["size"] == 20
    with pytest.raises(DomainValidationError):
        OpenSearchEvidenceSearch.query_body(scope, "x", vector=(float("nan"),))


def test_search_hits_cannot_smuggle_foreign_tenant_document_generation_or_hash():
    from proofops.application.evidence.retrieval import SearchHit, SearchResult, SearchScope

    graph, claim = corpus()
    far = next(b for b in graph.blocks if "2030" in b.raw_text)
    scope = SearchScope(TENANT, VERSION, MANIFEST, "generation-1")

    class ContaminatedSearch(SyntheticSearch):
        def search(self, scope, query, *, vector=None):
            return SearchResult((bad_hit,))

    for bad_hit in [
        SearchHit(
            replace(scope, **{field: OTHER}),
            far.source_id,
            sha256(far.raw_text.encode()).hexdigest(),
        )
        for field in ("tenant_id", "document_version_id", "parse_manifest_id", "index_generation")
    ] + [SearchHit(scope, far.source_id, "0" * 64)]:
        data = retrieve(graph, claim, ContaminatedSearch(graph)).to_dict()
        assert all(c["source_id"] != far.source_id for c in data["evidence_candidates"])
        assert data["search_coverage"]["rejected_hit_count"] == 2


def test_missing_vector_search_records_not_run_and_gri_mismatch_does_not_stop_search():
    from proofops.application.evidence.retrieval import retrieve_evidence
    from proofops.application.ingest.gri import IndexEntry

    graph, claim = corpus()
    search = SyntheticSearch(graph)
    entry = IndexEntry(
        TENANT,
        VERSION,
        MANIFEST,
        "305-5",
        ("999",),
        (999,),
        "synthetic broken route",
        "resolved",
        claim.source_refs[0],
    )
    data = retrieve_evidence(
        claim,
        graph,
        search,
        tenant_id=TENANT,
        run_id=RUN,
        index_generation="generation-1",
        rulepack=pack(),
        document_context={},
        token_counter=len,
        gri_entries=(entry,),
        indicator_codes=("305-5",),
    ).to_dict()
    assert search.calls == ["lexical"]
    assert data["search_coverage"]["routes"] == ["gri", "section_table", "lexical", "vector"]
    assert data["search_coverage"]["vector_status"] == "not_run"
    assert data["search_coverage"]["gri_unresolved"] == ["305-5"]
    assert len(data["evidence_candidates"]) == 3


def test_unverified_table_header_blocks_direct_packet_and_preserves_parse_issues():
    from proofops.application.ingest.graph_fusion import QualityIssue

    graph, claim = corpus(table=True)
    graph = replace(
        graph,
        blocks=tuple(
            replace(b, quality="conflicted") if b.kind == "table" else b for b in graph.blocks
        ),
        issues=(
            QualityIssue(
                "synthetic-issue",
                "missing_page",
                200,
                (),
                "unreadable",
                "synthetic unreadable region",
            ),
        ),
    )
    data = retrieve(graph, claim).to_dict()
    assert data["status"] == "blocked_evidence"
    assert data["search_coverage"]["quality_issues"][0]["state"] == "unreadable"


def test_adapter_executes_real_http_search_and_retrieval_rehydrates_original():
    """Local synthetic HTTP search service; production adapter and actual socket I/O."""
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from threading import Thread
    from urllib.request import Request, urlopen

    from proofops.adapters.aws.opensearch import OpenSearchEvidenceSearch

    graph, claim = corpus()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            query = body["query"]
            filters = (
                query["bool"]["filter"]
                if "bool" in query
                else query["knn"]["embedding"]["filter"]["bool"]["filter"]
            )
            identity = {key: value for clause in filters for key, value in clause["term"].items()}
            # Real synthetic lexical/one-dimensional vector corpus search.
            matches = [b for b in graph.blocks if "emissions" in b.raw_text]
            response = {
                "timed_out": False,
                "_shards": {"failed": 0},
                "hits": {
                    "hits": [
                        {
                            "_source": identity
                            | {
                                "source_id": b.source_id,
                                "raw_text_sha256": sha256(b.raw_text.encode()).hexdigest(),
                            }
                        }
                        for b in matches[: body["size"]]
                    ]
                },
            }
            encoded = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    class HTTPClient:
        def search(self, *, index, body):
            request = Request(
                f"http://127.0.0.1:{server.server_port}/{index}/_search",
                json.dumps(body).encode(),
                {"Content-Type": "application/json"},
            )
            with urlopen(request, timeout=5) as response:
                return json.load(response)

    try:
        adapter = OpenSearchEvidenceSearch(HTTPClient(), index="synthetic-index")
        packet = retrieve(graph, claim, adapter).to_dict()
        assert len(requests) == 2
        assert len(packet["evidence_candidates"]) == 3
        assert packet["search_coverage"]["lexical_status"] == "bounded"
        assert packet["search_coverage"]["vector_status"] == "bounded"
        assert packet["synthetic"] is True  # Propagated from the parser/claim, not AWS.
        assert all(
            ref["verification_state"] == "verified"
            for c in packet["evidence_candidates"]
            for ref in c["source_refs"]
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_provider_failure_leaves_partial_unknown_with_local_evidence():
    graph, claim = corpus()

    class FailedSearch(SyntheticSearch):
        def search(self, *args, **kwargs):
            raise RuntimeError("private document text must never appear in error metadata")

    data = retrieve(graph, claim, FailedSearch(graph)).to_dict()
    assert data["search_coverage"]["lexical_status"] == "failed"
    assert data["search_coverage"]["vector_status"] == "failed"
    assert data["search_coverage"]["not_found_state"] == "unknown"
    assert len(data["evidence_candidates"]) == 1
    assert "private document text" not in json.dumps(data)


def test_opensearch_rejects_wildcard_index():
    from proofops.adapters.aws.opensearch import OpenSearchEvidenceSearch

    with pytest.raises(DomainValidationError):
        OpenSearchEvidenceSearch(None, index="tenant-?")


def test_foreign_or_unknown_claim_reference_is_denied_before_search():
    graph, claim = corpus()
    for fields in (
        {"source_id": RUN},
        {"document_version_id": OTHER},
        {"parse_manifest_id": OTHER},
    ):
        search = SyntheticSearch(graph)
        bad = replace(claim, source_refs=(replace(claim.source_refs[0], **fields),))
        with pytest.raises(DomainValidationError):
            retrieve(graph, bad, search)
        assert search.calls == []


def test_direct_orphan_cell_cannot_make_a_ready_packet():
    graph, claim = corpus()
    graph = replace(
        graph,
        blocks=tuple(
            replace(b, kind="table_cell") if b.source_id == claim.source_refs[0].source_id else b
            for b in graph.blocks
        ),
    )
    data = retrieve(graph, claim).to_dict()
    assert data["status"] == "blocked_evidence"
    assert claim.source_refs[0].source_id in data["search_coverage"]["unprocessed_source_ids"]


def orphan_corpus(page=1):
    graph, claim = corpus(table=True)
    batch = graph.candidates[0]
    text = "Domestic operations only"
    note = replace(
        batch.blocks[0],
        kind="footnote",
        source=replace(
            batch.blocks[0].source,
            source_native_id="orphan-note",
            physical_page=page,
            raw_text=text,
            char_end=len(text),
        ),
    )
    graph = fuse_candidates((replace(batch, blocks=(*batch.blocks, note)),), tenant_id=TENANT)
    graph = replace(graph, blocks=tuple(replace(b, quality="verified") for b in graph.blocks))
    return graph, claim


@pytest.mark.parametrize("page", [1, 2])
def test_unassigned_note_cannot_enter_as_table_context_or_standalone_evidence(page):
    graph, claim = orphan_corpus(page)
    data = retrieve(graph, claim).to_dict()
    note = next(b for b in graph.blocks if b.kind == "footnote")
    assert data["status"] == ("blocked_evidence" if page == 1 else "candidate")
    assert note.source_id in data["search_coverage"]["unprocessed_source_ids"]
    assert all(c["source_id"] != note.source_id for c in data["evidence_candidates"])
    if page == 1:
        assert all(
            not any(r["page_num"] == 1 for r in c["source_refs"])
            for c in data["evidence_candidates"]
        )


def test_open_table_review_issue_blocks_retrieval_even_if_only_parent_is_named():
    from proofops.application.ingest.graph_fusion import QualityIssue

    graph, claim = corpus(table=True)
    table_id = next(b.source_id for b in graph.blocks if b.kind == "table")
    graph = replace(
        graph,
        issues=(QualityIssue("review", "table_note_review", 1, (table_id,), "open", "Unresolved"),),
    )
    assert retrieve(graph, claim).to_dict()["status"] == "blocked_evidence"
