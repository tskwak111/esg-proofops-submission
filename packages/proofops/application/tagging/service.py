"""Three source-guarded invocations over one immutable evidence packet.

Composition supplies an authorized transport, scoped cache, durable usage store,
model tokenizer and clock. No credentials, model IDs, approvals or prices are
invented. Raw recovery is restricted to the exact request, never another vote.
"""

import json
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from typing import Any
from unicodedata import normalize
from uuid import UUID, uuid5

from proofops.adapters.cache.aws import (
    CacheNamespace,
    CacheRequest,
    ImmutableResponseCache,
    cache_request,
)
from proofops.application.budget import (
    BudgetCall,
    BudgetExceeded,
    PricingSnapshot,
    TokenUsage,
    UsageRepository,
    count,
    record_usage,
    reserve_budget,
)
from proofops.application.evidence.binding import ClaimContext, accept_binding, relation_tags_for
from proofops.application.evidence.packet_guard import PacketMetadata, guard_untrusted_packet
from proofops.application.evidence.retrieval import (
    EvidencePacket,
    evidence_issue_ids,
    freeze_track_packet,
)
from proofops.application.evidence.span_citations import verify_source_ref
from proofops.application.ingest.graph_fusion import CanonicalDocumentGraph
from proofops.application.ports.models import ModelBinding
from proofops.application.tagging.tracks import TrackCandidate
from proofops.domain.errors import DomainValidationError
from proofops.domain.numeric import unassigned_note_ids
from proofops.domain.provenance import canonical_hash
from proofops.domain.rulepacks import RulePackSnapshot, canonical_json
from proofops.domain.values import LlmTags, SourceRef, _require_uuid, llm_tags_from_dict

_SAFE_INPUT_COUNT_CODES = frozenset(
    {
        "PROBE_REQUEST_TOO_LARGE",
        "PRICE_RECHECK_REQUIRED",
        "INVALID_PROBE_REQUEST",
        "TAGGING_RECOVERY_ALLOWANCE_EXHAUSTED",
        "UPSTAGE_TAGGING_REQUEST_INVALID",
        "UPSTAGE_TAGGING_CLASSIFICATION_REQUIRED",
        "UPSTAGE_TAGGING_CLASSIFICATION_INVALID",
        "UPSTAGE_TAGGING_PACKET_MISMATCH",
        "UPSTAGE_TAGGING_COVERAGE_INVALID",
        "UPSTAGE_TAGGING_ELEMENTS_REQUIRED",
        "LIVE_TAGGING_SETTINGS_MISMATCH",
        "LIVE_TAGGING_PACKET_NOT_AUTHORIZED",
        "LIVE_TAGGING_POLICY_MISMATCH",
        "LIVE_TAGGING_POLICY_NOT_AUTHORIZED",
        "LIVE_TAGGING_AUTHORIZATION_REVOKED",
        "LIVE_TAGGING_AUTHORIZATION_CHANGED",
    }
)


@dataclass(frozen=True, slots=True)
class TaggingSettings:
    binding: ModelBinding
    model_id: str
    model_profile: str
    region: str
    system_prompt: str
    schema_json: str
    max_tokens: int = 4096
    temperature: float = 0.0
    extraction_epoch: int = 1
    max_response_bytes: int = 1_048_576

    def __post_init__(self):
        if not isinstance(self.binding, ModelBinding) or self.binding.role != "tagger":
            raise DomainValidationError("tagger binding required")
        if type(self.binding.synthetic) is not bool:
            raise DomainValidationError("synthetic marker required")
        for value in (
            self.binding.binding_id,
            self.model_id,
            self.model_profile,
            self.region,
            self.system_prompt,
        ):
            if not isinstance(value, str) or not value.strip():
                raise DomainValidationError("explicit model and prompt settings required")
        if not isinstance(json.loads(self.schema_json), dict):
            raise DomainValidationError("JSON output schema required")
        if type(self.max_response_bytes) is not int or self.max_response_bytes <= 0:
            raise DomainValidationError("positive response byte limit required")

    @property
    def rendered_system(self) -> str:
        return self.system_prompt + "\nOutput JSON schema:\n" + self.schema_json

    def system_for_track(self, track: str, safe_harbor_category: str | None) -> str:
        return (
            self.rendered_system
            + "\nValidated classification; tag only its elements: "
            + canonical_json(dict(track=track, safe_harbor_category=safe_harbor_category))
        )

    @property
    def model_sha256(self) -> str:
        return canonical_hash(
            dict(
                binding=asdict(self.binding),
                model_id=self.model_id,
                model_profile=self.model_profile,
                region=self.region,
            )
        )

    @property
    def prompt_sha256(self) -> str:
        return canonical_hash(self.rendered_system)


