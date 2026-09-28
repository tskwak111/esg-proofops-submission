"""DART collection adapter package (Developer B).

Exports:
- DartClient: API client for OpenDART.
- DartResponse: Immutable response container preserving exact wire bytes and parsed data.
- Transport: Pluggable HTTP transport protocol.
- DartError, DartAuthError, DartRateLimitError, DartNotFoundError: Exception hierarchy.
- ArtifactStore: Content-addressed immutable artifact storage.
- safe_parse_xml: Defensive XML parser (XXE / Billion Laughs protection).
- create_artifact_entry: Artifact record builder for collection_manifest.schema.json.
- build_collection_manifest: Top-level collection manifest builder.
- normalize_financial_amount, normalize_entity_set, normalize_period: Data normalizers.
"""

from proofops.adapters.dart.artifacts import ArtifactStore, safe_parse_xml
from proofops.adapters.dart.client import (
    DartAuthError,
    DartClient,
    DartError,
    DartNotFoundError,
    DartRateLimitError,
    DartResponse,
    Transport,
)
from proofops.adapters.dart.normalization import (
    build_collection_manifest,
    create_artifact_entry,
    normalize_entity_set,
    normalize_financial_amount,
    normalize_period,
)

__all__ = [
    "DartClient",
    "DartResponse",
    "Transport",
    "DartError",
    "DartAuthError",
    "DartRateLimitError",
    "DartNotFoundError",
    "ArtifactStore",
    "safe_parse_xml",
    "create_artifact_entry",
    "build_collection_manifest",
    "normalize_financial_amount",
    "normalize_entity_set",
    "normalize_period",
]
