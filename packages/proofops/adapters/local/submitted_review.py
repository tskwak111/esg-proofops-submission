"""Local CSV references, never ConfirmedTags or evidence admission.

Contract v1: additive immutable `submission_reference` job records, scoped to
tenant/run/document/source/extraction hashes. ClaimDetail adds submitted_reviews
(empty for old runs); exports freeze the same references in a separate JSON member.
No table migration or change to existing revisions. Rollback disables import and
ignores the additive field/kind; retain stored references and frozen exports.
Only a trusted local operator can attach; there is no HTTP write endpoint.
"""

import csv
import json
import time
from hashlib import sha256
from io import StringIO
from pathlib import Path
from uuid import uuid4

from proofops.adapters.local.audit_store import append_audit_transaction, read_audit_head
from proofops.application.exports import timestamp
from proofops.domain.audit import ChangeSet
from proofops.domain.provenance import canonical_hash

TABLES = ("claims", "elements", "numeric", "assurance")
ORIGINS = ("data_manager_submission", "ai_corrected_submission")


def _read_tables(directory):
    tables, hashes = {}, {}
    for name in TABLES:
        path = Path(directory) / f"{name}.csv"
        # ponytail: bounded in-memory CSV import; stream if submissions exceed 1 MiB/file.
        with path.open("rb") as file:
            raw = file.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("SUBMISSION_SIZE_LIMIT")
        reader = csv.DictReader(StringIO(raw.decode("utf-8-sig"), newline=""))
        fields = reader.fieldnames or []
        required = {"claim_id"}
        if name == "claims":
            required |= {"document_id", "physical_page", "quote"}
        if not required <= set(fields) or len(set(fields)) != len(fields):
            raise ValueError("SUBMISSION_COLUMNS_INVALID")
        rows = list(reader)
        if len(rows) > 1000 or any(
            set(row) != set(fields)
            or any(not isinstance(v, str) or len(v) > 20000 for v in row.values())
            or not row["claim_id"].strip()
            for row in rows
        ):
            raise ValueError("SUBMISSION_ROWS_INVALID")
        tables[name], hashes[f"{name}.csv"] = rows, sha256(raw).hexdigest()
    return tables, hashes


