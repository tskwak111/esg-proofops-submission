"""Scoped local Upstage composition; immutable receipts, no implicit rule approval."""

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from uuid import UUID, uuid5

from proofops.adapters.local.tag_store import tagging_settings
from proofops.adapters.local.upstage import request_usage
from proofops.application.authorization import AuthContext
from proofops.application.budget import BudgetCall, BudgetExceeded, TokenUsage
from proofops.application.evidence.binding import local_relation_tags
from proofops.application.evidence.span_citations import verify_source_ref
from proofops.application.input_reservation import validate_capacity_policy
from proofops.application.ports.jobs import LeaseLost
from proofops.application.preflight import check_local_upstage_tagger
from proofops.application.registry import Registry, artifact_sha256
from proofops.application.tagging.preliminary import (
    preliminary_request,
    preliminary_table_request,
    validate_preliminary,
    validate_preliminary_table_sources,
)
from proofops.application.tagging.relations import (
    RelationValidationError,
    relation_request,
    validate_relations,
)
from proofops.application.tagging.service import RawTagResponse
from proofops.domain.provenance import canonical_hash
from proofops.domain.rulepacks import canonical_json
from proofops.domain.values import _source_ref_from_dict
from proofops_agent.upstage_preliminary import UpstagePreliminaryTransport
from proofops_agent.upstage_tagging import (
    NEVER_SENT_ERROR_CODES,
    TransportResume,
    UpstageTaggingTransport,
)

from proofops_worker.consumer import TagHeartbeatFailed

# The local stop conditions this module raises itself, per replica prefix. Recording
# only these keeps a durable record free of arbitrary exception text while still
# naming the exact condition that stopped the operation.
_LOCAL_STOP_SUFFIXES = (
    "_RECEIPT_INCOMPLETE_OR_MISMATCH",
    "_PENDING_CALL",
    "_PROVIDER_FAILED",
    "_PROVIDER_ID_REQUIRED",
    "_RECOVERY_ALLOWANCE_EXHAUSTED",
)
LOCAL_STOP_CODE = "LOCAL_STOP"


def _local_stop_code(prefix, error):
    """Map a local stop to a stable code; never echo an unrecognized message."""
    known = {prefix.upper() + suffix for suffix in _LOCAL_STOP_SUFFIXES}
    text = str(error)
    if text in known:
        return text
    return prefix.upper() + "_" + LOCAL_STOP_CODE + ":" + type(error).__name__


