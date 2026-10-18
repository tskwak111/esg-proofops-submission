"""Read-only original PDF and stored-location projections for the split viewer.

No source verifier, tags or immutable result snapshots are changed here.
"""

from __future__ import annotations

import fcntl
import json
import math
import sqlite3
import time
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory

from proofops.domain.documents import PageGeometry
from proofops.domain.provenance import canonical_hash
from starlette.responses import FileResponse

from proofops_api.hosted_store import HostedError, copy_verified_file


class _OriginalPdfResponse(FileResponse):
    def __init__(self, path, *, scratch, **kwargs):
        super().__init__(path, **kwargs)
        self._scratch = scratch

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # PDF.js intentionally aborts the initial full request after reading
            # range headers. Background tasks alone miss that disconnect path.
            self._scratch.cleanup()


def original_pdf(store, auth, document_id, expected_sha=None):
    """Serve only a private, hash-checked copy, never a mutable pathname or derivative."""
    scratch = TemporaryDirectory(prefix=".original-read-", dir=store.root)
    try:
        with store.transaction(readonly=True) as db:
            doc = store._document(db, auth.tenant_id, document_id)
            store._require_document_owner(db, auth, document_id)
            if doc["expires"] <= time.time():
                raise HostedError("SOURCE_EXPIRED", 410)
        if expected_sha is not None and expected_sha != doc["sha256"]:
            raise HostedError("SOURCE_REFERENCE_MISMATCH", 409)
        directory = store.document_dir(auth.tenant_id, document_id)
        target = Path(scratch.name) / "original.pdf"
        try:
            with (directory / "analysis.lock").open("a+b") as lease:
                fcntl.flock(lease, fcntl.LOCK_SH | fcntl.LOCK_NB)
                if (directory / "original.pdf").is_symlink():
                    raise ValueError("unsafe original")
                copy_verified_file(directory / "original.pdf", target, doc["sha256"])
        except (OSError, ValueError, HostedError):
            raise HostedError("SOURCE_INTEGRITY_UNAVAILABLE", 409) from None
        # Recheck after disk IO, just as the existing original-page reader does.
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
                session_port=store.sessions,
                membership_port=store.memberships,
                required_capability="viewer",
            )
        except SessionExpiredError:
            raise HostedError("AUTH_REQUIRED", 401) from None
        except CapabilityDeniedError:
            raise HostedError("FORBIDDEN", 403) from None
        except TenantNotFoundError:
            raise HostedError("RESOURCE_NOT_FOUND", 404) from None
        from proofops_api.hosted_guest import recheck_read_auth

        fresh = recheck_read_auth(store, auth, fresh, "documents", document_id, time.time())
        with store.transaction(readonly=True) as db:
            current = store._document(db, auth.tenant_id, document_id)
            store._require_document_owner(db, fresh, document_id)
            if current["expires"] <= time.time():
                raise HostedError("SOURCE_EXPIRED", 410)
        return _OriginalPdfResponse(
            target,
            scratch=scratch,
            media_type="application/pdf",
            filename="original.pdf",
            headers={
                "Cache-Control": "private, no-store",
                "ETag": f'"{doc["sha256"]}"',
                "X-Source-SHA256": doc["sha256"],
                "X-Content-Type-Options": "nosniff",
            },
        )
    except BaseException:
        scratch.cleanup()
        raise


def _references(claim):
    for i, ref in enumerate(claim.get("source_refs", [])):
        yield f"source_refs/{i}", "claim", None, ref
    for field, ref in (claim.get("table_observation") or {}).get("cell_source_refs", {}).items():
        yield f"table_observation/cell_source_refs/{field}", "claim", None, ref
    for key, elements in (
        ("elements", claim.get("elements", [])),
        ("provisional_grade/elements", (claim.get("provisional_grade") or {}).get("elements", [])),
    ):
        for i, element in enumerate(elements):
            for j, ref in enumerate(element.get("evidence_refs", [])):
                yield f"{key}/{i}/evidence_refs/{j}", "evidence", element["element_id"], ref


