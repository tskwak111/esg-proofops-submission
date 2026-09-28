"""Local-synthetic reconciliation cases: immutable snapshots over an existing run.

SQLite tables are `reconciliation_`-prefixed and carry their own schema version;
existing run, claim, tag, decision and export records are never written here.
Registration is a server/operator import. Imported approval assertions are
neutralised on the way in: only an authenticated admin can approve a bound
policy, and only an authenticated reviewer can promote imported bindings from
draft to confirmed. C5 never enters this store.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections.abc import Mapping
from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

from proofops.adapters.reconciliation.files import FileSourceReader, SourceReadError
from proofops.application.reconciliation.presentation import project_result
from proofops.application.reconciliation.schema import validate_schema
from proofops.application.reconciliation.service import canonical_sha256, reconcile
from proofops.application.reconciliation.sources import MAX_SOURCE_BYTES
from proofops.domain.provenance import canonical_hash
from proofops.domain.rules.engine import MAPPINGS

SCHEMA_VERSION = 1
IMMUTABLE_TABLES = (
    "reconciliation_case",
    "reconciliation_revision",
    "reconciliation_source",
    "reconciliation_idempotency",
)
BUNDLE_REQUIRED = ("packet", "policy", "documents", "artifacts")
BUNDLE_OPTIONAL = ("coverage", "policies")
REVIEW_FIELDS = (
    "reason",
    "confirm_source_bindings",
    "confirm_decision_bindings",
    "confirm_search_coverage",
)


class ReconciliationRejected(ValueError):
    """A reconciliation request cannot be served with the stated authority."""

    def __init__(self, code: str, status: int = 409) -> None:
        super().__init__(code)
        self.code, self.status = code, status


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _mapping(value: Any, code: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ReconciliationRejected(code, 422)
    return dict(value)


def _text(value: Any, code: str) -> str:
    if not isinstance(value, str) or not value:
        raise ReconciliationRejected(code, 422)
    return value


class LocalReconciliationStore:
    """Immutable local reconciliation cases; never a model, network or DART client."""

    kind = "local-synthetic-only"

    def __init__(self, database_path, artifact_root, *, run_store, claims, tags=None) -> None:
        if Path(database_path).resolve() != Path(run_store.path).resolve():
            raise ValueError("reconciliation and runs must share one local database")
        self.run_store = run_store
        self.jobs = run_store.jobs
        self.claims = claims
        # `tags` is the published tag store; without it no verified track exists.
        self.tags = tags
        self.artifact_root = Path(artifact_root)
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        with self.jobs._transaction() as db:
            self._initialize(db)

    # ------------------------------------------------------------------ #
    # schema
    # ------------------------------------------------------------------ #

    @staticmethod
    def _initialize(db: sqlite3.Connection) -> None:
        db.execute("CREATE TABLE IF NOT EXISTS reconciliation_schema (version INTEGER PRIMARY KEY)")
        versions = db.execute("SELECT version FROM reconciliation_schema").fetchall()
        if versions and versions != [(SCHEMA_VERSION,)]:
            raise ReconciliationRejected("UNSUPPORTED_RECONCILIATION_SCHEMA", 409)
        db.execute("INSERT OR IGNORE INTO reconciliation_schema VALUES (?)", (SCHEMA_VERSION,))
        db.execute("""CREATE TABLE IF NOT EXISTS reconciliation_case (
            tenant_id TEXT NOT NULL, case_id TEXT NOT NULL, run_id TEXT NOT NULL,
            claim_id TEXT NOT NULL, item TEXT NOT NULL, synthetic INTEGER NOT NULL,
            created_at TEXT NOT NULL, created_by TEXT NOT NULL, snapshot BLOB NOT NULL,
            PRIMARY KEY (tenant_id, case_id))""")
        db.execute("""CREATE TABLE IF NOT EXISTS reconciliation_head (
            tenant_id TEXT NOT NULL, case_id TEXT NOT NULL, revision INTEGER NOT NULL,
            review_state TEXT NOT NULL, policy_approved INTEGER NOT NULL,
            coverage_confirmed INTEGER NOT NULL, approval BLOB,
            latest_result_revision INTEGER, updated_at TEXT NOT NULL,
            PRIMARY KEY (tenant_id, case_id))""")
        db.execute("""CREATE TABLE IF NOT EXISTS reconciliation_revision (
            tenant_id TEXT NOT NULL, case_id TEXT NOT NULL, revision INTEGER NOT NULL,
            kind TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL,
            payload BLOB NOT NULL, payload_sha256 TEXT NOT NULL,
            PRIMARY KEY (tenant_id, case_id, revision))""")
        db.execute("""CREATE TABLE IF NOT EXISTS reconciliation_source (
            tenant_id TEXT NOT NULL, case_id TEXT NOT NULL, source_id TEXT NOT NULL,
            document_id TEXT NOT NULL, artifact_sha256 TEXT NOT NULL, locator TEXT NOT NULL,
            quote TEXT NOT NULL, format TEXT NOT NULL, stored_path TEXT NOT NULL,
            byte_size INTEGER NOT NULL,
            PRIMARY KEY (tenant_id, case_id, source_id))""")
        db.execute("""CREATE TABLE IF NOT EXISTS reconciliation_idempotency (
            tenant_id TEXT NOT NULL, case_id TEXT NOT NULL, operation TEXT NOT NULL,
            key TEXT NOT NULL, request_sha256 TEXT NOT NULL, response BLOB NOT NULL,
            PRIMARY KEY (tenant_id, case_id, operation, key))""")
        for table in IMMUTABLE_TABLES:
            for action in ("UPDATE", "DELETE"):
                db.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()}
                    BEFORE {action} ON {table}
                    BEGIN SELECT RAISE(ABORT, 'immutable reconciliation record'); END""")

    # ------------------------------------------------------------------ #
    # shared helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _require(auth, capability: str) -> None:
        capabilities = getattr(auth, "capabilities", None) or frozenset()
        if capability not in capabilities:
            raise ReconciliationRejected("FORBIDDEN", 403)

    @staticmethod
    def _tenant(auth) -> str:
        tenant = getattr(auth, "tenant_id", None)
        if not isinstance(tenant, str) or not tenant:
            raise ReconciliationRejected("FORBIDDEN", 403)
        return tenant

    @staticmethod
    def _case_row(db, tenant: str, case_id: str) -> dict[str, Any]:
        if not isinstance(case_id, str) or not case_id:
            raise ReconciliationRejected("RESOURCE_NOT_FOUND", 404)
        row = db.execute(
            "SELECT snapshot FROM reconciliation_case WHERE tenant_id=? AND case_id=?",
            (tenant, case_id),
        ).fetchone()
        if row is None:
            raise ReconciliationRejected("RESOURCE_NOT_FOUND", 404)
        return json.loads(row[0])

    @staticmethod
    def _head_row(db, tenant: str, case_id: str) -> dict[str, Any]:
        row = db.execute(
            "SELECT revision, review_state, policy_approved, coverage_confirmed, approval,"
            " latest_result_revision FROM reconciliation_head WHERE tenant_id=? AND case_id=?",
            (tenant, case_id),
        ).fetchone()
        if row is None:
            raise ReconciliationRejected("RESOURCE_NOT_FOUND", 404)
        return {
            "revision": row[0],
            "review_state": row[1],
            "policy_approved": bool(row[2]),
            "coverage_confirmed": bool(row[3]),
            "approval": json.loads(row[4]) if row[4] else None,
            "latest_result_revision": row[5],
        }

    @staticmethod
    def _expected_revision(if_match: Any) -> int:
        if if_match is None:
            raise ReconciliationRejected("IF_MATCH_REQUIRED", 400)
        if not isinstance(if_match, str):
            raise ReconciliationRejected("IF_MATCH_INVALID", 400)
        value = if_match.strip()
        if len(value) < 3 or value[0] != '"' or value[-1] != '"':
            raise ReconciliationRejected("IF_MATCH_INVALID", 400)
        digits = value[1:-1]
        if not digits.isdigit() or digits[0] == "0":
            raise ReconciliationRejected("IF_MATCH_INVALID", 400)
        return int(digits)

    @staticmethod
    def _idempotency_key(key: Any) -> str:
        if not isinstance(key, str) or not 16 <= len(key) <= 128:
            raise ReconciliationRejected("IDEMPOTENCY_KEY_INVALID", 400)
        return key

    @staticmethod
    def _replay(db, tenant, case_id, operation, key, request_sha256) -> dict[str, Any] | None:
        row = db.execute(
            "SELECT request_sha256, response FROM reconciliation_idempotency"
            " WHERE tenant_id=? AND case_id=? AND operation=? AND key=?",
            (tenant, case_id, operation, key),
        ).fetchone()
        if row is None:
            return None
        if row[0] != request_sha256:
            raise ReconciliationRejected("IDEMPOTENCY_CONFLICT", 409)
        return json.loads(row[1])

    def _sources(self, db, tenant: str, case_id: str) -> dict[str, dict[str, Any]]:
        rows = db.execute(
            "SELECT source_id, document_id, artifact_sha256, locator, quote, format,"
            " stored_path, byte_size FROM reconciliation_source WHERE tenant_id=? AND case_id=?",
            (tenant, case_id),
        ).fetchall()
        return {
            row[0]: {
                "source_id": row[0],
                "document_id": row[1],
                "artifact_sha256": row[2],
                "locator": row[3],
                "quote": row[4],
                "format": row[5],
                "stored_path": row[6],
                "byte_size": row[7],
            }
            for row in rows
        }

    def _case_root(self, tenant: str, case_id: str) -> Path:
        return self.artifact_root / tenant / case_id

    def managed_path(self, tenant: str, case_id: str, source_id: str) -> Path:
        """Filesystem location of the managed original behind one source reference."""
        with self.jobs._transaction() as db:
            sources = self._sources(db, tenant, case_id)
        source = sources.get(source_id)
        if source is None:
            raise ReconciliationRejected("RESOURCE_NOT_FOUND", 404)
        return self._case_root(tenant, case_id) / source["stored_path"]

    # ------------------------------------------------------------------ #
    # registration
    # ------------------------------------------------------------------ #

    def register_case(self, auth, run_id, claim_id, bundle, artifact_root) -> dict[str, Any]:
        """Import a prepared draft for an existing verified claim. Server-side only.

        Never expose this as an HTTP handler: it reads operator-controlled local
        files and trusts the caller's identity as an audited local operator.
        """
        self._require(auth, "admin")
        tenant = self._tenant(auth)
        packet, policy, documents, artifacts, coverage = self._validate_bundle(bundle)
        anchor = self._verified_claim(tenant, run_id, claim_id, packet, policy)

        case_id = str(uuid4())
        created_at, actor = _now(), _text(getattr(auth, "user_sub", None), "FORBIDDEN")
        imported = self._import_artifacts(tenant, case_id, artifacts, packet, Path(artifact_root))
        provenance = {
            "tenant_id": tenant,
            "run_id": run_id,
            "claim_id": claim_id,
            "document_version_id": anchor["document_version_id"],
            "company_id": anchor["company_id"],
            "tag_revision": anchor["tag_revision"],
            "mutation_epoch": anchor["mutation_epoch"],
            "claim_source_quality": anchor["source_quality"],
            "claim_track": anchor["track"],
            "imported_by": actor,
            "imported_at": created_at,
            "packet_sha256": canonical_sha256(packet),
            "policy_sha256": canonical_sha256(policy),
            "documents_sha256": canonical_sha256(documents),
            "artifacts_sha256": canonical_sha256(artifacts),
        }
        snapshot = {
            "case_id": case_id,
            "run_id": run_id,
            "claim_id": claim_id,
            "item": packet["item"],
            "synthetic": anchor["synthetic"],
            "packet": packet,
            "policy": policy,
            "documents": documents,
            "artifacts": artifacts,
            "coverage": coverage,
            "provenance": provenance,
        }
        try:
            with self.jobs._transaction() as db:
                db.execute(
                    "INSERT INTO reconciliation_case VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        tenant,
                        case_id,
                        run_id,
                        claim_id,
                        packet["item"],
                        int(anchor["synthetic"]),
                        created_at,
                        actor,
                        _encode(snapshot),
                    ),
                )
                for source in imported:
                    db.execute(
                        "INSERT INTO reconciliation_source VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (
                            tenant,
                            case_id,
                            source["source_id"],
                            source["document_id"],
                            source["artifact_sha256"],
                            source["locator"],
                            source["quote"],
                            source["format"],
                            source["stored_path"],
                            source["byte_size"],
                        ),
                    )
                self._append(
                    db,
                    tenant,
                    case_id,
                    1,
                    "registration",
                    actor,
                    created_at,
                    {"provenance": provenance, "event": {"kind": "registration", "actor": actor}},
                )
                db.execute(
                    "INSERT INTO reconciliation_head VALUES (?,?,?,?,?,?,?,?,?)",
                    (tenant, case_id, 1, "pending", 0, 0, None, None, created_at),
                )
                return self._detail(db, tenant, case_id)
        except sqlite3.DatabaseError as error:
            raise ReconciliationRejected("RECONCILIATION_CONFLICT", 409) from error

    def _validate_bundle(self, bundle) -> tuple[dict, dict, dict, dict, dict]:
        supplied = _mapping(bundle, "VALIDATION_ERROR")
        if not set(BUNDLE_REQUIRED) <= set(supplied) <= set(BUNDLE_REQUIRED + BUNDLE_OPTIONAL):
            raise ReconciliationRejected("VALIDATION_ERROR", 422)
        packet = deepcopy(_mapping(supplied["packet"], "VALIDATION_ERROR"))
        policy = deepcopy(_mapping(supplied["policy"], "VALIDATION_ERROR"))
        documents = deepcopy(_mapping(supplied["documents"], "VALIDATION_ERROR"))
        artifacts = deepcopy(_mapping(supplied["artifacts"], "VALIDATION_ERROR"))
        coverage = deepcopy(_mapping(supplied.get("coverage") or {}, "VALIDATION_ERROR"))
        if packet.get("item") == "C5":
            raise ReconciliationRejected("C5_DISABLED", 422)
        try:
            validate_schema("input", packet)
            validate_schema("policy", policy)
        except Exception as error:
            raise ReconciliationRejected("VALIDATION_ERROR", 422) from error
        # Imported approval assertions never carry authority into this store.
        policy["approved"], policy["approved_by"], policy["approved_on"] = False, None, None
        if not documents or not artifacts:
            raise ReconciliationRejected("VALIDATION_ERROR", 422)
        for document_id in {source["document_id"] for source in packet["sources"]}:
            if document_id not in documents or document_id not in artifacts:
                raise ReconciliationRejected("DOCUMENT_BINDING_MISSING", 422)
        return packet, policy, documents, artifacts, coverage

    def _verified_claim(self, tenant, run_id, claim_id, packet, policy) -> dict[str, Any]:
        """Bind the draft to a claim that a real extraction actually produced.

        `claims.get` replays the run's evidence, so a hand-written claim head or
        tag row cannot pass: the snapshot identity, checkpoint hash and pinned
        inputs must all agree before a claim exists at all.
        """
        try:
            run_snapshot = self.run_store.snapshot(tenant, run_id)
            claim = self.claims.get(tenant, run_id, claim_id)
        except (KeyError, ValueError) as error:
            raise ReconciliationRejected("RESOURCE_NOT_FOUND", 404) from error
        if getattr(claim, "tenant_id", None) != tenant:
            raise ReconciliationRejected("RESOURCE_NOT_FOUND", 404)
        if getattr(claim, "source_quality", None) != "verified":
            raise ReconciliationRejected("CLAIM_NOT_VERIFIED", 422)
        try:
            with self.jobs._transaction() as db:
                run = self.jobs._get(db, tenant, run_id, "run", "META")
                head = self._reviewed_head(db, tenant, run_id, claim)
        except ReconciliationRejected:
            raise
        except (KeyError, ValueError) as error:
            raise ReconciliationRejected("RESOURCE_NOT_FOUND", 404) from error
        track = head["track"]

        document = run_snapshot["document"]
        identity = packet["identity"]
        expected = {
            "tenant_id": tenant,
            "claim_id": claim_id,
            "company_id": document["company"]["company_id"],
            "sustainability_document_version": claim.document_version_id,
        }
        if any(identity.get(key) != value for key, value in expected.items()):
            raise ReconciliationRejected("CASE_IDENTITY_MISMATCH", 422)
        if packet["claim"]["quote"] != claim.quote:
            raise ReconciliationRejected("CLAIM_BINDING_MISMATCH", 422)
        if packet["claim"]["track"] != track:
            raise ReconciliationRejected("CLAIM_BINDING_MISMATCH", 422)

        # Synthetic is a property of the anchored document, never of the draft.
        synthetic = document.get("local_synthetic") is True
        if packet["synthetic"] is not synthetic:
            raise ReconciliationRejected("SYNTHETIC_SCOPE_MISMATCH", 422)
        if policy["synthetic_only"] and not synthetic:
            raise ReconciliationRejected("SYNTHETIC_SCOPE_MISMATCH", 422)
        return {
            "document_version_id": claim.document_version_id,
            "company_id": expected["company_id"],
            "tag_revision": head["tag_revision"],
            "mutation_epoch": run["mutation_epoch"],
            "source_quality": claim.source_quality,
            "track": track,
            "synthetic": synthetic,
        }

    def _reviewed_head(self, db, tenant, run_id, claim) -> dict[str, Any]:
        """Revision AND track of the CURRENT accepted tag head, from one atomic read.

        The revision authority is the atomic `claim_head`, read in the caller's
        transaction -- never `history()[-1]`, and never the head row's own
        `tag_revision`, which a tampered row can make agree with itself.

        The track comes from the SAME head. This replaces a separate
        `tags.load_inputs(...).packet` read, which always returned the ORIGINAL
        tag-stage packet: accepted reviews write new immutable revisions and move
        `claim_head` while that published packet is never rewritten, so a claim a
        real review moved to another track kept anchoring cases on the track it
        had been moved off.

        A confirmed head must carry the canonical `ConfirmedTags` identity in
        full; missing, foreign or empty is blocked, never accepted or fallen back
        on. Only the ORIGINAL unconfirmed revision 1 may use its own pinned
        preliminary packet, so the no-review behaviour is unchanged and a
        reviewed head can never reach a superseded revision's track. The
        `input_snapshot_sha256` pin `export_store` enforces is kept here too,
        since dropping `load_inputs` dropped its validation.
        """
        if self.tags is None:
            # Preserved precondition: with no published tag store there is no
            # tagging evidence to anchor a case against at all.
            raise ReconciliationRejected("TAG_INPUTS_UNAVAILABLE", 409)
        current = self.claims.current_tag(tenant, run_id, claim.claim_id, connection=db)
        if current is None:
            raise ReconciliationRejected("CLAIM_NOT_VERIFIED", 422)
        revision = self.jobs._get(db, tenant, run_id, "claim_head", claim.claim_id)["tag_revision"]
        tag = current["tag"]
        if not isinstance(tag, Mapping) or tag.get("tag_revision") != revision:
            raise ReconciliationRejected("TAG_HEAD_UNACCOUNTABLE", 409)

        # A reviewed head keeps no `inputs` of its own; its snapshot hash must
        # still match the immutable revision-1 inputs.
        original = self.jobs._raw(db, tenant, run_id, "tag_revision", f"{claim.claim_id}:{1:010}")
        inputs = tag.get("inputs")
        if inputs is None and original is not None:
            inputs = json.loads(original).get("inputs")
        snapshot_sha256 = tag.get("input_snapshot_sha256")
        if snapshot_sha256 is not None and (
            inputs is None or snapshot_sha256 != canonical_hash(inputs)
        ):
            raise ReconciliationRejected("TAG_HEAD_UNACCOUNTABLE", 409)

        confirmed = tag.get("confirmed_tags")
        if confirmed is None:
            if revision != 1:
                raise ReconciliationRejected("TAG_HEAD_UNACCOUNTABLE", 409)
            packet = (tag.get("inputs") or {}).get("packet")
            track = packet.get("track") if isinstance(packet, Mapping) else None
        else:
            if not isinstance(confirmed, Mapping) or (
                confirmed.get("tenant_id"),
                confirmed.get("document_version_id"),
                confirmed.get("claim_id"),
                confirmed.get("tag_revision"),
            ) != (tenant, claim.document_version_id, claim.claim_id, revision):
                raise ReconciliationRejected("TAG_HEAD_UNACCOUNTABLE", 409)
            track = confirmed.get("track")
        if not isinstance(track, str) or track not in MAPPINGS:
            raise ReconciliationRejected("CLAIM_NOT_VERIFIED", 422)
        return {"tag_revision": revision, "track": track}

    def _import_artifacts(self, tenant, case_id, artifacts, packet, source_root) -> list[dict]:
        """Copy operator originals into the managed store, then prove every locator.

        Only bytes this method has already hashed are written, and they are
        written with an exclusive create, so the managed copy can never be a
        re-read of a file that changed underneath the check.
        """
        root = Path(source_root).resolve()
        destination_root = self._case_root(tenant, case_id)
        stored: dict[str, dict[str, Any]] = {}
        try:
            for document_id, entry in artifacts.items():
                manifest = _mapping(entry, "VALIDATION_ERROR")
                relative = _text(manifest.get("path"), "VALIDATION_ERROR")
                if Path(relative).is_absolute() or ":" in relative or ".." in Path(relative).parts:
                    raise ReconciliationRejected("ARTIFACT_PATH_INVALID", 422)
                if manifest.get("format") not in {"text", "xml", "html", "pdf"}:
                    raise ReconciliationRejected("ARTIFACT_FORMAT_UNSUPPORTED", 422)
                origin = (root / relative).resolve()
                if not origin.is_relative_to(root) or not origin.is_file():
                    raise ReconciliationRejected("ARTIFACT_UNVERIFIED", 422)
                if origin.stat().st_size > MAX_SOURCE_BYTES:
                    raise ReconciliationRejected("ARTIFACT_TOO_LARGE", 422)
                payload = origin.read_bytes()
                if len(payload) > MAX_SOURCE_BYTES:
                    raise ReconciliationRejected("ARTIFACT_TOO_LARGE", 422)
                digest = sha256(payload).hexdigest()
                if digest != manifest.get("sha256"):
                    raise ReconciliationRejected("ARTIFACT_UNVERIFIED", 422)
                destination = destination_root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("xb") as stream:
                    stream.write(payload)
                stored[document_id] = {
                    "stored_path": relative,
                    "format": manifest["format"],
                    "sha256": digest,
                    "byte_size": len(payload),
                }
            reader = FileSourceReader(
                destination_root,
                {
                    document_id: {
                        "path": entry["stored_path"],
                        "format": entry["format"],
                        "sha256": entry["sha256"],
                    }
                    for document_id, entry in stored.items()
                },
            )
            imported = []
            for source in packet["sources"]:
                entry = stored.get(source["document_id"])
                if entry is None or entry["sha256"] != source["artifact_sha256"]:
                    raise ReconciliationRejected("ARTIFACT_UNVERIFIED", 422)
                # A matching hash is not a matching quote: prove the locator too.
                try:
                    reader.validate(source)
                except (SourceReadError, OSError, ValueError) as error:
                    raise ReconciliationRejected("SOURCE_LOCATOR_UNVERIFIED", 422) from error
                imported.append({**source, **entry})
            return imported
        except BaseException:
            shutil.rmtree(destination_root, ignore_errors=True)
            raise

    # ------------------------------------------------------------------ #
    # reads
    # ------------------------------------------------------------------ #

    def list_for_claim(self, auth, run_id, claim_id) -> dict[str, Any]:
        self._require(auth, "viewer")
        tenant = self._tenant(auth)
        with self.jobs._transaction() as db:
            rows = db.execute(
                "SELECT case_id FROM reconciliation_case WHERE tenant_id=? AND run_id=?"
                " AND claim_id=? ORDER BY created_at, case_id",
                (tenant, run_id, claim_id),
            ).fetchall()
            return {"items": [self._detail(db, tenant, row[0]) for row in rows]}

    def get_case(self, auth, case_id) -> dict[str, Any]:
        self._require(auth, "viewer")
        tenant = self._tenant(auth)
        with self.jobs._transaction() as db:
            return self._detail(db, tenant, case_id)

    def revision(self, auth, case_id, revision) -> dict[str, Any]:
        self._require(auth, "viewer")
        tenant = self._tenant(auth)
        with self.jobs._transaction() as db:
            self._case_row(db, tenant, case_id)
            row = db.execute(
                "SELECT kind, actor, created_at, payload, payload_sha256 FROM"
                " reconciliation_revision WHERE tenant_id=? AND case_id=? AND revision=?",
                (tenant, case_id, revision),
            ).fetchone()
        if row is None:
            raise ReconciliationRejected("RESOURCE_NOT_FOUND", 404)
        payload = json.loads(row[3])
        return {
            "case_id": case_id,
            "revision": revision,
            "kind": row[0],
            "actor": row[1],
            "created_at": row[2],
            "snapshot_sha256": row[4],
            "provenance": payload.get("provenance"),
            "event": payload.get("event"),
            "result": payload.get("result"),
            "projection": payload.get("projection"),
        }

    def source_content(self, auth, case_id, source_id) -> tuple[bytes, str, str]:
        self._require(auth, "viewer")
        tenant = self._tenant(auth)
        with self.jobs._transaction() as db:
            self._case_row(db, tenant, case_id)
            sources = self._sources(db, tenant, case_id)
        source = sources.get(source_id)
        if source is None:
            raise ReconciliationRejected("RESOURCE_NOT_FOUND", 404)
        path = self._case_root(tenant, case_id) / source["stored_path"]
        try:
            payload = path.read_bytes()
        except OSError as error:
            raise ReconciliationRejected("SOURCE_UNVERIFIED", 409) from error
        digest = sha256(payload).hexdigest()
        if digest != source["artifact_sha256"]:
            raise ReconciliationRejected("SOURCE_UNVERIFIED", 409)
        return payload, f"{source['document_id']}.{source['format']}", digest

    def _detail(self, db, tenant: str, case_id: str) -> dict[str, Any]:
        snapshot = self._case_row(db, tenant, case_id)
        head = self._head_row(db, tenant, case_id)
        sources = self._sources(db, tenant, case_id)
        confirmed = head["review_state"] == "reviewed"
        latest = None
        if head["latest_result_revision"] is not None:
            row = db.execute(
                "SELECT payload, created_at FROM reconciliation_revision"
                " WHERE tenant_id=? AND case_id=? AND revision=?",
                (tenant, case_id, head["latest_result_revision"]),
            ).fetchone()
            if row is not None:
                payload = json.loads(row[0])
                latest = {
                    "revision": head["latest_result_revision"],
                    "result": payload.get("result"),
                    "projection": payload.get("projection"),
                    "created_at": row[1],
                }
        return {
            "case_id": case_id,
            "run_id": snapshot["run_id"],
            "claim_id": snapshot["claim_id"],
            "item": snapshot["item"],
            "revision": head["revision"],
            "synthetic": snapshot["synthetic"],
            "review_state": head["review_state"],
            "policy_approved": head["policy_approved"],
            "packet": snapshot["packet"],
            "policy": snapshot["policy"],
            "sources": [
                {
                    "source_id": source["source_id"],
                    "document_id": source["document_id"],
                    "artifact_sha256": source["artifact_sha256"],
                    "locator": source["locator"],
                    "quote": source["quote"],
                    "format": source["format"],
                    "byte_size": source["byte_size"],
                    "binding_state": "confirmed" if confirmed else "draft",
                    "document_role": (
                        snapshot["documents"].get(source["document_id"], {}).get("document_role")
                    ),
                }
                for source in sorted(sources.values(), key=lambda value: value["source_id"])
            ],
            "latest_result": latest,
            "provenance": snapshot["provenance"],
            "coverage_confirmed": head["coverage_confirmed"],
            "policy_approval": head["approval"],
        }

    # ------------------------------------------------------------------ #
    # mutations
    # ------------------------------------------------------------------ #

    def _append(self, db, tenant, case_id, revision, kind, actor, created_at, payload) -> None:
        encoded = _encode(payload)
        db.execute(
            "INSERT INTO reconciliation_revision VALUES (?,?,?,?,?,?,?,?)",
            (
                tenant,
                case_id,
                revision,
                kind,
                actor,
                created_at,
                encoded,
                sha256(encoded).hexdigest(),
            ),
        )

    def _mutate(self, auth, case_id, operation, capability, if_match, key, request, apply):
        """One compare-and-set head advance with replay, inside a single transaction."""
        self._require(auth, capability)
        tenant = self._tenant(auth)
        key = self._idempotency_key(key)
        expected = self._expected_revision(if_match)
        request_sha256 = canonical_sha256({"operation": operation, "request": request})
        actor = _text(getattr(auth, "user_sub", None), "FORBIDDEN")
        try:
            with self.jobs._transaction() as db:
                self._case_row(db, tenant, case_id)
                replayed = self._replay(db, tenant, case_id, operation, key, request_sha256)
                if replayed is not None:
                    return replayed
                head = self._head_row(db, tenant, case_id)
                if head["revision"] != expected:
                    raise ReconciliationRejected("REVISION_CONFLICT", 409)
                revision, created_at = head["revision"] + 1, _now()
                changes = apply(db, tenant, case_id, head, revision, actor, created_at)
                db.execute(
                    "UPDATE reconciliation_head SET revision=?, review_state=?, policy_approved=?,"
                    " coverage_confirmed=?, approval=?, latest_result_revision=?, updated_at=?"
                    " WHERE tenant_id=? AND case_id=? AND revision=?",
                    (
                        revision,
                        changes["review_state"],
                        int(changes["policy_approved"]),
                        int(changes["coverage_confirmed"]),
                        _encode(changes["approval"]) if changes["approval"] else None,
                        changes["latest_result_revision"],
                        created_at,
                        tenant,
                        case_id,
                        expected,
                    ),
                )
                detail = self._detail(db, tenant, case_id)
                db.execute(
                    "INSERT INTO reconciliation_idempotency VALUES (?,?,?,?,?,?)",
                    (tenant, case_id, operation, key, request_sha256, _encode(detail)),
                )
                return detail
        except sqlite3.DatabaseError as error:
            raise ReconciliationRejected("REVISION_CONFLICT", 409) from error

    def review(self, auth, case_id, body, if_match, idempotency_key) -> dict[str, Any]:
        request = self._validate_review(body)

        def apply(db, tenant, case_id, head, revision, actor, created_at):
            snapshot = self._case_row(db, tenant, case_id)
            event = {
                "kind": "review",
                "actor": actor,
                "created_at": created_at,
                "reason": request["reason"],
                "confirm_source_bindings": True,
                "confirm_decision_bindings": True,
                "confirm_search_coverage": request["confirm_search_coverage"],
                "documents_sha256": canonical_sha256(snapshot["documents"]),
                "packet_sha256": canonical_sha256(snapshot["packet"]),
                "coverage_sha256": canonical_sha256(snapshot["coverage"]),
            }
            self._append(
                db, tenant, case_id, revision, "review", actor, created_at, {"event": event}
            )
            return {
                "review_state": "reviewed",
                "policy_approved": head["policy_approved"],
                "coverage_confirmed": request["confirm_search_coverage"],
                "approval": head["approval"],
                "latest_result_revision": head["latest_result_revision"],
            }

        return self._mutate(
            auth, case_id, "review", "reviewer", if_match, idempotency_key, request, apply
        )

    @staticmethod
    def _validate_review(body) -> dict[str, Any]:
        request = _mapping(body, "VALIDATION_ERROR")
        if set(request) != set(REVIEW_FIELDS):
            raise ReconciliationRejected("VALIDATION_ERROR", 422)
        reason = request["reason"]
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 2000:
            raise ReconciliationRejected("VALIDATION_ERROR", 422)
        for field in ("confirm_source_bindings", "confirm_decision_bindings"):
            if request[field] is not True:
                raise ReconciliationRejected("VALIDATION_ERROR", 422)
        if type(request["confirm_search_coverage"]) is not bool:
            raise ReconciliationRejected("VALIDATION_ERROR", 422)
        return {
            "reason": reason.strip(),
            "confirm_source_bindings": True,
            "confirm_decision_bindings": True,
            "confirm_search_coverage": request["confirm_search_coverage"],
        }

    def approve_policy(self, auth, case_id, body, if_match, idempotency_key) -> dict[str, Any]:
        request = self._validate_approval(body)

        def apply(db, tenant, case_id, head, revision, actor, created_at):
            snapshot = self._case_row(db, tenant, case_id)
            policy_sha256 = canonical_sha256(snapshot["policy"])
            event = {
                "kind": "policy_approval",
                "actor": actor,
                "created_at": created_at,
                "approved": request["approved"],
                "reason": request["reason"],
                "policy_sha256": policy_sha256,
                "policy_version": snapshot["policy"]["version"],
                "source_policy_sha256": snapshot["policy"]["source_policy_sha256"],
                "synthetic_only": snapshot["policy"]["synthetic_only"],
            }
            self._append(
                db,
                tenant,
                case_id,
                revision,
                "policy_approval",
                actor,
                created_at,
                {"event": event},
            )
            return {
                "review_state": head["review_state"],
                "policy_approved": request["approved"],
                "coverage_confirmed": head["coverage_confirmed"],
                "approval": event if request["approved"] else None,
                "latest_result_revision": head["latest_result_revision"],
            }

        return self._mutate(
            auth, case_id, "policy_approval", "admin", if_match, idempotency_key, request, apply
        )

    @staticmethod
    def _validate_approval(body) -> dict[str, Any]:
        request = _mapping(body, "VALIDATION_ERROR")
        if set(request) != {"approved", "reason"}:
            raise ReconciliationRejected("VALIDATION_ERROR", 422)
        if type(request["approved"]) is not bool:
            raise ReconciliationRejected("VALIDATION_ERROR", 422)
        reason = request["reason"]
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 2000:
            raise ReconciliationRejected("VALIDATION_ERROR", 422)
        return {"approved": request["approved"], "reason": reason.strip()}

    def evaluate(self, auth, case_id, body, if_match, idempotency_key) -> dict[str, Any]:
        if _mapping(body, "VALIDATION_ERROR"):
            raise ReconciliationRejected("VALIDATION_ERROR", 422)

        def apply(db, tenant, case_id, head, revision, actor, created_at):
            snapshot = self._case_row(db, tenant, case_id)
            result = self._reconcile(tenant, case_id, snapshot, head)
            projection = project_result(result, snapshot["packet"], snapshot["documents"])
            self._append(
                db,
                tenant,
                case_id,
                revision,
                "evaluation",
                actor,
                created_at,
                {
                    "result": result,
                    "projection": projection,
                    "provenance": dict(
                        snapshot["provenance"],
                        evaluated_by=actor,
                        evaluated_at=created_at,
                        review_state=head["review_state"],
                        policy_approved=head["policy_approved"],
                    ),
                    "event": {"kind": "evaluation", "actor": actor, "created_at": created_at},
                },
            )
            return {
                "review_state": head["review_state"],
                "policy_approved": head["policy_approved"],
                "coverage_confirmed": head["coverage_confirmed"],
                "approval": head["approval"],
                "latest_result_revision": revision,
            }

        return self._mutate(
            auth, case_id, "evaluation", "editor", if_match, idempotency_key, {}, apply
        )

    def _reconcile(self, tenant, case_id, snapshot, head) -> dict[str, Any]:
        """Evaluate server-owned snapshots. Unconfirmed bindings are simply absent."""
        confirmed = head["review_state"] == "reviewed"
        policy = snapshot["policy"]
        policy_registry = None
        if head["policy_approved"] and head["approval"]:
            approval = head["approval"]
            policy_registry = {
                canonical_sha256(policy): {
                    "approved": True,
                    "approved_by": approval["actor"],
                    "approved_on": approval["created_at"][:10],
                    "version": policy["version"],
                    "source_policy_sha256": policy["source_policy_sha256"],
                    "synthetic_only": policy["synthetic_only"],
                }
            }
        artifacts = {
            document_id: {
                "path": entry["path"],
                "format": entry["format"],
                "sha256": entry["sha256"],
            }
            for document_id, entry in snapshot["artifacts"].items()
        }
        try:
            reader = FileSourceReader(self._case_root(tenant, case_id), artifacts)
        except Exception:
            reader = _unreadable
        return reconcile(
            deepcopy(snapshot["packet"]),
            deepcopy(policy),
            source_reader=reader,
            explanation_search=_no_candidates,
            policy_registry=policy_registry,
            coverage_registry=deepcopy(snapshot["coverage"])
            if confirmed and head["coverage_confirmed"]
            else None,
            document_registry=deepcopy(snapshot["documents"]) if confirmed else None,
        )


def _encode(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _no_candidates(_packet: Mapping[str, Any]) -> list[dict[str, Any]]:
    """This store performs no extraction; candidates arrive only by operator import."""
    return []


def _unreadable(_ref: Mapping[str, Any]) -> bytes:
    raise ReconciliationRejected("SOURCE_UNVERIFIED", 409)