class LiveTaggingRuntime:
    synthetic = False
    heartbeat_state = None

    def _check_heartbeat(self):
        if self.heartbeat_state is not None:
            self.heartbeat_state.check()

    def __init__(self, runner, snapshot, graph, lease, usage, *, probe, ledger, receipts):
        if (
            snapshot.get("tagging_mode") != "upstage_local"
            or snapshot["input_hash"]
            != canonical_hash({k: v for k, v in snapshot.items() if k != "input_hash"})
            or (graph.tenant_id, graph.document_version_id, graph.source_sha256)
            != (
                snapshot["tenant_id"],
                snapshot["document"]["version_id"],
                snapshot["document"]["sha256"],
            )
        ):
            raise ValueError("LIVE_TAGGING_SNAPSHOT_MISMATCH")
        self.runner, self.snapshot, self.graph, self.lease, self.usage = (
            runner,
            snapshot,
            graph,
            lease,
            usage,
        )
        self.ledger, self.receipts = ledger, Path(receipts)
        # An acknowledged bounded resume authorization, when the owning runner is
        # executing an explicit recovery job. None in ordinary operation, which
        # keeps every stop, receipt and budget behavior exactly as it was.
        self.resume = getattr(runner, "resume", None)
        if self.resume is not None:
            if not isinstance(self.resume, TransportResume):
                raise ValueError("LIVE_TAGGING_RESUME_INVALID")
            if (
                self.resume.expected_root is not None
                and self.resume.expected_root.resolve() != self.receipts.resolve()
            ):
                # An authorization is bound to the exact receipt tree it was proven
                # against; it can never be applied to a different local state.
                raise ValueError("LIVE_TAGGING_RESUME_ROOT_MISMATCH")
        self.settings = tagging_settings(snapshot)
        self.preliminary_settings = tagging_settings(snapshot, preliminary=True)
        self.registry = Registry.sqlite(runner.store.path)
        self.auth = AuthContext(
            "local-worker", graph.tenant_id, "viewer", frozenset(), snapshot["run_id"]
        )
        self.allowed_packets = set()
        self.preliminary_records = {}
        self.relation_records = {}
        self.relation_settings = (
            tagging_settings(snapshot, relation=True) if "relation_settings" in snapshot else None
        )
        self.request_ids = set()
        self._account_lock = Lock()
        self.previously_accounted = {
            identifier
            for row in runner.store.jobs.list_usage(graph.tenant_id, snapshot["run_id"])
            for identifier in row.get("tag_request_ids", [])
        }
        self.element_transport = UpstageTaggingTransport(
            probe,
            self.receipts / "elements",
            settings=self.settings,
            tenant_id=graph.tenant_id,
            authorize=self._authorize,
            resume=self.resume,
        )
        self.preliminary_transport = UpstagePreliminaryTransport(
            probe,
            self.receipts / "preliminary",
            settings=self.preliminary_settings,
            tenant_id=graph.tenant_id,
            authorize=self._authorize,
            resume=self.resume,
        )
        if self.relation_settings is not None:
            from proofops_agent.upstage_relations import UpstageRelationsTransport

            self.relation_transport = UpstageRelationsTransport(
                probe,
                self.receipts / "relation",
                settings=self.relation_settings,
                tenant_id=graph.tenant_id,
                authorize=self._authorize,
                resume=self.resume,
            )
        self._capacity(self.settings.model_id)

    def _capacity(self, model_id):
        if model_id == "openai/gpt-6-luna":
            from proofops.adapters.local.openrouter import (
                validate_capacity_policy as validate_luna_capacity,
            )

            return validate_luna_capacity(
                self.snapshot["input_reservation_policy"], model_id=model_id
            )
        return validate_capacity_policy(
            self.snapshot["input_reservation_policy"],
            model_id=model_id,
            checked_at=datetime.fromtimestamp(self.runner.clock(), UTC),
        )

    def _fence(self):
        self._check_heartbeat()
        now = int(self.runner.clock())
        if not self.runner.store.jobs.can_call(self.lease, now=now):
            raise LeaseLost("LEASE_LOST")
        try:
            self.runner.store.jobs.heartbeat(self.lease, now=now, lease_seconds=300)
        except Exception as error:
            if self.heartbeat_state is not None:
                self.heartbeat_state.fail()
            if isinstance(error, LeaseLost):
                raise
            raise TagHeartbeatFailed() from None
        self._check_heartbeat()

    def _authorize(self, settings, request):
        self._fence()
        prefix = next(
            (
                name
                for name, selected in (
                    ("preliminary", self.preliminary_settings),
                    ("tagging", self.settings),
                    ("relation", self.relation_settings),
                )
                if selected is not None and settings == selected
            ),
            None,
        )
        if prefix is None:
            raise ValueError("LIVE_TAGGING_SETTINGS_MISMATCH")
        if (request.get("claim_id"), request.get("packet_sha256")) not in self.allowed_packets:
            raise ValueError("LIVE_TAGGING_PACKET_NOT_AUTHORIZED")
        self._capacity(settings.model_id)
        policy_hash = canonical_hash(self.snapshot["input_reservation_policy"])
        if policy_hash != self.snapshot["input_reservation_policy_hash"]:
            raise ValueError("LIVE_TAGGING_POLICY_MISMATCH")
        binding = self.snapshot[prefix + "_runtime"]
        if binding.get("input_reservation_policy_sha256") != policy_hash:
            raise ValueError("LIVE_TAGGING_POLICY_NOT_AUTHORIZED")
        # A frozen grant cannot conceal a later revocation or replacement.
        for kind, frozen, identifier in (
            ("runtime", binding, "runtime_binding_id"),
            ("consent", self.snapshot["consent"], "consent_profile_id"),
            ("rights", self.snapshot["rights"], "rights_profile_id"),
        ):
            try:
                current = self.registry.resolve_profile(self.auth, kind, frozen[identifier])
            except LookupError:
                raise ValueError("LIVE_TAGGING_AUTHORIZATION_REVOKED") from None
            if artifact_sha256(current) != artifact_sha256(frozen):
                raise ValueError("LIVE_TAGGING_AUTHORIZATION_CHANGED")
        return check_local_upstage_tagger(
            binding=binding,
            consent=self.snapshot["consent"],
            settings=settings,
            auth=self.auth,
            checked_at=datetime.fromtimestamp(self.runner.clock(), UTC).isoformat(),
            source_sha256=self.graph.source_sha256,
            document_rights=self.snapshot["document"]["metadata"]["rights_profile_id"],
        )

    def allow_packet(self, claim_id, packet_sha256):
        self.allowed_packets.add((claim_id, packet_sha256))

    @staticmethod
    def token_counter(text):
        # Retrieval size bound only; provider reservation uses the full capacity below.
        return len(text.encode("utf-8"))

    def count_input_tokens(self, request):
        # Validate the actual wire and current authority before reserving its upper bound.
        # A withdrawn or exhausted recovery allowance stops here, before any
        # reservation: tag_replicates records TAGGING_INPUT_COUNT_INVALID and
        # moves on without buying a call or settling a ledger row.
        if not self.element_transport.may_dispatch():
            raise ValueError("TAGGING_RECOVERY_ALLOWANCE_EXHAUSTED")
        return self.element_transport.count_input_tokens(
            request, counter=lambda system, user: self._capacity(self.settings.model_id)
        )

    def account(self, request_id):
        with self._account_lock:
            if request_id not in self.previously_accounted:
                self.request_ids.add(request_id)
            self.usage.update(request_usage(self.ledger, sorted(self.request_ids)))
            self.usage["tag_request_ids"] = sorted(self.request_ids)

    def invoke(self, request):
        try:
            return self.element_transport.invoke(request)
        finally:
            self.account(request["request_id"])

    def preliminary(self, claim, graph):
        profile = self.preliminary_settings.model_profile
        from proofops.application.tagging.preliminary import CONTEXT_POSITION_ORDER

        position_order = profile in (
            "upstage-preliminary-source-quotes-table-role-v1-position-v1",
            "upstage-preliminary-source-quotes-table-role-v2-p2-position-v1",
        )
        if position_order and self.snapshot.get("position_context_order") != CONTEXT_POSITION_ORDER:
            raise ValueError("PRELIMINARY_CONTEXT_ORDER_MISMATCH")
        p2 = profile in (
            "upstage-preliminary-source-quotes-table-role-v2-p2",
            "upstage-preliminary-source-quotes-table-role-v2-p2-position-v1",
        )
        actor_role_v2 = profile == "upstage-preliminary-source-quotes-actor-role-v2"
        actor_role = profile == "upstage-preliminary-source-quotes-actor-role-v1" or actor_role_v2
        goal_role = profile == "upstage-preliminary-source-quotes-goal-role-v1" or actor_role
        role_table = (
            profile
            in (
                "upstage-preliminary-source-quotes-table-role-v1",
                "upstage-preliminary-source-quotes-table-role-v1-position-v1",
            )
            or goal_role
            or p2
        )
        table = profile == "upstage-preliminary-source-quotes-table-v1" or role_table
        if table:
            packet = preliminary_table_request(
                claim,
                graph,
                tenant_id=self.auth.tenant_id,
                role_resolution=role_table,
                goal_role=goal_role,
                actor_role=actor_role,
                period_role=actor_role_v2,
                p2=p2,
                position_order=position_order,
            )
        else:
            packet = preliminary_request(
                claim,
                graph,
                tenant_id=self.auth.tenant_id,
                include_context=profile == "upstage-preliminary-source-quotes-context-v1",
            )

        packet = self.preliminary_transport.bound_context(packet)

        def validate(raw):
            # The table profile recomputes its own quotable source tuple from
            # claim+graph; the legacy validator stays the only path for the two
            # older profiles, so stored responses replay unchanged. The
            # role-resolution profile and the goal-role profile differ only
            # in their prompt, so they reuse this same validator without a new
            # schema or a new source rule.
            result = (
                validate_preliminary_table_sources(claim, graph, raw, tenant_id=self.auth.tenant_id)
                if table
                else validate_preliminary(claim, graph, raw, tenant_id=self.auth.tenant_id)
            )
            return result, dict(
                track=asdict(result.track) if result.track else None,
                dimensions={
                    key: asdict(value) if value else None
                    for key, value in result.context.dimensions.items()
                },
                safe_harbor_category=result.safe_harbor_category,
            )

        results = self._source_replicas(
            "preliminary",
            claim,
            packet,
            self.preliminary_settings,
            self.preliminary_transport,
            self.preliminary_records,
            validate,
        )
        if results is None or results[0].track is None:
            return None
        context = results[0].context
        return results[0].track, context, local_relation_tags(context)

    def preliminary_agreement(self, claim_id):
        """Field-level replica agreement for review only; never a tag, state or grade.

        Reads the replica records this run already stored and compares each
        asserted field on its own, because the replica signature covers the whole
        classification: one differing field otherwise hides what every replica
        did agree on. A field is agreed only when all three validated replicas
        returned the identical literal value, conflict when validated values
        differ, and unresolved when a replica is missing or did not report that
        axis. A null category against a non-null one is a conflict: it is never
        reported as null or not applicable, so an unagreed safe-harbor category
        cannot reach the rule engine as a fact.
        """
        records = self.preliminary_records.get(claim_id, [])
        validated = [
            record["values"] for record in records if record["status"] == "validated_candidate"
        ]

        def state(reported):
            distinct = {canonical_json(value) for value in reported}
            return dict(
                state="unresolved"
                if len(validated) != 3 or len(reported) != len(validated)
                else "agreed"
                if len(distinct) == 1
                else "conflict",
                replicate_values=reported,
                distinct_count=len(distinct),
            )

        axes = sorted({axis for values in validated for axis in values["dimensions"]})
        return dict(
            schema="preliminary_field_agreement_v1",
            claim_id=claim_id,
            replicates=len(records),
            validated_replicates=len(validated),
            fields=dict(
                # The literal asserted track, separate from the category candidate.
                track=state(
                    [values["track"]["track"] if values["track"] else None for values in validated]
                ),
                safe_harbor_category=state(
                    [values["safe_harbor_category"] for values in validated]
                ),
            ),
            dimensions={
                axis: state(
                    [
                        values["dimensions"][axis]
                        for values in validated
                        if axis in values["dimensions"]
                    ]
                )
                for axis in axes
            },
        )

    def relations(self, claim, packet):
        """Tag only verified external sources already present in this frozen packet."""
        self.relation_records[claim.claim_id] = []
        if self.relation_settings is None:
            return {}
        try:
            data = packet.to_dict()
            expected = dict(
                tenant_id=self.auth.tenant_id,
                run_id=self.snapshot["run_id"],
                claim_id=claim.claim_id,
                document_version_id=self.graph.document_version_id,
                parse_manifest_id=self.graph.parse_manifest_id,
                source_sha256=self.graph.source_sha256,
                graph_sha256=canonical_hash(asdict(self.graph)),
                status="candidate",
            )
            if any(data.get(key) != value for key, value in expected.items()):
                raise ValueError("RELATION_PACKET_MISMATCH")
            if (
                claim.tenant_id,
                claim.document_version_id,
                claim.parse_manifest_id,
                claim.source_sha256,
            ) != (
                self.auth.tenant_id,
                self.graph.document_version_id,
                self.graph.parse_manifest_id,
                self.graph.source_sha256,
            ):
                raise ValueError("RELATION_CLAIM_MISMATCH")
            local = {ref.source_id for ref in claim.source_refs}
            blocks = {block.source_id: block for block in self.graph.blocks}
            selected = {}
            for candidate in data["evidence_candidates"]:
                for raw in candidate["source_refs"]:
                    ref = _source_ref_from_dict(raw)
                    if ref.source_id in local:
                        continue
                    source = verify_source_ref(ref, self.graph, tenant_id=self.auth.tenant_id)
                    block = blocks.get(ref.source_id)
                    if source.verification_state != "verified" or block is None:
                        continue
                    canonical = verify_source_ref(
                        block.source_ref(), self.graph, tenant_id=self.auth.tenant_id
                    )
                    if source != canonical or source.quote != ref.quote:
                        continue  # Never widen a selected substring to its parent paragraph.
                    selected[ref.source_id] = source
            sources = tuple(selected.values())
            if not sources:
                return {}
            envelope = relation_request(sources, self.graph, tenant_id=self.auth.tenant_id) | {
                "claim_id": claim.claim_id,
                "retrieval_packet_sha256": packet.packet_sha256,
            }
        except (ValueError, KeyError, TypeError, AttributeError):
            return None

        def validate(raw):
            roles = validate_relations(sources, self.graph, raw, tenant_id=self.auth.tenant_id)
            values = {
                sid: {name: asdict(ref) if ref else None for name, ref in dimensions.items()}
                for sid, dimensions in roles.items()
            }
            return roles, values

        results = self._source_replicas(
            "relation",
            claim,
            envelope,
            self.relation_settings,
            self.relation_transport,
            self.relation_records,
            validate,
            retrieval_packet_sha256=packet.packet_sha256,
            require_consensus=False,
        )
        if results is None:
            return None
        # A disputed source stays wholly unresolved; never combine roles from
        # different replicas into a relationship that no replica proposed.
        return {
            source_id: roles
            if all(result[source_id] == roles for result in results)
            else {name: None for result in results for name in result[source_id]}
            for source_id, roles in results[0].items()
        }

    def _source_replicas(
        self,
        prefix,
        claim,
        packet,
        settings,
        transport,
        record_store,
        validate,
        *,
        retrieval_packet_sha256=None,
        require_consensus=True,
    ):
        packet_hash = canonical_hash(packet)
        self.allow_packet(claim.claim_id, packet_hash)
        record_store[claim.claim_id] = []

        def run_one(replica):
            records, results, signatures, provider_ids = [], [], [], []
            for replica in (replica,):
                self._check_heartbeat()
                request_id = str(
                    uuid5(UUID(self.lease.message.job_id), f"{prefix}:{claim.claim_id}:{replica}")
                )
                request = dict(
                    tenant_id=self.auth.tenant_id,
                    claim_id=claim.claim_id,
                    packet_sha256=packet_hash,
                    replicate_id=replica,
                    request_id=request_id,
                    binding=asdict(settings.binding),
                    model_id=settings.model_id,
                    model_profile=settings.model_profile,
                    region=settings.region,
                    system_prompt=settings.rendered_system,
                    user_json=canonical_json(packet),
                    temperature=settings.temperature,
                    max_tokens=settings.max_tokens,
                    input_reservation_policy_sha256=self.snapshot["input_reservation_policy_hash"],
                )
                if retrieval_packet_sha256 is not None:
                    request["retrieval_packet_sha256"] = retrieval_packet_sha256
                request["request_signature"] = canonical_hash(request)
                call = BudgetCall(
                    self.auth.tenant_id,
                    self.snapshot["run_id"],
                    self.graph.document_version_id,
                    request_id,
                    1,
                    "tagger",
                    settings.model_id,
                    settings.region,
                    settings.model_sha256,
                    request["request_signature"],
                    replica,
                )
                directory = self.receipts / prefix / request_id
                record = dict(request_id=request_id, replicate_id=replica, status="unresolved")
                records.append(record)
                response = None
                try:
                    capacity = transport.count_input_tokens(
                        request, counter=lambda system, user: self._capacity(settings.model_id)
                    )
                    if directory.exists():
                        retained = json.loads((directory / "request.json").read_text())
                        if (
                            retained["request"] != request
                            or not (directory / "response.json").is_file()
                        ):
                            raise ValueError(prefix.upper() + "_RECEIPT_INCOMPLETE_OR_MISMATCH")
                        raw = json.loads((directory / "response.json").read_text())
                        response = RawTagResponse(**(raw | {"usage": TokenUsage(**raw["usage"])}))
                    else:
                        # A withdrawn or exhausted recovery allowance stops before the
                        # reservation, so no budget is held and no ledger row settles
                        # for a request this operation is no longer authorized to send.
                        if not transport.may_dispatch():
                            raise ValueError(prefix.upper() + "_RECOVERY_ALLOWANCE_EXHAUSTED")
                        self._fence()
                        lock = (
                            self.heartbeat_state.lock
                            if self.heartbeat_state is not None
                            else nullcontext()
                        )
                        with lock:
                            self._check_heartbeat()
                            if not self.runner.store.usage.reserve_budget(
                                call,
                                input_tokens=capacity,
                                max_output_tokens=settings.max_tokens,
                                pricing=None,
                                now=int(self.runner.clock()),
                            ):
                                raise ValueError(prefix.upper() + "_PENDING_CALL")
                        self._fence()
                        with lock:
                            self._check_heartbeat()
                            if not self.runner.store.usage.mark_dispatched(call):
                                raise ValueError(prefix.upper() + "_PENDING_CALL")
                        self._check_heartbeat()
                        response = transport.invoke(request)
                    self.runner.store.usage.record_usage(
                        call, response.usage, now=int(self.runner.clock())
                    )
                    if (
                        response.synthetic
                        or response.usage.status != "succeeded"
                        or not response.raw_response_json
                    ):
                        raise ValueError(prefix.upper() + "_PROVIDER_FAILED")
                    result, values = validate(json.loads(response.raw_response_json))
                    record.update(
                        status="validated_candidate",
                        values=values,
                        raw_response_sha256=canonical_hash(response.raw_response_json),
                    )
                    if not response.usage.provider_request_id:
                        raise ValueError(prefix.upper() + "_PROVIDER_ID_REQUIRED")
                    results.append(result)
                    signatures.append(canonical_hash(values))
                    provider_ids.append(response.usage.provider_request_id)
                except LeaseLost:
                    raise
                except (ValueError, OSError, KeyError, TypeError, BudgetExceeded) as error:
                    # Never collapse the actual outcome to a bare "needs_review": a
                    # provider error_code (e.g. UPSTREAM_UNAVAILABLE) means this specific
                    # attempt was never actually sent (locally suppressed, latency 0ms,
                    # no provider_request_id); that is a distinct, stable fact from a
                    # real settled provider failure or a local/authorization/budget stop
                    # raised before any transport call. Both are surfaced here so a
                    # never-sent attempt is never misread as an unknown model outcome.
                    usage = getattr(response, "usage", None)
                    settled_code = getattr(usage, "error_code", None)
                    record.update(
                        status="needs_review",
                        stable_reason=dict(
                            # "never_sent" requires the transport's own explicitly
                            # known local-suppression code, not merely a zero latency:
                            # a fast real failure can also round to 0ms and lack a
                            # provider id. The durable proof that no call happened is
                            # the absent receipt directory, checked by the recovery
                            # classifier; this label only reports the transport's
                            # settled code without reinterpreting it.
                            category="never_sent"
                            if usage is not None
                            and usage.status == "failed"
                            and settled_code in NEVER_SENT_ERROR_CODES
                            and usage.provider_request_id is None
                            and usage.latency_ms == 0
                            else "provider_failed"
                            if usage is not None and usage.status == "failed"
                            else "local_stop",
                            # Stable internal codes only. An unrecognized exception
                            # never leaks its message text into a durable record.
                            error_code=settled_code
                            or (
                                error.code
                                if prefix == "relation"
                                and isinstance(error, RelationValidationError)
                                else _local_stop_code(prefix, error)
                            ),
                            detail=(
                                error.field
                                if prefix == "relation"
                                and isinstance(error, RelationValidationError)
                                else None
                            ),
                        ),
                    )
                    return records, results, signatures, provider_ids, True
                finally:
                    self.account(request_id)
            return records, results, signatures, provider_ids, False

        workers = (
            min(getattr(self, "replica_workers", getattr(self.runner, "max_workers", 1)), 3)
            if settings.model_id == "openai/gpt-6-luna" and settings.wire_policy_version == 2
            else 1
        )
        if workers == 1 or self.resume is not None:
            outcomes = []
            for replica in (1, 2, 3):
                outcome = run_one(replica)
                outcomes.append(outcome)
                if outcome[-1]:
                    break
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                outcomes = list(pool.map(run_one, (1, 2, 3)))
        records, results, signatures, provider_ids = [], [], [], []
        failed = False
        for outcome in outcomes:
            replica_records, replica_results, replica_signatures, replica_ids, replica_failed = (
                outcome
            )
            records.extend(replica_records)
            results.extend(replica_results)
            signatures.extend(replica_signatures)
            provider_ids.extend(replica_ids)
            failed |= replica_failed
        record_store[claim.claim_id] = records
        if failed:
            return None
        if len(set(provider_ids)) != 3 or (require_consensus and len(set(signatures)) != 1):
            return None
        return results