def _stored_glyphs(path, evidence_hash, ref, physical, source_sha, geometry):
    # C3/C4 receipts already contain exact glyph rectangles. Copy them verbatim;
    # do not estimate lines or search/re-parse the PDF to manufacture coordinates.
    for name in ("c3-evidence.json", "c4-evidence.json"):
        file = path / name
        if file.is_symlink() or not file.is_file():
            continue
        try:
            evidence = json.loads(file.read_bytes())
            if canonical_hash(evidence) != evidence_hash:
                return None
            for record in evidence.get("source_receipt", {}).get("records", []):
                stored = record.get("ref", {})
                if record.get("status") != "verified" or any(
                    stored.get(key) != ref.get(key)
                    for key in (
                        "source_id",
                        "document_version_id",
                        "parse_manifest_id",
                        "page_num",
                        "raw_text_sha256",
                        "bbox",
                    )
                ):
                    continue
                normalization = record.get("normalization", {})
                if (
                    normalization.get("source_sha256") != source_sha
                    or normalization.get("physical_page") != physical
                ):
                    continue
                glyphs = normalization.get("glyphs", [])
                if any(stored.get(k) != ref.get(k) for k in ("quote", "char_start", "char_end")):
                    # Evidence may cite an exact subspan of the already verified
                    # receipt. Use its stored character map, never text search.
                    start, end = ref.get("char_start"), ref.get("char_end")
                    parent_start, parent_end = stored.get("char_start"), stored.get("char_end")
                    quote = stored.get("quote", "")
                    offsets = normalization.get("canonical_nonspace_offsets", [])
                    if (
                        not all(type(n) is int for n in (start, end, parent_start, parent_end))
                        or not parent_start <= start < end <= parent_end
                        or parent_end - parent_start != len(quote)
                        or quote[start - parent_start : end - parent_start] != ref.get("quote")
                        or normalization.get("canonical_quote") != quote
                        or offsets != [i for i, char in enumerate(quote) if not char.isspace()]
                        or len(offsets) != len(glyphs)
                        or any(
                            g.get("char") != quote[i] for i, g in zip(offsets, glyphs, strict=True)
                        )
                    ):
                        continue
                    glyphs = [
                        glyph
                        for offset, glyph in zip(offsets, glyphs, strict=True)
                        if start - parent_start <= offset < end - parent_start
                    ]
                boxes = [g["bbox"] for g in glyphs]
                if boxes and all(
                    isinstance(b, list)
                    and len(b) == 4
                    and all(type(v) in (int, float) and math.isfinite(v) for v in b)
                    and 0 <= b[0] < b[2] <= geometry.width_pt
                    and 0 <= b[1] < b[3] <= geometry.height_pt
                    for b in boxes
                ):
                    return boxes
        except (OSError, ValueError, KeyError, TypeError):
            return None
    return None


def project_source_view(reads, row, doc, result, *, w18=False):
    """Add nullable locations using only the existing pinned snapshot/geometry reader."""
    if result.get("reuse", {}).get("reused"):
        from proofops_api.hosted_reuse import source_document, verify_copy

        verify_copy(reads.store, row, result)
        return result | dict(w16_source_document=source_document(doc))
    root = (
        reads.store.document_dir(row["tenant_id"], row["document_id"]) / "analysis" / row["run_id"]
    )
    pages = result["page_map"]
    anchors = {}
    claims = []
    w18_requests = []
    for claim in result.get("claims", []):
        original = claim.get("source_ref_page_space") == "original"
        path = (
            root / "bounded-c3" if original and (root / "bounded-c3/pilot.json").is_file() else root
        )
        refs = []
        for ref_id, kind, element_id, ref in _references(claim):
            number = ref.get("page_num")
            physical = (
                number
                if original and type(number) is int and 1 <= number <= doc["page_count"]
                else pages[number - 1]
                if not original and type(number) is int and 0 < number <= len(pages)
                else None
            )
            location = None
            key = (path, number)
            if physical is not None and key not in anchors:
                try:
                    anchors[key] = (
                        _publication_anchor(path, row, doc, number)
                        if original
                        else reads._anchor(path, row, number)
                    )
                except (
                    OSError,
                    ValueError,
                    KeyError,
                    TypeError,
                    IndexError,
                    StopIteration,
                    sqlite3.DatabaseError,
                    HostedError,
                ):
                    anchors[key] = None
            anchor = anchors.get(key)
            if anchor is not None:
                version, manifest, evidence_hash, geometry = anchor
                box = ref.get("bbox")
                if (
                    ref.get("document_version_id") == version
                    and ref.get("parse_manifest_id") == manifest
                    and ref.get("verification_state") == "verified"
                    and ref.get("location_quality") == "located"
                    and isinstance(box, list)
                    and len(box) == 4
                    and all(type(x) in (int, float) and math.isfinite(x) for x in box)
                    and 0 <= box[0] < box[2] <= geometry.width_pt
                    and 0 <= box[1] < box[3] <= geometry.height_pt
                ):
                    glyphs = _stored_glyphs(
                        path, evidence_hash, ref, physical, doc["sha256"], geometry
                    )
                    location = dict(
                        bbox=box,
                        line_rects=_glyph_lines(glyphs) if glyphs else None,
                        glyph_rects=glyphs,
                        page=asdict(geometry),
                        units="pdf_points",
                        origin="top_left",
                        space="rotated_cropbox_display",
                        bbox_format="x0,y0,x1,y1",
                    )
            refs.append(
                dict(
                    ref_id=ref_id,
                    source_id=ref.get("source_id"),
                    kind=kind,
                    element_id=element_id,
                    quote=ref.get("quote", ""),
                    physical_page=physical,
                    location=location,
                )
            )
            if w18:
                display_ref = ref
                if (
                    kind == "claim"
                    and (claim.get("source_status") or {}).get(
                        "status", claim.get("source_quality")
                    )
                    != "verified"
                ):
                    display_ref = ref | {"verification_state": "unverified"}
                w18_requests.append((refs[-1], display_ref, path, anchor, original))
        claims.append(claim | dict(w16_source_view=dict(quote=claim["quote"], refs=refs)))
    if w18:
        from proofops_api.w18_source_view import enrich

        enrich(reads, row, doc, w18_requests)
    return result | dict(
        claims=claims,
        w16_source_document=dict(
            document_id=row["document_id"],
            sha256=doc["sha256"],
            page_count=doc["page_count"],
            url=f'/v1/documents/{row["document_id"]}/original?sha256={doc["sha256"]}',
        ),
    )


