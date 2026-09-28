"""Typed parser boundary; adapters return candidates with immutable provenance."""

from typing import Protocol

from proofops.application.ingest.graph_fusion import (
    CanonicalDocumentGraph,
    ParserProfile,
    SourceArtifact,
)


class ParserPort(Protocol):
    def parse(
        self, source: SourceArtifact, profile: ParserProfile, *, tenant_id: str
    ) -> CanonicalDocumentGraph: ...
