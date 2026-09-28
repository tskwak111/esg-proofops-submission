"""OpenSearch query/response adapter; composition supplies an authorized SDK client.

Implements EvidenceSearchPort. No client construction, AWS discovery, embedding
calls, index mutation or synthetic fallback. Index metadata fields must be exact
keyword mappings and embedding must use a filter-capable k-NN index.
"""

from __future__ import annotations

import math
from dataclasses import asdict
from typing import Any, Protocol

from proofops.application.evidence.retrieval import SearchHit, SearchResult, SearchScope
from proofops.domain.errors import DomainValidationError


class OpenSearchClient(Protocol):
    def search(self, *, index: str, body: dict[str, Any]) -> dict[str, Any]: ...


class OpenSearchEvidenceSearch:
    synthetic = False

    def __init__(self, client: OpenSearchClient, *, index: str):
        if not isinstance(index, str) or not index or any(c in index for c in "/*?, #\\:"):
            raise DomainValidationError("one explicit search index required")
        self.client = client
        self.index = index

    @staticmethod
    def query_body(
        scope: SearchScope, query: str, *, vector: tuple[float, ...] | None = None
    ) -> dict[str, Any]:
        if not isinstance(scope, SearchScope) or not isinstance(query, str) or not query.strip():
            raise DomainValidationError("search scope and query required")
        filters = [{"term": {key: value}} for key, value in asdict(scope).items()]
        clause: dict[str, Any]
        if vector is None:
            clause = {"bool": {"filter": filters, "must": [{"match": {"text": query}}]}}
        else:
            if not vector or any(
                type(v) not in (int, float) or not math.isfinite(v) for v in vector
            ):
                raise DomainValidationError("finite nonempty query vector required")
            clause = {
                "knn": {
                    "embedding": {
                        "vector": list(vector),
                        "k": 20,
                        "filter": {"bool": {"filter": filters}},
                    }
                }
            }
        return {
            "size": 20,
            "query": clause,
            "_source": [*asdict(scope), "source_id", "raw_text_sha256"],
        }

    def search(
        self, scope: SearchScope, query: str, *, vector: tuple[float, ...] | None = None
    ) -> SearchResult:
        body = self.query_body(scope, query, vector=vector)
        try:
            response = self.client.search(index=self.index, body=body)
            hits = []
            for hit in response["hits"]["hits"][:20]:
                source = hit["_source"]
                identity = SearchScope(**{key: source[key] for key in asdict(scope)})
                if identity != scope:
                    raise DomainValidationError("search identity mismatch")
                hits.append(SearchHit(identity, source["source_id"], source["raw_text_sha256"]))
            partial = response.get("timed_out", False) or response.get("_shards", {}).get(
                "failed", 0
            )
            return SearchResult(tuple(hits), "partial" if partial else "bounded")
        except Exception:
            raise DomainValidationError("SEARCH_UNAVAILABLE: invalid or failed search") from None
