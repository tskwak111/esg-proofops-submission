"""Authenticated hosted inventory and immutable original-page read adapter."""

from __future__ import annotations

import base64
import fcntl
import hmac
import json
import math
import sqlite3
import time
from collections import OrderedDict
from contextlib import ExitStack, closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from uuid import UUID, uuid5

from proofops.adapters.local.job_store import LocalSQLiteJobStore
from proofops.domain.documents import PageGeometry
from proofops.domain.provenance import canonical_hash

from proofops_api.hosted_page_render import render
from proofops_api.hosted_store import (
    HostedError,
    copy_verified_file,
    digest,
    file_sha256,
    identifier,
)

_CACHE_LIMIT = 32 * 1024 * 1024
_cache: OrderedDict[tuple, tuple[float, bytes, dict]] = OrderedDict()
_cache_lock = Lock()
_render_lock = Lock()


def _ops2_fit_page(options):
    from proofops.application.analysis_profile import policy_for

    # Read the persisted pin; never apply today's default to an older run.
    pin = options.get("analysis_profile_sha256")
    if pin is None:
        return False
    try:
        policy = policy_for(options.get("analysis_profile"), pinned_hash=pin)
    except ValueError:
        return False
    return policy.get("ops2_page_render") == "ops2_fit_page_v1"


def clear_source_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _cached(key, expires, now, value=None):
    with _cache_lock:
        for expired in [k for k, v in _cache.items() if v[0] <= now]:
            del _cache[expired]
        if value is not None:
            _cache[key] = (expires, *value)
            _cache.move_to_end(key)
            # Include conservative per-entry metadata/key/object overhead; also cap entries.
            while len(_cache) > 32 or sum(len(v[1]) + 4096 for v in _cache.values()) > _CACHE_LIMIT:
                _cache.popitem(last=False)
        item = _cache.get(key)
        if item:
            _cache.move_to_end(key)
            return item[1:]
    return None


def _iso(timestamp):
    return datetime.fromtimestamp(timestamp, UTC).isoformat().replace("+00:00", "Z")


@contextmanager
def _source_slot(root):
    # Admission precedes copying/hash work and stays held through child cleanup.
    if not _render_lock.acquire(blocking=False):
        raise HostedError("SOURCE_RENDER_BUSY", 429)
    try:
        with (root / "source-render.lock").open("a+b") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise HostedError("SOURCE_RENDER_BUSY", 429) from None
            yield
    finally:
        _render_lock.release()


