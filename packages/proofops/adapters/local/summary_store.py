"""Local-synthetic summary reads over immutable claim and decision revisions."""

from __future__ import annotations

import json
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path

from proofops.application.ports.jobs import JobMessage
from proofops.application.summaries import summarize_snapshot
from proofops.domain.provenance import canonical_hash
from proofops.domain.rulepacks import RulePackSnapshot
from proofops.domain.values import _require_sha256, _require_uuid


class LocalSummaryStore:
    """Read one run epoch and all current heads in one SQLite transaction."""

    kind = "local-synthetic-only"

    def __init__(self, run_store, claims) -> None:
        if Path(run_store.path).resolve() != Path(claims.store.path).resolve():
            raise ValueError("runs and claims must share one local database")
        self.runs, self.claims = run_store, claims

    def _claim_ids(self, db, run, snapshot) -> tuple[str, ...]:
        discovered = run.get("coverage", {}).get("claims_discovered", 0)
        if "extract_job" not in run:
            if discovered:
                raise ValueError("claim coverage exists without an immutable claim snapshot")
            return ()
        message = JobMessage(**run["extract_job"])
        if (
            message.tenant_id,
            message.run_id,
            message.document_version_id,
            message.stage,
            message.input_hash,
        ) != (
            run["tenant_id"],
            run["run_id"],
            snapshot["document"]["version_id"],
            "extract",
            snapshot["input_hash"],
        ):
            raise ValueError("claim snapshot identity mismatch")
        job = self.runs.jobs._job(db, message)
        ref = job.get("artifact_ref")
        if job.get("status") != "succeeded" or not isinstance(ref, dict):
            raise ValueError("claim snapshot is not published")
        raw = self.runs.jobs._raw(db, run["tenant_id"], run["run_id"], "artifact", ref["key"])
        if (
            raw is None
            or sha256(raw).hexdigest() != ref.get("sha256")
            or ref.get("sha256") != run.get("claim_snapshot_sha256")
        ):
            raise ValueError("claim snapshot hash mismatch")
        try:
            envelope = json.loads(raw)
            claims = envelope["discovery"]["claims"]
        except (KeyError, TypeError, ValueError, UnicodeDecodeError) as exc:
            raise ValueError("claim snapshot is malformed") from exc
        frozen_rule = snapshot["rulepack"]["sha256"]
        if (
            envelope.get("schema")
            not in {"local_extract_checkpoint_v1", "local_extract_checkpoint_v2"}
            or envelope.get("tenant_id") != run["tenant_id"]
            or envelope.get("run_id") != run["run_id"]
            or envelope.get("document_version_id") != run["document_version_id"]
            or envelope.get("input_hash") != snapshot["input_hash"]
            or envelope.get("rule_pack_sha256") != frozen_rule
            or run.get("rule_pack_sha256", frozen_rule) != frozen_rule
            or not isinstance(claims, list)
        ):
            raise ValueError("claim snapshot pins do not match the frozen run")
        ids = []
        for item in claims:
            if not isinstance(item, dict) or not isinstance(item.get("claim_id"), str):
                raise ValueError("claim snapshot does not match coverage")
            ids.append(item["claim_id"])
        if len(ids) != discovered or len(set(ids)) != len(ids):
            raise ValueError("claim snapshot does not match coverage")
        for claim_id in ids:
            _require_uuid("claim_id", claim_id)
        return tuple(ids)

    def _decision_has_valid_lineage(
        self, db, tenant_id, run_id, claim_id, head, record, tag, snapshot
    ) -> bool:
        decision = record.get("decision", {})
        revision = head.get("decision_revision")
        if (
            record.get("decision_revision") != revision
            or decision.get("decision_revision") != revision
            or decision.get("tag_revision") != head.get("tag_revision")
        ):
            return False
        frozen_rule = snapshot["rulepack"]["sha256"]
        if decision.get("rule_pack_sha256") == frozen_rule:
            return True
        # ponytail: local receipt scan; index lineage if local volume makes this measurable.
        for artifact_id, raw in db.execute(
            "SELECT record_id,value FROM job_records WHERE tenant_id=? AND run_id=? "
            "AND kind='rescore_artifact'",
            (tenant_id, run_id),
        ):
            try:
                artifact = json.loads(raw)
                previous = artifact["previous_heads"][claim_id]
                saved = artifact["decisions"][claim_id]
                target = artifact["target_pack"]
                target_snapshot = RulePackSnapshot(**target)
                if (
                    artifact["response"]["resource_id"] == artifact_id
                    and artifact["tenant_id"] == tenant_id
                    and artifact["run_id"] == run_id
                    and artifact["execution_profile"] == self.kind
                    and artifact["original_run_snapshot_sha256"] == canonical_hash(snapshot)
                    and target_snapshot.tenant_id == tenant_id
                    and target_snapshot.mode == snapshot["rulepack"]["mode"]
                    and target_snapshot.sha256 == decision["rule_pack_sha256"]
                    and saved == record
                    and previous
                    == {
                        "tag_revision": head["tag_revision"],
                        "decision_revision": revision - 1,
                    }
                    and record["tag_record_sha256"] == canonical_hash(tag)
                    and record["input_snapshot_sha256"]
                    == (
                        canonical_hash(tag["inputs"])
                        if "inputs" in tag
                        else tag["input_snapshot_sha256"]
                    )
                ):
                    return True
            except (KeyError, TypeError, ValueError, UnicodeDecodeError):
                continue
        return False

    def get(self, tenant_id: str, run_id: str) -> dict:
        _require_uuid("tenant_id", tenant_id)
        _require_uuid("run_id", run_id)
        jobs = self.runs.jobs
        with jobs._transaction() as db:
            run = jobs._get(db, tenant_id, run_id, "run", "META")
            snapshot = self.runs._snapshot(db, tenant_id, run_id)
            projection = self.runs._project(run, snapshot)
            frozen_rule = projection["rule_pack_sha256"]
            _require_sha256("rule_pack_sha256", frozen_rule)
            decisions: list[Mapping[str, object] | None] = []
            for claim_id in self._claim_ids(db, run, snapshot):
                current = self.claims.current_tag(tenant_id, run_id, claim_id, connection=db)
                if current is None or current["decision"] is None:
                    decisions.append(None)
                    continue
                head = jobs._get(db, tenant_id, run_id, "claim_head", claim_id)
                record = jobs._get(
                    db,
                    tenant_id,
                    run_id,
                    "decision_revision",
                    f'{claim_id}:{head["decision_revision"]:010}',
                )
                if (
                    current["epoch"] != run["mutation_epoch"]
                    or record.get("api") != current["decision"]
                    or not self._decision_has_valid_lineage(
                        db,
                        tenant_id,
                        run_id,
                        claim_id,
                        head,
                        record,
                        current["tag"],
                        snapshot,
                    )
                ):
                    raise ValueError("decision head does not match the frozen run epoch")
                decisions.append(record["decision"])
            return summarize_snapshot(
                run_id=run_id,
                snapshot_epoch=run["mutation_epoch"],
                coverage=projection["coverage"],
                decisions=decisions,
                # No approved requirement-universe artifact is published locally yet.
                applicability=None,
            ).to_api_dict()