@dataclass(frozen=True, slots=True)
class RawTagResponse:
    raw_response_json: str | None
    usage: TokenUsage
    synthetic: bool
    provider_response_json: str | None = None

    def __post_init__(self):
        if type(self.synthetic) is not bool or not isinstance(self.usage, TokenUsage):
            raise DomainValidationError("typed usage and synthetic provenance required")
        if any(
            value is not None and not isinstance(value, str)
            for value in (self.raw_response_json, self.provider_response_json)
        ):
            raise DomainValidationError("raw responses must be strings or null")


@dataclass(frozen=True, slots=True)
class TagRun:
    request: CacheRequest
    tenant_id: str
    run_id: str
    claim_id: str
    packet_sha256: str
    graph_sha256: str
    rule_sha256: str
    model_sha256: str
    prompt_sha256: str
    raw_response_json: str | None
    usage: TokenUsage | None
    guarded: LlmTags | None
    status: str
    errors: tuple[str, ...]
    source_scopes: tuple[tuple[str, str], ...]
    binding_hashes: tuple[tuple[str, str], ...]
    synthetic: bool
    product_variant: bool = False
    recovered: bool = False
    provider_response_json: str | None = None
    diagnostic_detail: str | None = None

    @property
    def replicate_id(self) -> int:
        return self.request.replicate_id

    @property
    def semantic_hash(self) -> str:
        data = asdict(self)
        data.pop("recovered")
        if data["diagnostic_detail"] is None:
            data.pop("diagnostic_detail")
        return canonical_hash(data)


