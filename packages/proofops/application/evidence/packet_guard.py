"""Bounded source data and server-owned model/tool capabilities.

This pure boundary grants no network access. Evidence tools have an empty URL
allowlist: model endpoints belong to the approved transport, never
to PDF text or tool arguments. A JSON data boundary is not a model-behavior
guarantee; composition must enforce these capability checks and the existing
preflight and output-schema guards for every invocation.
"""

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace

from proofops.application.evidence.span_citations import verify_source_ref
from proofops.application.ingest.graph_fusion import CanonicalDocumentGraph
from proofops.application.ports.models import ModelBinding
from proofops.domain.errors import DomainValidationError
from proofops.domain.provenance import canonical_hash
from proofops.domain.rulepacks import canonical_json
from proofops.domain.values import SourceRef, _require_uuid


@dataclass(frozen=True, slots=True)
class PacketMetadata:
    """Server-resolved run scope/config only; never deserialize from PDF or model output."""

    tenant_id: str
    run_id: str
    claim_id: str
    source_ref: SourceRef = field(repr=False)
    model_binding: ModelBinding
    allowed_models: tuple[ModelBinding, ...]
    system_prompt: str = field(repr=False)
    max_text_bytes: int
    max_packet_bytes: int
    allowed_tools: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("tenant_id", "run_id", "claim_id"):
            _require_uuid(name, getattr(self, name))
        if not isinstance(self.source_ref, SourceRef):
            raise DomainValidationError("source reference required")
        models = tuple(self.allowed_models)
        if any(
            not isinstance(model, ModelBinding)
            or not isinstance(model.binding_id, str)
            or not model.binding_id.strip()
            or model.role not in ("extractor", "tagger", "vision", "writer")
            or type(model.synthetic) is not bool
            for model in (*models, self.model_binding)
        ):
            raise DomainValidationError("invalid server model policy")
        if self.model_binding not in models:
            raise DomainValidationError("model denied by server allowlist")
        tools = tuple(self.allowed_tools)
        if any(tool != "read_source" for tool in tools):
            raise DomainValidationError("unsupported server tool capability")
        object.__setattr__(self, "allowed_models", models)
        object.__setattr__(self, "allowed_tools", tools)
        if not isinstance(self.system_prompt, str) or not self.system_prompt.strip():
            raise DomainValidationError("server system prompt required")
        for name in ("max_text_bytes", "max_packet_bytes"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise DomainValidationError("positive integer packet limit required")


@dataclass(frozen=True, slots=True)
class GuardedPacket:
    user_json: str = field(repr=False)
    packet_sha256: str
    _metadata: PacketMetadata = field(repr=False)

    @property
    def system_prompt(self) -> str:
        return self._metadata.system_prompt

    def authorize_model(self, binding: ModelBinding) -> ModelBinding:
        """Only the selected allowlisted binding, including role and synthetic flag."""
        if not isinstance(binding, ModelBinding) or binding != self._metadata.model_binding:
            raise DomainValidationError("model override denied")
        return self._metadata.model_binding

    def authorize_tool(self, name: str, arguments: Mapping[str, object]) -> SourceRef:
        """Resolve one already verified source; no arbitrary query or external read."""
        if (
            name != "read_source"
            or name not in self._metadata.allowed_tools
            or not isinstance(arguments, Mapping)
            or set(arguments) != {"source_id"}
            or arguments["source_id"] != self._metadata.source_ref.source_id
        ):
            raise DomainValidationError("tool or source scope denied")
        return self._metadata.source_ref

    def authorize_url(self, url: str) -> None:
        """P0 evidence tools have no URL-fetch/transmit capability, including redirects."""
        raise DomainValidationError("external URL capability denied")


def guard_untrusted_packet(
    text: str,
    server_metadata: PacketMetadata,
    *,
    original: CanonicalDocumentGraph,
    tenant_id: str,
) -> GuardedPacket:
    """Bind one evidence snippet to trusted scope without interpreting its instructions.

    Pass the authenticated tenant separately and load original under the authorized
    run. The caller's source must already pass the source-quality/citation gate.
    Reject over-budget input instead of silently truncating direct evidence; the
    packet builder owns larger multi-source windows and blocked_evidence handling.
    Replica/model/prompt/rule receipts stay with existing tagging/provenance code.
    """
    if not isinstance(server_metadata, PacketMetadata) or not isinstance(text, str):
        raise DomainValidationError("text and trusted server metadata required")
    if server_metadata.tenant_id != tenant_id:
        raise DomainValidationError("tenant scope denied")
    try:
        text_size = len(text.encode("utf-8"))
    except UnicodeError:
        raise DomainValidationError("invalid document text encoding") from None
    if text_size > server_metadata.max_text_bytes:
        raise DomainValidationError("document text byte limit exceeded")
    source = verify_source_ref(server_metadata.source_ref, original, tenant_id=tenant_id)
    if source.verification_state != "verified" or text != source.quote:
        raise DomainValidationError("original evidence text required")
    metadata = replace(server_metadata, source_ref=source)
    user = dict(
        schema_version="1",
        tenant_id=tenant_id,
        run_id=metadata.run_id,
        claim_id=metadata.claim_id,
        document_version_id=source.document_version_id,
        parse_manifest_id=source.parse_manifest_id,
        source_sha256=original.source_sha256,
        untrusted_document_data=dict(text=text, source_ref=asdict(source)),
    )
    user_json = canonical_json(user)
    try:
        size = len(user_json.encode("utf-8")) + len(metadata.system_prompt.encode("utf-8"))
    except UnicodeError:
        raise DomainValidationError("invalid server prompt encoding") from None
    if size > metadata.max_packet_bytes:
        raise DomainValidationError("rendered packet byte limit exceeded")
    # Hash authority separately from data serialization; no policy comes from the PDF.
    digest = canonical_hash(dict(user=user, server_metadata=asdict(metadata)))
    return GuardedPacket(user_json, digest, metadata)