def attach_submission(
    claims, *, tenant_id, run_id, directory, document_id, source_sha256, origin, operator
):
    """Append an all-or-nothing reference batch after unique literal/page matching.

    Matching identifies the existing candidate; it does not verify its source or
    promote any annotation state. CSV states and authors are preserved as submitted.
    """
    if origin not in ORIGINS or not isinstance(operator, str) or not operator.strip():
        raise ValueError("SUBMISSION_ORIGIN_REQUIRED")
    snapshot = claims.store.snapshot(tenant_id, run_id)
    if source_sha256 != snapshot["document"]["sha256"]:
        raise ValueError("SUBMISSION_SOURCE_HASH_MISMATCH")
    tables, hashes = _read_tables(directory)
    external_ids = [row["claim_id"] for row in tables["claims"]]
    if not external_ids or len(set(external_ids)) != len(external_ids):
        raise ValueError("SUBMISSION_CLAIM_IDS_INVALID")
    if any(row["document_id"] != document_id for row in tables["claims"]) or any(
        row["claim_id"] not in external_ids
        or any(
            row.get(field) not in (None, "", document_id)
            for field in ("document_id", "evidence_document_id")
        )
        for rows in tables.values()
        for row in rows
    ):
        raise ValueError("SUBMISSION_DOCUMENT_OR_CLAIM_MISMATCH")
    run = claims.store.jobs.get_run(tenant_id, run_id)
    candidates = claims.list(tenant_id, run_id)
    references, matched = [], set()
    for row in tables["claims"]:
        matches = [
            c
            for c in candidates
            if row["quote"].strip()
            and row["quote"] in c.quote
            and any(str(ref.page_num) == row["physical_page"] for ref in c.source_refs)
        ]
        if len(matches) != 1 or matches[0].claim_id in matched:
            raise ValueError(f"SUBMISSION_CLAIM_MATCH_REQUIRED:{row['claim_id']}:{len(matches)}")
        claim = matches[0]
        matched.add(claim.claim_id)
        references.append(
            dict(
                schema_version=1,
                status="reference_only",
                origin=origin,
                tenant_id=tenant_id,
                run_id=run_id,
                document_version_id=claim.document_version_id,
                claim_id=claim.claim_id,
                external_claim_id=row["claim_id"],
                source_sha256=source_sha256,
                run_input_sha256=snapshot["input_hash"],
                claim_snapshot_sha256=run["claim_snapshot_sha256"],
                claim_source_quality=claim.source_quality,
                files_sha256=hashes,
                tables={
                    name: [r for r in rows if r["claim_id"] == row["claim_id"]]
                    for name, rows in tables.items()
                },
            )
        )
    jobs = claims.store.jobs
    with jobs._transaction() as db:
        current = jobs._get(db, tenant_id, run_id, "run", "META")
        if current["claim_snapshot_sha256"] != run["claim_snapshot_sha256"]:
            raise ValueError("SUBMISSION_EXTRACTION_CHANGED")
        for action in ("UPDATE", "DELETE"):
            db.execute(f"""CREATE TRIGGER IF NOT EXISTS submission_no_{action.lower()}
                BEFORE {action} ON job_records WHEN OLD.kind='submission_reference'
                BEGIN SELECT RAISE(ABORT, 'immutable submission reference'); END""")
        db.execute("""CREATE TRIGGER IF NOT EXISTS submission_no_replace
            BEFORE INSERT ON job_records WHEN NEW.kind='submission_reference' AND EXISTS (
            SELECT 1 FROM job_records WHERE tenant_id=NEW.tenant_id AND run_id=NEW.run_id
            AND kind=NEW.kind AND record_id=NEW.record_id)
            BEGIN SELECT RAISE(ABORT, 'submission reference exists'); END""")
        added = False
        for reference in references:
            key = f"{reference['claim_id']}:{canonical_hash(reference)}"
            existing = jobs._raw(db, tenant_id, run_id, "submission_reference", key)
            if existing is not None:
                if json.loads(existing) != reference:
                    raise ValueError("SUBMISSION_INTEGRITY_FAILED")
                continue
            jobs._put(db, tenant_id, run_id, "submission_reference", key, reference, immutable=True)
            added = True
        if added:
            current["mutation_epoch"] += 1
            jobs._put(db, tenant_id, run_id, "run", "META", current)
            append_audit_transaction(
                connection=db,
                change=ChangeSet(
                    tenant_id,
                    run_id,
                    operator,
                    "submission_reference_attached",
                    run_id,
                    None,
                    canonical_hash(references),
                    current["mutation_epoch"],
                    "Reference only; no tagging or grading",
                ),
                expected_head=read_audit_head(db, tenant_id, run_id),
                event_id=str(uuid4()),
                timestamp=timestamp(time.time()),
            )
    return [r | {"reference_sha256": canonical_hash(r)} for r in references]


def read_submissions(claims, tenant_id, run_id, claim_id=None, *, connection=None):
    if connection is None:
        with claims.store.jobs._transaction() as db:
            return read_submissions(claims, tenant_id, run_id, claim_id, connection=db)
    jobs = claims.store.jobs
    jobs._get(connection, tenant_id, run_id, "run", "META")
    snapshot = claims.store._snapshot(connection, tenant_id, run_id)
    rows = connection.execute(
        "SELECT record_id,value FROM job_records WHERE tenant_id=? AND run_id=? "
        "AND kind='submission_reference' ORDER BY record_id",
        (tenant_id, run_id),
    )
    result = []
    for key, raw in rows:
        ref = json.loads(raw)
        digest = canonical_hash(ref)
        if key != f"{ref['claim_id']}:{digest}" or (
            ref["tenant_id"],
            ref["run_id"],
            ref["document_version_id"],
            ref["source_sha256"],
            ref["run_input_sha256"],
        ) != (
            tenant_id,
            run_id,
            snapshot["document"]["version_id"],
            snapshot["document"]["sha256"],
            snapshot["input_hash"],
        ):
            raise ValueError("SUBMISSION_INTEGRITY_FAILED")
        if claim_id is None or ref["claim_id"] == claim_id:
            result.append(ref | {"reference_sha256": digest})
    return result