def tag_replicates(
    packet: EvidencePacket,
    *,
    context: ClaimContext,
    track: TrackCandidate,
    original: CanonicalDocumentGraph,
    relation_tags: Mapping[str, Mapping[str, SourceRef | None]],
    rulepack: RulePackSnapshot,
    settings: TaggingSettings,
    cache: ImmutableResponseCache,
    usage_store: UsageRepository,
    invoke: Callable[[dict[str, Any]], RawTagResponse],
    tenant_id: str,
    ensemble_id: str,
    consent_profile: str,
    token_counter: Callable[[str], int],
    now: Callable[[], int],
    pricing: PricingSnapshot | None = None,
    count_input_tokens: Callable[[dict[str, Any]], int] | None = None,
) -> tuple[TagRun, ...]:
    """Execute or recover replicas 1/2/3, preserving failures and budget stops.

    Call freeze_track_packet once before this function and pass that same
    selected packet to form_consensus and immutable storage.

    No implicit retry or model fallback is performed. An indeterminate in-flight
    attempt stays pending for recovery; it is never sent twice. Absence and N/A
    require upstream approval/coverage unavailable in a bounded retrieval packet,
    so those model candidates remain unknown here. Computed numeric checks and
    covered-assurance assertions likewise require their dedicated services.
    """
    _require_uuid("ensemble_id", ensemble_id)
    _require_uuid("tenant_id", tenant_id)
    if not settings.binding.synthetic and not callable(count_input_tokens):
        raise DomainValidationError("TAGGING_INPUT_COUNTER_REQUIRED")
    if not isinstance(packet, EvidencePacket) or not isinstance(context, ClaimContext):
        raise DomainValidationError("immutable packet and claim context required")
    data = packet.to_dict()
    claim = context.claim
    if not isinstance(track, TrackCandidate) or track.claim != claim:
        raise DomainValidationError("track/claim identity mismatch")
    if freeze_track_packet(packet, track=track, rulepack=rulepack) != packet:
        raise DomainValidationError("freeze the complete track packet before tagging")
    if claim.source_quality != "verified" or (
        claim.document_version_id,
        claim.parse_manifest_id,
        claim.source_sha256,
    ) != (original.document_version_id, original.parse_manifest_id, original.source_sha256):
        raise DomainValidationError("claim/original source identity or quality mismatch")
    rendered_system = settings.system_for_track(track.track, track.safe_harbor_category)
    prompt_sha256 = canonical_hash(rendered_system)
    expected = dict(
        tenant_id=tenant_id,
        document_version_id=claim.document_version_id,
        parse_manifest_id=claim.parse_manifest_id,
        claim_id=claim.claim_id,
        atomic_quote=claim.quote,
        source_sha256=claim.source_sha256,
        graph_sha256=canonical_hash(asdict(original)),
        rule_sha256=rulepack.sha256,
    )
    if (original.tenant_id, claim.tenant_id, rulepack.tenant_id) != (tenant_id,) * 3 or any(
        data.get(key) != value for key, value in expected.items()
    ):
        raise DomainValidationError("packet/claim/source identity mismatch")
    if data.get("status") != "candidate" or data.get("content_trust") != "untrusted_document_data":
        raise DomainValidationError("blocked or unguarded evidence packet")
    if data.get("claim_source_refs") != json.loads(
        canonical_json(
            [asdict(verify_source_ref(s, original, tenant_id=tenant_id)) for s in claim.source_refs]
        )
    ):
        raise DomainValidationError("atomic source span mismatch")
    if any(unassigned_note_ids(original, ref.source_id) for ref in claim.source_refs):
        raise DomainValidationError("unassigned footnote requires review")
    _require_uuid("run_id", data["run_id"])
    definitions = {e["id"]: e for e in rulepack.file_content("rubric/elements.yaml")["elements"]}
    allowed = tuple(data["allowed_elements"])
    if not allowed or len(set(allowed)) != len(allowed) or set(allowed) - definitions.keys():
        raise DomainValidationError("invalid allowed element catalog")
    document_context = data.get("document_context")
    if (
        not isinstance(document_context, dict)
        or set(document_context) - {"company", "period", "industry", "boundary"}
        or any(
            value is not None and not isinstance(value, str) for value in document_context.values()
        )
    ):
        raise DomainValidationError("invalid document context; gold/grades forbidden")
    source_candidates: dict[str, dict] = {}
    approved_refs: list[SourceRef] = []
    for candidate in data["evidence_candidates"]:
        if set(candidate) != {"source_id", "source_scope", "allowed_elements", "source_refs"}:
            raise DomainValidationError("invalid evidence candidate fields")
        if candidate["source_id"] in source_candidates:
            raise DomainValidationError("duplicate evidence candidate")
        source_candidates[candidate["source_id"]] = candidate
        for value in candidate["source_refs"]:
            source = SourceRef(**value)
            if unassigned_note_ids(original, source.source_id):
                raise DomainValidationError("unassigned footnote requires review")
            metadata = PacketMetadata(
                tenant_id,
                data["run_id"],
                claim.claim_id,
                source,
                settings.binding,
                (settings.binding,),
                rendered_system,
                1_048_576,
                2_097_152,
            )
            guarded = guard_untrusted_packet(
                source.quote, metadata, original=original, tenant_id=tenant_id
            )
            guarded.authorize_model(settings.binding)
            approved_refs.append(source)
    if not approved_refs:
        raise DomainValidationError("verified packet evidence required")
    source_ids = {ref.source_id for ref in approved_refs}
    source_ids.update(ref.source_id for ref in claim.source_refs)
    if evidence_issue_ids(original, source_ids, [*approved_refs, *claim.source_refs]):
        raise DomainValidationError("open source issue requires review")
    # Explicit data allowlist: no prior votes, gold, tools or model-selected policy.
    user_data = {
        key: data[key]
        for key in (
            "atomic_quote",
            "claim_source_refs",
            "document_context",
            "allowed_elements",
            "evidence_candidates",
            "search_coverage",
        )
    }
    # Only typed coverage identifiers reach the model, never arbitrary metadata.
    user_data["search_coverage"] = {"not_found_state": "unknown"}
    graph_ids = {block.source_id for block in original.blocks}
    for key in ("omitted_source_ids", "unprocessed_source_ids"):
        values = data["search_coverage"].get(key, [])
        if not isinstance(values, list) or any(
            not isinstance(sid, str) or sid not in graph_ids for sid in values
        ):
            raise DomainValidationError("invalid search coverage identifiers")
        user_data["search_coverage"][key] = list(values)
    namespace = CacheNamespace(tenant_id, consent_profile, claim.document_version_id, "tagger")
    synthetic = settings.binding.synthetic or bool(data["synthetic"])
    runs = []
    for replica in (1, 2, 3):
        user_json = canonical_json(
            dict(
                packet_sha256=packet.packet_sha256,
                replicate_id=replica,
                claim_id=claim.claim_id,
                untrusted_document_data=user_data,
            )
        )
        signature_options = dict(
            namespace=namespace,
            temperature=settings.temperature,
            model_id=settings.model_id,
            model_profile=settings.model_sha256,
            prompt_sha256=prompt_sha256,
            schema_sha256=canonical_hash(settings.schema_json),
            packet_sha256=packet.packet_sha256,
            tools=[],
            max_tokens=settings.max_tokens,
            replicate_id=replica,
            extraction_epoch=settings.extraction_epoch,
        )
        provisional = cache_request(request_id=ensemble_id, **signature_options)
        request = replace(
            provisional, request_id=str(uuid5(UUID(ensemble_id), provisional.request_signature))
        )
        identity = dict(
            request=asdict(request),
            tenant_id=tenant_id,
            run_id=data["run_id"],
            claim_id=claim.claim_id,
            packet_sha256=packet.packet_sha256,
            graph_sha256=data["graph_sha256"],
            rule_sha256=rulepack.sha256,
            model_sha256=settings.model_sha256,
            prompt_sha256=prompt_sha256,
        )
        run = TagRun(
            request,
            tenant_id,
            data["run_id"],
            claim.claim_id,
            packet.packet_sha256,
            data["graph_sha256"],
            rulepack.sha256,
            settings.model_sha256,
            prompt_sha256,
            None,
            None,
            None,
            "pending",
            (),
            (),
            (),
            synthetic,
        )
        run = replace(run, product_variant=context.dimensions.get("product") is not None)
        try:
            cached = cache.get_raw(request, recovery_request_id=request.request_id)
            if cached is not None:
                envelope = json.loads(cached)
                if envelope["identity"] != json.loads(canonical_json(identity)):
                    raise DomainValidationError("raw cache identity mismatch")
                response = RawTagResponse(
                    envelope["raw_response_json"],
                    TokenUsage(**envelope["usage"]),
                    envelope["synthetic"],
                    envelope.get("provider_response_json"),
                )
                run = replace(run, recovered=True)
            else:
                call = BudgetCall(
                    tenant_id,
                    data["run_id"],
                    claim.document_version_id,
                    request.request_id,
                    1,
                    "tagger",
                    settings.model_id,
                    settings.region,
                    settings.model_sha256,
                    request.request_signature,
                    replica,
                )
                model_request = dict(
                    tenant_id=tenant_id,
                    claim_id=claim.claim_id,
                    packet_sha256=packet.packet_sha256,
                    replicate_id=replica,
                    request_id=request.request_id,
                    request_signature=request.request_signature,
                    binding=asdict(settings.binding),
                    model_id=settings.model_id,
                    model_profile=settings.model_profile,
                    region=settings.region,
                    system_prompt=rendered_system,
                    user_json=user_json,
                    temperature=settings.temperature,
                    max_tokens=settings.max_tokens,
                )
                try:
                    input_tokens = count(
                        "input_tokens",
                        count_input_tokens(model_request)
                        if count_input_tokens is not None
                        else token_counter(rendered_system + user_json),
                    )
                except (ValueError, KeyError, TypeError) as error:
                    code = str(error)
                    runs.append(
                        replace(
                            run,
                            status="invalid_request",
                            errors=("TAGGING_INPUT_COUNT_INVALID",),
                            diagnostic_detail=type(error).__name__
                            + (f": {code}" if code in _SAFE_INPUT_COUNT_CODES else ""),
                        )
                    )
                    continue
                if not reserve_budget(
                    usage_store,
                    call,
                    input_tokens=input_tokens,
                    max_output_tokens=settings.max_tokens,
                    pricing=pricing,
                    now=now(),
                ):
                    runs.append(run)
                    continue
                if not usage_store.mark_dispatched(call):
                    runs.append(run)
                    continue
                try:
                    response = invoke(model_request)
                    if not isinstance(response, RawTagResponse) or not isinstance(
                        response.usage, TokenUsage
                    ):
                        raise DomainValidationError("raw response and usage required")
                except Exception:
                    response = RawTagResponse(
                        None,
                        TokenUsage(None, None, None, None, 0, "failed", None, "MODEL_UNAVAILABLE"),
                        settings.binding.synthetic,
                    )
                record_usage(usage_store, call, response.usage, now=now())
                envelope = dict(
                    identity=identity,
                    raw_response_json=response.raw_response_json,
                    usage=asdict(response.usage),
                    synthetic=response.synthetic,
                    provider_response_json=response.provider_response_json,
                )
                cache.put_raw(request, canonical_json(envelope).encode())
        except BudgetExceeded:
            runs.append(replace(run, status="budget_exhausted", errors=("BUDGET_EXHAUSTED",)))
            continue
        except (ValueError, KeyError, TypeError):
            runs.append(replace(run, status="invalid_cache", errors=("CACHE_INVALID",)))
            continue
        run = replace(
            run,
            raw_response_json=response.raw_response_json,
            usage=response.usage,
            provider_response_json=response.provider_response_json,
        )
        if response.synthetic != settings.binding.synthetic:
            runs.append(
                replace(run, status="invalid_response", errors=("MODEL_PROVENANCE_INVALID",))
            )
            continue
        if response.usage.status != "succeeded" or response.raw_response_json is None:
            runs.append(replace(run, status="model_failed", errors=("MODEL_UNAVAILABLE",)))
            continue
        try:
            if len(response.raw_response_json.encode()) > settings.max_response_bytes:
                raise DomainValidationError("response exceeds limit")
            raw_tags = llm_tags_from_dict(json.loads(response.raw_response_json))
            if (raw_tags.claim_id, raw_tags.packet_sha256, raw_tags.replicate_id) != (
                claim.claim_id,
                packet.packet_sha256,
                replica,
            ):
                raise DomainValidationError("response identity mismatch")
            ids = [element.element_id for element in raw_tags.elements]
            if len(set(ids)) != len(ids) or set(ids) != set(allowed):
                raise DomainValidationError("response must tag every allowed element once")
        except (ValueError, TypeError, UnicodeError):
            runs.append(replace(run, status="invalid_response", errors=("LLM_SCHEMA_INVALID",)))
            continue
        elements, errors, scopes, bindings = [], [], [], []
        if (
            raw_tags.track != track.track
            or raw_tags.safe_harbor_category != track.safe_harbor_category
        ):
            errors.append("TRACK_CATEGORY_CHANGED")
        if raw_tags.superlative_quote and raw_tags.superlative_quote not in claim.quote:
            errors.append("SUPERLATIVE_SOURCE_INVALID")
        for element in raw_tags.elements:
            state = element.state
            refs = []
            scope_set = set()
            if state == "present":
                for source in element.evidence_refs:
                    candidate = source_candidates.get(source.source_id)
                    contained = any(
                        s.source_id == source.source_id
                        and s.char_start <= source.char_start < source.char_end <= s.char_end
                        for s in approved_refs
                    )
                    if (
                        not candidate
                        or not contained
                        or element.element_id not in candidate["allowed_elements"]
                        or candidate["source_scope"]
                        not in definitions[element.element_id]["source_scopes"]
                    ):
                        state = "unknown"
                        break
                    verified = verify_source_ref(source, original, tenant_id=tenant_id)
                    if (
                        verified.verification_state != "verified"
                        or accept_binding(
                            context,
                            verified,
                            relation_tags_for(verified, relation_tags),
                            original=original,
                            tenant_id=tenant_id,
                            rulepack=rulepack,
                            element_id=element.element_id,
                        )
                        != "accepted"
                    ):
                        state = "unknown"
                        break
                    refs.append(verified)
                    scope_set.add(candidate["source_scope"])
                if element.normalized_value is None and element.element_id in (
                    "G1",
                    "G2",
                    "G3",
                    "G5",
                    "P1",
                    "P2",
                ):
                    state = "unknown"
                if element.normalized_value is not None and not any(
                    " ".join(normalize("NFC", element.normalized_value).split())
                    == " ".join(normalize("NFC", source.quote).split())
                    for source in refs
                ):
                    state = "unknown"
                if element.credited_from is not None and element.credited_from not in {
                    ref.source_id for ref in refs
                }:
                    state = "unknown"
                # These are outputs of dedicated deterministic services, never LLM votes.
                if element.element_id in ("P4", "P6") or not refs:
                    state = "unknown"
            elif state in ("absent", "not_applicable"):
                state = "unknown"
            if element.element_id == "P6" and element.state in ("present", "conflict"):
                # Both agreement AND contradiction require the numeric service.
                # Retain the raw vote and citations for review, never a model verdict.
                state = "unknown"
                errors.append("DETERMINISTIC_CHECK_REQUIRED:P6")
            if state in ("unknown", "conflict"):
                errors.append(f"UNRESOLVED:{element.element_id}")
            scope = next(
                (
                    scope
                    for scope in ("global_bound", "same_table", "local_claim")
                    if scope in scope_set
                ),
                "local_claim",
            )
            scopes.append((element.element_id, scope))
            bindings.append(
                (
                    element.element_id,
                    canonical_hash(
                        {
                            key: source.quote if source else None
                            for key, source in context.dimensions.items()
                        }
                    ),
                )
            )
            elements.append(
                replace(
                    element,
                    state=state,
                    evidence_refs=tuple(refs) if state == "present" else element.evidence_refs,
                    normalized_value=element.normalized_value if state == "present" else None,
                    credited_from=element.credited_from if state == "present" else None,
                )
            )
        guarded_tags = replace(raw_tags, elements=tuple(elements))
        run = replace(
            run,
            guarded=guarded_tags,
            status="needs_review" if errors else "succeeded",
            errors=tuple(errors),
            source_scopes=tuple(scopes),
            binding_hashes=tuple(bindings),
        )
        # Include postprocessor/graph/rule/role pins; a changed guard cannot overwrite a revision.
        post_request = replace(
            request,
            request_signature=canonical_hash(
                dict(
                    raw_request=asdict(request),
                    graph=run.graph_sha256,
                    rule=run.rule_sha256,
                    roles={
                        sid: {name: asdict(ref) if ref else None for name, ref in values.items()}
                        for sid, values in relation_tags.items()
                    },
                    claim_roles={
                        name: asdict(ref) if ref else None
                        for name, ref in context.dimensions.items()
                    },
                    track=track.track,
                    safe_harbor_category=track.safe_harbor_category,
                    guard_version="tagging-010-v4-numeric-origin",
                )
            ),
        )
        cache.put_guarded(post_request, canonical_json(asdict(run) | {"recovered": False}).encode())
        runs.append(run)
    return tuple(runs)