def _glyph_lines(boxes):
    """Display unions of sequential, vertically overlapping stored glyphs only.

    A leftward jump or disjoint vertical extent starts a new rectangle. Every
    edge is a min/max of receipt coordinates; no new text or location is inferred.
    """
    lines = []
    previous_x = None
    for box in boxes:
        if not lines or box[0] < previous_x or box[1] >= lines[-1][3] or box[3] <= lines[-1][1]:
            lines.append(list(box))
        else:
            prior = lines[-1]
            lines[-1] = [
                min(prior[0], box[0]),
                min(prior[1], box[1]),
                max(prior[2], box[2]),
                max(prior[3], box[3]),
            ]
        previous_x = box[0]
    return lines


def _publication_anchor(path, row, doc, page):
    """Read C3/C4 frozen display geometry without replaying current rule policies.

    The result reader has checked publication source_hashes. Additionally bind the
    immutable local snapshot, evidence hash, original SHA and graph identity here.
    This does not re-verify or promote any stored source/evidence state.
    """
    pilot = json.loads((path / "pilot.json").read_bytes())
    if pilot["tenant_id"] != row["tenant_id"] or path.is_symlink():
        raise ValueError("publication scope")
    database = path / "state.sqlite3"
    if database.is_symlink():
        raise ValueError("unsafe database")
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        frozen = json.loads(
            db.execute(
                "SELECT payload FROM run_snapshots WHERE tenant_id=? AND run_id=?",
                (row["tenant_id"], pilot["run_id"]),
            ).fetchone()[0]
        )
    if (
        frozen["tenant_id"] != row["tenant_id"]
        or frozen["run_id"] != pilot["run_id"]
        or frozen["input_hash"]
        != canonical_hash({k: v for k, v in frozen.items() if k != "input_hash"})
        or frozen["document"]["sha256"] != doc["sha256"]
    ):
        raise ValueError("frozen source identity")
    kind = "c4" if "c4_evidence_sha256" in frozen else "c3"
    file = path / f"{kind}-evidence.json"
    if file.is_symlink():
        raise ValueError("unsafe evidence")
    evidence = json.loads(file.read_bytes())
    expected = frozen[f"{kind}_evidence_sha256"]
    if canonical_hash(evidence) != expected:
        raise ValueError("evidence hash")
    graph = evidence["parser_artifact"]
    if (
        graph["tenant_id"] != row["tenant_id"]
        or graph["source_sha256"] != doc["sha256"]
        or graph["document_version_id"] != frozen["document"]["version_id"]
    ):
        raise ValueError("graph identity")
    geometries = [
        candidate["geometry"]
        for block in graph["blocks"]
        if block.get("winner") is not None
        for candidate in [block["candidates"][block["winner"]]]
        if candidate["source"]["physical_page"] == page
    ]
    if not geometries or any(g != geometries[0] for g in geometries):
        raise ValueError("missing or inconsistent page geometry")
    return (
        graph["document_version_id"],
        graph["parse_manifest_id"],
        expected,
        PageGeometry(**geometries[0]),
    )
