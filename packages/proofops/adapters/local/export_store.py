"""Local-synthetic-only durable export adapter, schema v1.

Additive job-record kinds and guards only; existing readers are compatible.
Rollback disables export routes/writers and retains immutable artifacts/history.
No AWS, model calls, or production storage guarantees are implied.
"""

import json
import secrets
from dataclasses import asdict
from hashlib import sha256
from uuid import uuid4

from proofops.adapters.local.audit_store import append_audit_transaction, read_audit_head
from proofops.adapters.local.run_artifacts import load_run_graph
from proofops.adapters.local.summary_store import LocalSummaryStore
from proofops.application.exports import (
    MAX_EXPORT_BYTES,
    REVISION_RECORDS_V2,
    SEARCH_COVERAGE_V1,
    SOURCE_RECEIPTS_V1,
    ExportRejected,
    encode_report_level_source_receipts,
    encode_revision_record,
    encode_search_coverages,
    timestamp,
)
from proofops.application.reporting import _basis_refs, _tag_elements, build_report_model
from proofops.domain.audit import ChangeSet
from proofops.domain.provenance import canonical_hash
from proofops.domain.rulepacks import canonical_json


class LocalExportStore:
    kind = "local-synthetic-only"

    def __init__(self, runs, claims):
        self.runs, self.claims, self.jobs = runs, claims, runs.jobs
        self.summaries = LocalSummaryStore(runs, claims)
        with self.jobs._transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS export_schema (version INTEGER PRIMARY KEY)")
            if db.execute("SELECT version FROM export_schema").fetchall() not in ([], [(1,)]):
                raise ExportRejected("UNSUPPORTED_EXPORT_SCHEMA")
            db.execute("INSERT OR IGNORE INTO export_schema VALUES (1)")
            kinds = "'export_request','export_snapshot','export_artifact','export_ticket'"
            for action in ("UPDATE", "DELETE"):
                db.execute(f"""CREATE TRIGGER IF NOT EXISTS export_immutable_{action.lower()}
                    BEFORE {action} ON job_records WHEN OLD.kind IN ({kinds})
                    BEGIN SELECT RAISE(ABORT, 'immutable export artifact'); END""")
            db.execute(f"""CREATE TRIGGER IF NOT EXISTS export_immutable_insert
                BEFORE INSERT ON job_records WHEN NEW.kind IN ({kinds}) AND EXISTS (
                SELECT 1 FROM job_records WHERE tenant_id=NEW.tenant_id AND run_id=NEW.run_id
                AND kind=NEW.kind AND record_id=NEW.record_id)
                BEGIN SELECT RAISE(ABORT, 'export artifact exists'); END""")

    def _state(self, db, tenant, export_id):
        row = db.execute(
            "SELECT value FROM job_records WHERE tenant_id=? AND kind='export_state' "
            "AND record_id=?",
            (tenant, export_id),
        ).fetchone()
        if row is None:
            raise ExportRejected("RESOURCE_NOT_FOUND", 404)
        state = json.loads(row[0])
        try:
            self.jobs._get(db, tenant, state["response"]["run_id"], "run", "META")
        except KeyError:
            raise ExportRejected("RESOURCE_NOT_FOUND", 404) from None
        return state

    def state(self, tenant, export_id):
        with self.jobs._transaction() as db:
            return self._state(db, tenant, export_id)

    def get(self, tenant, export_id):
        return self.state(tenant, export_id)["response"]

    def reserve(self, actor, run_id, body, key, *, now):
        tenant = actor.tenant_id
        request_key = canonical_hash([actor.user_sub, "export_create", key])
        request_hash = canonical_hash([run_id, body])
        with self.jobs._transaction() as db:
            try:
                run = self.jobs._get(db, tenant, run_id, "run", "META")
            except KeyError:
                raise ExportRejected("RESOURCE_NOT_FOUND", 404) from None
            row = db.execute(
                "SELECT value FROM job_records WHERE tenant_id=? AND kind='export_request' "
                "AND record_id=?",
                (tenant, request_key),
            ).fetchone()
            if row:
                previous = json.loads(row[0])
                if previous["request_hash"] != request_hash:
                    raise ExportRejected("IDEMPOTENCY_CONFLICT")
                return previous["export_id"]
            export_id = str(uuid4())
            response = dict(
                export_id=export_id,
                run_id=run_id,
                state="queued",
                snapshot_epoch=run["mutation_epoch"],
                partial=True,
                manifest_sha256=None,
                created_at=timestamp(now),
            )
            self.jobs._put(
                db,
                tenant,
                run_id,
                "export_request",
                request_key,
                dict(request_hash=request_hash, export_id=export_id),
                immutable=True,
            )
            self.jobs._put(
                db,
                tenant,
                run_id,
                "export_state",
                export_id,
                dict(response=response, body=body, actor=actor.user_sub, retry_at=0, errors=[]),
            )
            return export_id

    def frozen(self, tenant, export_id):
        with self.jobs._transaction() as db:
            state = self._state(db, tenant, export_id)
            raw = self.jobs._raw(
                db, tenant, state["response"]["run_id"], "export_snapshot", export_id
            )
            if raw is None:
                return None
            if sha256(raw).hexdigest() != state["snapshot_sha256"]:
                raise ExportRejected("EXPORT_INTEGRITY_FAILED")
            return json.loads(raw)

    def capture(self, tenant, export_id):
        state = self.state(tenant, export_id)
        run_id = state["response"]["run_id"]
        initial = self.jobs.get_run(tenant, run_id)
        # ponytail: local exports cap at 1000 claims/32 MiB; stream remote artifacts above this.
        if initial.get("coverage", {}).get("claims_discovered", 0) > 1000:
            raise ExportRejected("EXPORT_SIZE_LIMIT")
        # Heavy immutable source/graph replay remains outside SQLite's write lock.
        graph = (
            load_run_graph(
                self.runs, self.claims.uploads, self.claims.parser, tenant_id=tenant, run_id=run_id
            )
            if "parse_job" in initial
            else None
        )
        claims = self.claims.list(tenant, run_id) if "extract_job" in initial else ()
        if len(claims) > 1000:
            raise ExportRejected("EXPORT_SIZE_LIMIT")
        claim_map = {claim.claim_id: claim for claim in claims}
        with self.jobs._transaction() as db:
            run = self.jobs._get(db, tenant, run_id, "run", "META")
            snapshot = self.runs._snapshot(db, tenant, run_id)
            ids = self.summaries._claim_ids(db, run, snapshot)
            if initial["mutation_epoch"] != run["mutation_epoch"] or set(ids) != set(claim_map):
                # Freeze will reject this attempt and retry the capture only.
                return {"manifest": {"mutation_epoch": initial["mutation_epoch"]}}
            if (
                canonical_hash({k: v for k, v in snapshot.items() if k != "input_hash"})
                != snapshot["input_hash"]
            ):
                raise ExportRejected("EXPORT_INTEGRITY_FAILED")
            refs, records, raw_records, hashes = [], {}, {}, {snapshot["rulepack"]["sha256"]}
            source_receipts = {}
            search_coverages, coverage_documents = {}, {}
            captured_bytes = 0
            for claim_id in ids:
                raw_head = self.jobs._raw(db, tenant, run_id, "claim_head", claim_id)
                head = (
                    json.loads(raw_head) if raw_head else dict(tag_revision=0, decision_revision=0)
                )
                refs.append(dict(claim_id=claim_id, **head))
                claim = claim_map[claim_id]
                record = dict(
                    tenant_id=tenant,
                    document_version_id=claim.document_version_id,
                    claim_id=claim_id,
                    claim_quote=claim.quote,
                    **head,
                    parse_manifest_id=graph.parse_manifest_id,
                    source_sha256=graph.source_sha256,
                    source_refs=[asdict(s) for s in claim.source_refs],
                    model_sha256=None,
                    prompt_sha256=None,
                    replicate_hashes=[],
                )
                classification_raw = self.jobs._raw(
                    db, tenant, run_id, "preliminary_classification_head", claim_id
                )
                if classification_raw is not None:
                    classification = json.loads(classification_raw)
                    immutable = self.jobs._get(
                        db,
                        tenant,
                        run_id,
                        "preliminary_classification",
                        classification["classification_id"],
                    )
                    if (
                        classification != immutable
                        or classification["record_sha256"]
                        != canonical_hash(
                            {k: v for k, v in classification.items() if k != "record_sha256"}
                        )
                        or tuple(
                            classification.get(k)
                            for k in (
                                "tenant_id",
                                "run_id",
                                "claim_id",
                                "document_version_id",
                                "parse_manifest_id",
                                "source_sha256",
                            )
                        )
                        != (
                            tenant,
                            run_id,
                            claim_id,
                            claim.document_version_id,
                            claim.parse_manifest_id,
                            claim.source_sha256,
                        )
                    ):
                        raise ExportRejected("EXPORT_INTEGRITY_FAILED")
                    record["classification_review"] = {
                        k: classification[k]
                        for k in (
                            "classification_id",
                            "record_sha256",
                            "revision",
                            "origin",
                            "track",
                        )
                    }
                tag, decision, raw_record = None, None, None
                if not head["tag_revision"]:
                    # New untagged snapshots carry an empty list; old snapshots
                    # omit the key entirely and project to null (unavailable).
                    record["tag_elements"] = []
                if head["tag_revision"]:
                    current = self.claims.current_tag(tenant, run_id, claim_id, connection=db)
                    tag = current["tag"]
                    if tag["tag_revision"] != head["tag_revision"]:
                        raise ExportRejected("EXPORT_INTEGRITY_FAILED")
                    original = self.jobs._get(
                        db, tenant, run_id, "tag_revision", f"{claim_id}:0000000001"
                    )
                    inputs = tag.get("inputs", original.get("inputs"))
                    if (
                        inputs is None
                        or (
                            tag.get("input_snapshot_sha256") is not None
                            and tag["input_snapshot_sha256"] != canonical_hash(inputs)
                        )
                        or (
                            inputs["claim"]["tenant_id"],
                            inputs["claim"]["claim_id"],
                            inputs["original"]["source_sha256"],
                            inputs["original"]["parse_manifest_id"],
                        )
                        != (tenant, claim_id, graph.source_sha256, graph.parse_manifest_id)
                    ):
                        raise ExportRejected("EXPORT_INTEGRITY_FAILED")
                    confirmed = tag.get("confirmed_tags")
                    if confirmed:
                        if (
                            confirmed["tenant_id"],
                            confirmed["document_version_id"],
                            confirmed["claim_id"],
                            confirmed["tag_revision"],
                        ) != (tenant, claim.document_version_id, claim_id, head["tag_revision"]):
                            raise ExportRejected("EXPORT_INTEGRITY_FAILED")
                        provenance = confirmed
                    else:
                        provenance = inputs["tag_runs"][0] | {
                            "replicate_hashes": inputs["consensus"]["replicate_hashes"]
                        }
                    record.update(
                        {
                            k: provenance[k]
                            for k in ("model_sha256", "prompt_sha256", "replicate_hashes")
                        }
                    )
                    # Optional immutable usability snapshot of accepted values,
                    # states, and exact quotes from the pinned tag revision.
                    # Reuses domain/reporting validators; no grading or inference.
                    try:
                        raw_elements = tag.get("elements")
                        if not isinstance(raw_elements, list):
                            raise ValueError("tag elements must be an array")
                        record["tag_elements"] = _tag_elements(
                            raw_elements, claim.document_version_id, graph.parse_manifest_id
                        )
                    except (ValueError, TypeError, KeyError) as exc:
                        raise ExportRejected("EXPORT_INTEGRITY_FAILED") from exc
                    # Preserve tag uncertainty even while approval prevents a decision.
                    # Existing frozen snapshots omit this optional list; never rewrite them.
                    if not head["decision_revision"]:
                        record["unresolved_elements"] = [
                            element["element_id"]
                            for element in tag["elements"]
                            if element["state"] in ("unknown", "conflict")
                        ]
                    if head["decision_revision"]:
                        decision = self.jobs._get(
                            db,
                            tenant,
                            run_id,
                            "decision_revision",
                            f'{claim_id}:{head["decision_revision"]:010}',
                        )
                        if (
                            not self.summaries._decision_has_valid_lineage(
                                db, tenant, run_id, claim_id, head, decision, tag, snapshot
                            )
                            or not confirmed
                            or decision["decision"]["input_tags_sha256"]
                            != canonical_hash(confirmed)
                        ):
                            raise ExportRejected("EXPORT_INTEGRITY_FAILED")
                        record.update(decision["decision"])
                        record["review_status"] = decision["api"]["review_status"]
                        hashes.add(record["rule_pack_sha256"])
                    # Preserve all observed states and evidence, never convert uncertainty.
                    # The original packet is stored once only when its bytes are identical.
                    raw_record = encode_revision_record(tag, decision, inputs)
                    try:
                        compacted, new_receipts = encode_report_level_source_receipts(
                            {claim_id: raw_record},
                            identity={
                                "tenant_id": tenant,
                                "document_version_id": run["document_version_id"],
                                "parse_manifest_id": graph.parse_manifest_id,
                                "source_sha256": snapshot["document"]["sha256"],
                            },
                        )
                        raw_record = compacted[claim_id]
                    except (ValueError, TypeError, KeyError) as exc:
                        raise ExportRejected("EXPORT_INTEGRITY_FAILED") from exc
                    for digest, receipt in new_receipts.items():
                        previous = source_receipts.get(digest)
                        if previous is not None and canonical_json(previous) != canonical_json(
                            receipt
                        ):
                            raise ExportRejected("EXPORT_INTEGRITY_FAILED")
                        if previous is None:
                            source_receipts[digest] = receipt
                            captured_bytes += len(canonical_json(receipt).encode())
                    try:
                        raw_record, new_coverages, new_documents = encode_search_coverages(
                            raw_record,
                            identity={
                                "tenant_id": tenant,
                                "document_version_id": run["document_version_id"],
                                "parse_manifest_id": graph.parse_manifest_id,
                                "source_sha256": snapshot["document"]["sha256"],
                            },
                        )
                    except (ValueError, TypeError, KeyError) as exc:
                        raise ExportRejected("EXPORT_INTEGRITY_FAILED") from exc
                    for table, new in (
                        (search_coverages, new_coverages),
                        (coverage_documents, new_documents),
                    ):
                        for digest, entry in new.items():
                            previous = table.get(digest)
                            if previous is not None and canonical_json(previous) != canonical_json(
                                entry
                            ):
                                raise ExportRejected("EXPORT_INTEGRITY_FAILED")
                            if previous is None:
                                table[digest] = entry
                                captured_bytes += len(canonical_json(entry).encode())
                captured_bytes += len(canonical_json([record, raw_record]).encode())
                if captured_bytes > MAX_EXPORT_BYTES:
                    raise ExportRejected("EXPORT_SIZE_LIMIT")
                records[claim_id] = record
                if raw_record is not None:
                    raw_records[claim_id] = raw_record
            basis_count = sum(
                b.get("clause") is None or b["verification_status"] != "verified"
                for record in records.values()
                for b in _basis_refs(record.get("basis_refs", []))
            )
            manifest = dict(
                tenant_id=tenant,
                run_id=run_id,
                document_version_id=run["document_version_id"],
                parse_manifest_id=graph.parse_manifest_id if graph else None,
                source_sha256=snapshot["document"]["sha256"],
                object_version_id=snapshot["document"]["object_version_id"],
                mutation_epoch=run["mutation_epoch"],
                claim_revision_refs=refs,
                rule_pack_hashes=sorted(hashes),
                coverage=self.runs._project(run, snapshot)["coverage"],
                unverified_basis=basis_count,
                generated_at=state["response"]["created_at"],
                execution_profile=self.kind,
                run_snapshot_sha256=canonical_hash(snapshot),
                model_binding_hash=snapshot["model_binding_hash"],
                parser_profile_hash=snapshot["parser_profile_hash"],
                revision_records=raw_records,
                revision_records_encoding=REVISION_RECORDS_V2,
            )
            if source_receipts:
                manifest["source_receipts_encoding"] = SOURCE_RECEIPTS_V1
                manifest["source_receipts"] = source_receipts
            if search_coverages:
                manifest["search_coverage_encoding"] = SEARCH_COVERAGE_V1
                manifest["search_coverages"] = search_coverages
                manifest["coverage_documents"] = coverage_documents
            submitted = self.claims.submitted_reviews(tenant, run_id, connection=db)
            if submitted:
                manifest["submitted_reviews"] = submitted
            return dict(manifest=manifest, decisions=records)

    def freeze(self, tenant, export_id, captured):
        with self.jobs._transaction() as db:
            state = self._state(db, tenant, export_id)
            run_id = state["response"]["run_id"]
            if self.jobs._raw(db, tenant, run_id, "export_snapshot", export_id) is not None:
                return False
            run = self.jobs._get(db, tenant, run_id, "run", "META")
            if run["mutation_epoch"] != captured["manifest"]["mutation_epoch"]:
                return False
            model = build_report_model(captured["manifest"], captured["decisions"])
            if model["partial"] and not state["body"]["allow_partial"]:
                raise ExportRejected("REPORT_NOT_FINALIZABLE")
            raw = canonical_json(captured).encode()
            if len(raw) > MAX_EXPORT_BYTES:
                raise ExportRejected("EXPORT_SIZE_LIMIT")
            self.jobs._put(db, tenant, run_id, "export_snapshot", export_id, raw, immutable=True)
            state.update(snapshot_sha256=sha256(raw).hexdigest())
            state["response"].update(state="building", snapshot_epoch=run["mutation_epoch"])
            self.jobs._put(db, tenant, run_id, "export_state", export_id, state)
            return True

    def complete(self, tenant, export_id, content, manifest_hash, partial):
        with self.jobs._transaction() as db:
            state = self._state(db, tenant, export_id)
            if state["response"]["state"] == "ready":
                return state["response"]
            run_id = state["response"]["run_id"]
            self.jobs._put(
                db, tenant, run_id, "export_artifact", export_id, content, immutable=True
            )
            state.update(artifact_sha256=sha256(content).hexdigest(), errors=[], retry_at=0)
            state["response"].update(state="ready", manifest_sha256=manifest_hash, partial=partial)
            append_audit_transaction(
                connection=db,
                change=ChangeSet(
                    tenant,
                    run_id,
                    state["actor"],
                    "export_created",
                    export_id,
                    None,
                    state["artifact_sha256"],
                    1,
                    "Immutable local export snapshot.",
                ),
                expected_head=read_audit_head(db, tenant, run_id),
                event_id=str(uuid4()),
                timestamp=state["response"]["created_at"],
            )
            self.jobs._put(db, tenant, run_id, "export_state", export_id, state)
            return state["response"]

    def defer(self, tenant, export_id, code, *, now):
        with self.jobs._transaction() as db:
            state = self._state(db, tenant, export_id)
            if state["response"]["state"] == "ready":
                return
            state.update(errors=[code], retry_at=now + 5)
            state["response"]["state"] = "queued" if code == "EXPORT_SNAPSHOT_BUSY" else "failed"
            self.jobs._put(
                db, tenant, state["response"]["run_id"], "export_state", export_id, state
            )

    def _content(self, db, tenant, export_id, state):
        if state["response"]["state"] != "ready":
            raise ExportRejected("EXPORT_NOT_READY")
        content = self.jobs._raw(
            db, tenant, state["response"]["run_id"], "export_artifact", export_id
        )
        if content is None or sha256(content).hexdigest() != state["artifact_sha256"]:
            raise ExportRejected("EXPORT_INTEGRITY_FAILED")
        return content

    def authorize_download(self, actor, export_id, *, now):
        with self.jobs._transaction() as db:
            state = self._state(db, actor.tenant_id, export_id)
            self._content(db, actor.tenant_id, export_id, state)
            token = secrets.token_urlsafe(32)
            ticket = dict(
                user_sub=actor.user_sub,
                export_id=export_id,
                expires=now + 300,
                sha256=state["artifact_sha256"],
            )
            self.jobs._put(
                db,
                actor.tenant_id,
                state["response"]["run_id"],
                "export_ticket",
                sha256(token.encode()).hexdigest(),
                ticket,
                immutable=True,
            )
            return dict(
                url=f"/v1/exports/{export_id}/content?ticket={token}",
                expires_at=timestamp(now + 300),
                sha256=ticket["sha256"],
            )

    def content(self, actor, export_id, token, *, now):
        with self.jobs._transaction() as db:
            state = self._state(db, actor.tenant_id, export_id)
            raw = self.jobs._raw(
                db,
                actor.tenant_id,
                state["response"]["run_id"],
                "export_ticket",
                sha256(token.encode()).hexdigest(),
            )
            ticket = json.loads(raw) if raw else {}
            if (
                ticket.get("user_sub") != actor.user_sub
                or ticket.get("export_id") != export_id
                or ticket.get("expires", 0) <= now
                or ticket.get("sha256") != state["artifact_sha256"]
            ):
                raise ExportRejected("DOWNLOAD_TICKET_INVALID", 403)
            return self._content(db, actor.tenant_id, export_id, state)