class HostedReads:
    def __init__(self, store):
        self.store = store

    def _cursor(self, auth, kind, document, cursor=None, position=None):
        from proofops_api.hosted_guest import ShowcaseReader

        if isinstance(auth, ShowcaseReader):
            kind = "showcase:" + kind
        secret = self.store.settings.secret.encode()
        scope = hmac.digest(
            secret, json.dumps([auth.tenant_id, auth.user_sub, kind, document]).encode(), "sha256"
        ).hex()
        if position is not None:
            payload = json.dumps([scope, *position], separators=(",", ":")).encode()
            signature = hmac.digest(secret, b"hosted-list-v1:" + payload, "sha256")
            return base64.urlsafe_b64encode(signature + payload).decode().rstrip("=")
        if not cursor:
            return None
        try:
            raw = base64.b64decode(
                cursor + "=" * ((-len(cursor)) % 4), altchars=b"-_", validate=True
            )
            signature, payload = raw[:32], raw[32:]
            if not hmac.compare_digest(
                signature, hmac.digest(secret, b"hosted-list-v1:" + payload, "sha256")
            ):
                raise ValueError("signature")
            bound, created, resource = json.loads(payload)
            if bound != scope or type(created) not in (float, int) or not math.isfinite(created):
                raise ValueError("scope")
            identifier(resource)
            return created, resource
        except (ValueError, TypeError, HostedError):
            raise HostedError("CURSOR_INVALID", 400) from None

    def collection(self, auth, kind, *, limit=20, cursor=None, document=None, now=None):
        now = time.time() if now is None else now
        if not auth.has_capability("viewer"):
            raise HostedError("FORBIDDEN", 403)
        if not 1 <= limit <= 100 or kind not in ("documents", "runs"):
            raise HostedError("VALIDATION_ERROR", 422)
        position = self._cursor(auth, kind, document, cursor)
        from proofops_api.hosted_access import is_organizer
        from proofops_api.hosted_guest import ShowcaseReader

        showcase = isinstance(auth, ShowcaseReader)
        personal = is_organizer(auth) and not showcase
        with self.store.transaction(readonly=True) as db:
            if document:
                self.store._document(db, auth.tenant_id, document)
                self.store._require_document_owner(db, auth, document)
            if kind == "documents":
                sql = (
                    "SELECT * FROM (SELECT d.*,row_number() OVER "
                    "(ORDER BY created,document_id) AS ordinal "
                    "FROM documents d WHERE tenant_id=? AND expires>? AND NOT EXISTS "
                    "(SELECT 1 FROM deletion.tombstones t WHERE t.tenant_id=d.tenant_id "
                    "AND t.document_id=d.document_id)"
                )
                params = [auth.tenant_id, now]
                if personal:
                    sql += (
                        " AND EXISTS (SELECT 1 FROM document_owners o WHERE "
                        "o.tenant_id=d.tenant_id AND o.document_id=d.document_id "
                        "AND o.user_hash=?)"
                    )
                    params.append(self.store.user_hash(auth.user_sub))
                sql += ") WHERE 1=1"
                key = "document_id"
            else:
                sql = (
                    "SELECT r.* FROM runs r JOIN documents d USING(tenant_id,document_id) "
                    "WHERE r.tenant_id=? AND d.expires>? AND NOT EXISTS "
                    "(SELECT 1 FROM deletion.tombstones t WHERE t.tenant_id=r.tenant_id "
                    "AND t.document_id=r.document_id)"
                )
                params = [auth.tenant_id, now]
                if showcase:
                    sql += " AND r.status='completed'"
                if personal:
                    sql += (
                        " AND r.user_hash=? AND EXISTS (SELECT 1 FROM document_owners o "
                        "WHERE o.tenant_id=r.tenant_id AND o.document_id=r.document_id "
                        "AND o.user_hash=?)"
                    )
                    params.extend([self.store.user_hash(auth.user_sub)] * 2)
                key = "run_id"
                if document:
                    sql += " AND r.document_id=?"
                    params.append(document)
            prefix = "r." if kind == "runs" else ""
            if position:
                sql += f" AND ({prefix}created<? OR ({prefix}created=? AND {prefix}{key}<?))"
                params.extend([position[0], position[0], position[1]])
            sql += f" ORDER BY {prefix}created DESC,{prefix}{key} DESC LIMIT ?"
            params.append(limit + 1)
            rows = db.execute(sql, params).fetchall()
            items = []
            result_runs = []
            for row in rows[:limit]:
                if kind == "documents":
                    items.append(
                        {
                            k: row[k]
                            for k in (
                                "document_id",
                                "page_count",
                                "size_bytes",
                                "sha256",
                                "expires",
                            )
                        }
                        | dict(
                            display_name=f"보고서 {row['ordinal']}",
                            uploaded_at=_iso(row["created"]),
                        )
                        | self.store.ux1_document_metadata(auth.tenant_id, row["document_id"])
                    )
                else:
                    projected = self.store._project_run(row, db, inline_result=False)
                    if projected["result_url"]:
                        # Same source-integrity guard as individual GET.
                        result_runs.append(row["run_id"])
                    fields = (
                        "run_id",
                        "document_id",
                        "status",
                        "stage",
                        "progress",
                        "selected_pages",
                        "claim_pages",
                        "status_url",
                        "result_url",
                        "error_code",
                        "error_message",
                        "claims_extracted",
                        "claims_processed",
                        "claim_stage_counts",
                        "queue_position",
                    )
                    items.append(
                        {k: projected[k] for k in fields} | dict(created_at=_iso(row["created"]))
                    )
            next_cursor = (
                self._cursor(
                    auth,
                    kind,
                    document,
                    position=(rows[limit - 1]["created"], rows[limit - 1][key]),
                )
                if len(rows) > limit
                else None
            )
        for run in result_runs:
            self.store.get_run(auth.tenant_id, run)
        return dict(items=items, next_cursor=next_cursor)

    def _live_run(self, db, auth, run, now):
        identity = db.execute(
            "SELECT * FROM deletion.source_retention WHERE tenant_id=? AND run_id=?",
            (auth.tenant_id, run),
        ).fetchone()
        if identity and identity["expires"] <= now:
            raise HostedError("SOURCE_EXPIRED", 410)
        row = self.store._run(db, auth.tenant_id, run)
        document = self.store._document(db, auth.tenant_id, row["document_id"])
        if document["expires"] <= now:
            raise HostedError("SOURCE_EXPIRED", 410)
        return row, document

    def _anchor(self, path, row, prepared_page):
        """Read old immutable publications directly, without initializing/mutating local stores."""
        pilot = json.loads((path / "pilot.json").read_bytes())
        tenant = row["tenant_id"]
        local_run = identifier(pilot["run_id"])
        if pilot["tenant_id"] != tenant:
            raise ValueError("tenant")
        database = path / "state.sqlite3"
        if database.is_symlink() or path.is_symlink() or (path / "pilot.json").is_symlink():
            raise ValueError("database")
        from proofops.application.tagging.fact_assembly_c3 import PROFILE as C3_PROFILE
        from proofops.application.tagging.fact_assembly_c3_v2 import PROFILE as C3_V2_PROFILE
        from proofops.application.tagging.fact_assembly_c4 import PROFILE as C4_PROFILE

        if pilot.get("publication_profile") == C4_PROFILE:
            from proofops.adapters.local.c4_publication import LocalC4Publication

            publication = LocalC4Publication(path)
            inputs = publication.load_inputs(tenant, local_run, pilot["claim_id"])
            graph = inputs.original
            if prepared_page not in pilot["selected_pages"] or row["prepared_sha256"] not in (
                None,
                graph.source_sha256,
            ):
                raise ValueError("C4 source page binding")
            block = next(
                b for b in graph.blocks if b.page_num == prepared_page and b.winner is not None
            )
            return (
                graph.document_version_id,
                graph.parse_manifest_id,
                publication.runs.snapshot(tenant, local_run)["c4_evidence_sha256"],
                block.candidates[block.winner].geometry,
            )

        if pilot.get("publication_profile") in (C3_PROFILE, C3_V2_PROFILE):
            from proofops.adapters.local.c3_publication import LocalC3Publication

            publication = LocalC3Publication(path)
            inputs = publication.load_inputs(tenant, local_run, pilot["claim_id"])
            graph = inputs.original
            if prepared_page != 36 or (
                pilot.get("publication_profile") == C3_PROFILE
                and row["prepared_sha256"] not in (None, graph.source_sha256)
            ):
                raise ValueError("C3 source page binding")
            block = next(
                b for b in graph.blocks if b.page_num == prepared_page and b.winner is not None
            )
            return (
                graph.document_version_id,
                graph.parse_manifest_id,
                publication.runs.snapshot(tenant, local_run)["c3_evidence_sha256"],
                block.candidates[block.winner].geometry,
            )
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
            db.execute("BEGIN")
            snapshot = json.loads(
                db.execute(
                    "SELECT payload FROM run_snapshots WHERE tenant_id=? AND run_id=?",
                    (tenant, local_run),
                ).fetchone()[0]
            )
            if (snapshot["tenant_id"], snapshot["run_id"], snapshot["input_hash"]) != (
                tenant,
                local_run,
                canonical_hash({k: v for k, v in snapshot.items() if k != "input_hash"}),
            ):
                raise ValueError("snapshot")
            meta = json.loads(LocalSQLiteJobStore._raw(db, tenant, local_run, "run", "META"))
            message = meta["parse_job"]
            job = json.loads(
                LocalSQLiteJobStore._raw(db, tenant, local_run, "job", message["job_id"])
            )
            if job["message"] != message:
                raise ValueError("job")
            ref = job["artifact_ref"]
            raw = LocalSQLiteJobStore._raw(db, tenant, local_run, "artifact", ref["key"])
            if digest(raw) != ref["sha256"] or len(raw) != ref["byte_size"]:
                raise ValueError("checkpoint")
            checkpoint = json.loads(raw)
        version = identifier(snapshot["document"]["version_id"])
        manifest_id = str(uuid5(UUID(local_run), "parse:" + snapshot["input_hash"]))
        prepared_sha = row["prepared_sha256"]
        if (
            meta["document_version_id"] != version
            or snapshot["document"]["sha256"] != prepared_sha
            or checkpoint["tenant_id"] != tenant
            or checkpoint["run_id"] != local_run
            or checkpoint["document_version_id"] != version
            or checkpoint["source_sha256"] != prepared_sha
            or checkpoint["input_hash"] != snapshot["input_hash"]
            or checkpoint["parse_manifest_id"] != manifest_id
            or checkpoint["object_version_id"] != snapshot["document"]["object_version_id"]
        ):
            raise ValueError("binding")
        directory = path / "parser-prepared" / tenant / version / manifest_id
        if directory.is_symlink() or any(
            (directory / name).is_symlink() for name in ("manifest.json", "geometry.json")
        ):
            raise ValueError("unsafe parse artifact")
        manifest_raw = (directory / "manifest.json").read_bytes()
        if digest(manifest_raw) != checkpoint["manifest_sha256"]:
            raise ValueError("manifest hash")
        manifest = json.loads(manifest_raw)
        if any(
            manifest[k] != checkpoint[k]
            for k in (
                "tenant_id",
                "document_version_id",
                "source_sha256",
                "parse_manifest_id",
                "object_version_id",
            )
        ):
            raise ValueError("manifest scope")
        geometry_raw = (directory / "geometry.json").read_bytes()
        if digest(geometry_raw) != manifest["artifacts"]["geometry.json"]:
            raise ValueError("geometry")
        geometry = json.loads(geometry_raw)[str(prepared_page)]["crop"]
        return version, manifest_id, checkpoint["manifest_sha256"], PageGeometry(**geometry)

    @staticmethod
    def _refs(result):
        # Stored result refs only; arbitrary client-provided boxes never enter.
        def visit(value, identity):
            if isinstance(value, dict):
                if {
                    "source_id",
                    "document_version_id",
                    "parse_manifest_id",
                    "page_num",
                } <= value.keys():
                    yield identity, value
                else:
                    for key, child in value.items():
                        yield from visit(child, identity + "/" + key)
            elif isinstance(value, list):
                for i, child in enumerate(value):
                    yield from visit(child, identity + f"/{i}")

        for claim in result.get("claims", []):
            yield claim["claim_id"], list(visit(claim, "claims/" + claim["claim_id"]))

    def page(
        self, auth, run, physical_page, *, source_id=None, claim_id=None, document_version_id=None
    ):
        with _source_slot(self.store.root):
            return self._page(
                auth,
                run,
                physical_page,
                source_id=source_id,
                claim_id=claim_id,
                document_version_id=document_version_id,
            )

    def _page(
        self, auth, run, physical_page, *, source_id=None, claim_id=None, document_version_id=None
    ):
        identifier(run)
        if not auth.has_capability("viewer"):
            raise HostedError("FORBIDDEN", 403)
        with (
            TemporaryDirectory(prefix=".source-read-", dir=self.store.root) as scratch,
            ExitStack() as leases,
        ):
            with self.store.transaction(readonly=True) as db:
                row, doc = self._live_run(db, auth, run, time.time())
                directory = self.store.document_dir(auth.tenant_id, row["document_id"])
                lock = leases.enter_context((directory / "analysis.lock").open("a+b"))
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            selected = json.loads(row["pages"])
            if physical_page not in selected:
                raise HostedError("SOURCE_REFERENCE_NOT_FOUND", 404)
            result = self.store._result(None, row)
            directory = self.store.document_dir(auth.tenant_id, row["document_id"])
            path = directory / "analysis" / run
            original_refs = any(
                c.get("source_ref_page_space") == "original"
                and (claim_id is None or c["claim_id"] == claim_id)
                and (c.get("original_page_num") == physical_page)
                for c in result.get("claims", [])
            )
            if original_refs and (path / "bounded-c3" / "pilot.json").is_file():
                path = path / "bounded-c3"
            if original_refs:
                from proofops.application.tagging.fact_assembly_c3 import PROFILE as C3_PROFILE
                from proofops.application.tagging.fact_assembly_c3 import (
                    REVIEWED_IDENTITY,
                )
                from proofops.application.tagging.fact_assembly_c3_v2 import (
                    PROFILE as C3_V2_PROFILE,
                )
                from proofops.application.tagging.fact_assembly_c4 import PROFILE as C4_PROFILE

                pilot = json.loads((path / "pilot.json").read_text())
                c4 = pilot.get("publication_profile") == C4_PROFILE
                if c4:
                    if selected != pilot["selected_pages"]:
                        raise HostedError("SOURCE_REFERENCE_MISMATCH", 409)
                elif pilot.get("publication_profile") not in (C3_PROFILE, C3_V2_PROFILE):
                    raise HostedError("SOURCE_REFERENCE_MISMATCH", 409)
                elif selected != [36] and not (
                    path.name == "bounded-c3"
                    and pilot.get("publication_profile") == C3_V2_PROFILE
                    and 36 in selected
                ):
                    raise HostedError("SOURCE_REFERENCE_MISMATCH", 409)
            reference_page = physical_page if original_refs else selected.index(physical_page) + 1
            refs = [
                (identity, ref)
                for cid, items in self._refs(result)
                for identity, ref in items
                if (claim_id is None or cid == claim_id)
                and ref["page_num"] == reference_page
                and (source_id is None or ref["source_id"] == source_id)
            ]
            if not refs:
                raise HostedError("SOURCE_REFERENCE_NOT_FOUND", 404)
            directory = self.store.document_dir(auth.tenant_id, row["document_id"])
            try:
                version, manifest, manifest_sha, geometry = self._anchor(path, row, reference_page)
                if (document_version_id is not None and document_version_id != version) or any(
                    ref["document_version_id"] != version or ref["parse_manifest_id"] != manifest
                    for _, ref in refs
                ):
                    raise HostedError("SOURCE_REFERENCE_MISMATCH", 409)
                prepared = directory / f"{run}.prepared.pdf"
                if not original_refs and (
                    prepared.is_symlink() or file_sha256(prepared) != row["prepared_sha256"]
                ):
                    raise ValueError("prepared hash")
                source = self.store.analysis_source(directory, dict(doc))
                source_sha = file_sha256(source)
                if original_refs:
                    if c4 or pilot.get("publication_profile") == C3_V2_PROFILE:
                        from proofops.adapters.local.c3_publication import LocalC3Publication
                        from proofops.adapters.local.c4_publication import LocalC4Publication

                        publication = LocalC4Publication(path) if c4 else LocalC3Publication(path)
                        frozen = publication.runs.snapshot(auth.tenant_id, pilot["run_id"])
                        expected_original = frozen["document"]["sha256"]
                    else:
                        expected_original = REVIEWED_IDENTITY["original_sha256"]
                    if source_sha != expected_original:
                        raise ValueError("uploaded original mismatch")
                key = (
                    str(self.store.root),
                    auth.tenant_id,
                    row["document_id"],
                    doc["sha256"],
                    source_sha,
                    row["prepared_sha256"],
                    manifest_sha,
                    physical_page,
                )
                cached = _cached(key, doc["expires"], time.time())
                if not cached:
                    copy_verified_file(source, Path(scratch) / "source.pdf", source_sha)
            except HostedError as exc:
                if exc.code == "SOURCE_REFERENCE_MISMATCH":
                    raise
                raise HostedError("SOURCE_INTEGRITY_UNAVAILABLE", 409) from None
            except (
                OSError,
                ValueError,
                KeyError,
                TypeError,
                IndexError,
                sqlite3.DatabaseError,
            ):
                raise HostedError("SOURCE_INTEGRITY_UNAVAILABLE", 409) from None
            if cached:
                png, metadata = cached
            else:
                ops2_fit = _ops2_fit_page(self.store.run_options(row))
                if original_refs or ops2_fit:
                    png, metadata = render(
                        Path(scratch) / "source.pdf",
                        physical_page,
                        geometry,
                        Path(scratch),
                        fit_page=True,
                    )
                else:
                    png, metadata = render(
                        Path(scratch) / "source.pdf", physical_page, geometry, Path(scratch)
                    )
            # A cache or subprocess can outlive a membership revoke, deletion or expiry.
            from proofops.application.authorization import (
                CapabilityDeniedError,
                SessionExpiredError,
                TenantNotFoundError,
                authorize,
            )

            try:
                fresh = authorize(
                    session_id=auth.session_id,
                    now=time.time(),
                    session_port=self.store.sessions,
                    membership_port=self.store.memberships,
                    required_capability="viewer",
                )
                from proofops_api.hosted_guest import recheck_read_auth

                recheck_read_auth(self.store, auth, fresh, "runs", run, time.time())
            except SessionExpiredError:
                raise HostedError("AUTH_REQUIRED", 401) from None
            except CapabilityDeniedError:
                raise HostedError("FORBIDDEN", 403) from None
            except TenantNotFoundError:
                raise HostedError("RESOURCE_NOT_FOUND", 404) from None
            with self.store.transaction(readonly=True) as db:
                self._live_run(db, auth, run, time.time())
                if not cached:
                    _cached(key, doc["expires"], time.time(), (png, metadata))
            width, height = geometry.width_pt, geometry.height_pt
            highlights = []
            unresolved = []
            for identity, ref in refs:
                box = ref.get("bbox")
                valid = (
                    ref.get("verification_state") == "verified"
                    and ref.get("location_quality") == "located"
                    and isinstance(box, list)
                    and len(box) == 4
                    and all(type(x) in (float, int) and math.isfinite(x) for x in box)
                    and 0 <= box[0] < box[2] <= width
                    and 0 <= box[1] < box[3] <= height
                )
                item = dict(
                    ref_id=identity,
                    source_id=ref["source_id"],
                    verification_state=ref.get("verification_state"),
                )
                if valid:
                    highlights.append(
                        item
                        | dict(
                            bbox_normalized=[
                                box[0] / width,
                                box[1] / height,
                                box[2] / width,
                                box[3] / height,
                            ]
                        )
                    )
                else:
                    unresolved.append(item | dict(message="원문 미검증"))
            image_sha = digest(png)
            manifest_body = dict(
                document_id=row["document_id"],
                document_version_id=version,
                source_sha256=doc["sha256"],
                render_source_sha256=source_sha,
                prepared_sha256=row["prepared_sha256"],
                parse_manifest_id=manifest,
                parse_manifest_sha256=manifest_sha,
                run_id=run,
                physical_page=physical_page,
                prepared_page=selected.index(physical_page) + 1,
                image_sha256=image_sha,
                image_url=f"/v1/runs/{run}/source-pages/{physical_page}/image?image_sha256={image_sha}",
                expires_at=_iso(doc["expires"]),
                image=metadata,
                coordinates=dict(
                    units="pdf_points",
                    origin="top_left",
                    space="rotated_cropbox_display",
                    bbox_format="x0,y0,x1,y1",
                    source_to_image=[
                        metadata["width_px"] / width,
                        0,
                        0,
                        metadata["height_px"] / height,
                        0,
                        0,
                    ],
                ),
                highlights=highlights,
                unresolved_refs=unresolved,
                message="원문 미검증" if unresolved else None,
            )
            return manifest_body, png
