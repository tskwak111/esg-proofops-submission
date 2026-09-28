"""Fixed source/quality reads from immutable, fence-published parser artifacts."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Annotated, Literal
from urllib.parse import urlencode
from uuid import UUID, uuid5

from fastapi import APIRouter, Query, Request, Security
from fastapi.responses import JSONResponse, Response
from fastapi.security import APIKeyCookie
from proofops.adapters.local.claim_store import LocalClaimStore
from proofops.adapters.local.run_artifacts import load_run_graph
from proofops.adapters.parsing.source_preview import SourcePreviewFailure, render_page_preview
from proofops.application.runs import RunRejected
from proofops_api.auth import (
    SESSION_COOKIE_NAME,
    _error_response,
    _request_limit_response,
    _verify_csrf,
)
from proofops_api.request_limits import READ_REQUESTS_PER_MINUTE
from proofops_api.routers.documents import StrictDTO
from proofops_api.routers.registry import _authorize
from proofops_api.rulepacks import _ERROR_RESPONSES
from pydantic import Field


class SourceRef(StrictDTO):
    source_id: UUID
    document_version_id: UUID
    parse_manifest_id: UUID
    page_num: Annotated[int, Field(ge=1)]
    printed_page_label: str | None
    bbox: tuple[float, float, float, float] | None
    raw_text_sha256: Annotated[str, Field(pattern="^[0-9a-f]{64}$")]
    quote: str
    char_start: Annotated[int, Field(ge=0)]
    char_end: Annotated[int, Field(ge=0)]
    location_quality: Literal["located", "unlocated", "unreadable"]
    verification_state: Literal["verified", "candidate", "rejected"]


class QualityIssue(StrictDTO):
    issue_id: UUID
    kind: str
    page_num: Annotated[int, Field(ge=1)]
    source_ids: list[UUID]
    state: Literal["open", "resolved", "unreadable"]
    reason: str


class QualityIssuePage(StrictDTO):
    items: list[QualityIssue]
    next_cursor: str | None
    snapshot_epoch: Annotated[int, Field(ge=0)] | None


class Download(StrictDTO):
    url: str
    expires_at: datetime
    sha256: Annotated[str, Field(pattern="^[0-9a-f]{64}$")]


def build_sources_router(
    store, uploads, parser, auth_store, *, allowed_origin=None, clock=time.time
):
    router = APIRouter(
        responses=_ERROR_RESPONSES,
        dependencies=[
            Security(
                APIKeyCookie(
                    name=SESSION_COOKIE_NAME, scheme_name="sessionCookie", auto_error=False
                )
            )
        ],
    )
    read_contract = {
        "x-minimum-role": "viewer",
        "x-idempotency-required": False,
        "x-rate-limit": "120/min/user",
    }

    def failure(exc):
        if isinstance(exc, RunRejected):
            return _error_response(exc.status, exc.code, "Evidence request could not be processed.")
        return _error_response(
            409, "ARTIFACT_UNAVAILABLE", "Evidence is pending or failed integrity checks."
        )

    def authorize(request, operation_id, *, issue_ticket=False):
        auth = _authorize(request, auth_store, time.time(), "viewer")
        if isinstance(auth, JSONResponse):
            return auth
        if issue_ticket:
            session = auth_store.sessions.get(auth.session_id)
            if session is None or not _verify_csrf(
                request, session.csrf_hash, allowed_origin=allowed_origin
            ):
                return _error_response(403, "CSRF_INVALID", "Session and request origin required.")
        limited = _request_limit_response(
            auth_store,
            user_sub=auth.user_sub,
            operation_id=operation_id,
            requests=60 if issue_ticket else READ_REQUESTS_PER_MINUTE,
            now=clock(),
        )
        if limited is not None:
            return limited
        return auth

    def load(request, run_id, operation_id):
        auth = authorize(request, operation_id)
        if isinstance(auth, JSONResponse):
            return auth
        return load_run_graph(store, uploads, parser, tenant_id=auth.tenant_id, run_id=run_id)

    @router.get(
        "/v1/runs/{run_id}/sources/{source_id}",
        response_model=SourceRef,
        operation_id="source_get",
        openapi_extra=read_contract,
    )
    def source(request: Request, run_id: UUID, source_id: UUID):
        try:
            graph = load(request, str(run_id), "source_get")
            if isinstance(graph, JSONResponse):
                return graph
            block = next((b for b in graph.blocks if b.source_id == str(source_id)), None)
            if block is None:
                raise RunRejected("RESOURCE_NOT_FOUND", 404)
            if block.winner is None:
                raise RunRejected("SOURCE_CONFLICT")
            return JSONResponse(asdict(block.source_ref()))
        except (ValueError, KeyError, OSError, sqlite3.DatabaseError) as exc:
            return failure(exc)

    @router.post(
        "/v1/runs/{run_id}/sources/{source_id}/view",
        response_model=Download,
        operation_id="source_view",
        openapi_extra={
            **read_contract,
            "x-rate-limit": "60/min/user",
            "parameters": [
                {
                    "name": "X-CSRF-Token",
                    "in": "header",
                    "required": True,
                    "schema": {"type": "string"},
                }
            ],
        },
    )
    def source_view(request: Request, run_id: UUID, source_id: UUID):
        auth = authorize(request, "source_view", issue_ticket=True)
        if isinstance(auth, JSONResponse):
            return auth
        try:
            graph = load_run_graph(
                store, uploads, parser, tenant_id=auth.tenant_id, run_id=str(run_id)
            )
            block = next((b for b in graph.blocks if b.source_id == str(source_id)), None)
            if block is None:
                raise RunRejected("RESOURCE_NOT_FOUND", 404)
            expires = int(clock()) + 300
            ticket = store._encode_cursor(
                dict(
                    scope=[auth.tenant_id, str(run_id), "source_view", 0],
                    user=auth.user_sub,
                    source=str(source_id),
                    version=graph.document_version_id,
                    manifest=graph.parse_manifest_id,
                    sha256=graph.source_sha256,
                    expires=expires,
                )
            )
            return JSONResponse(
                dict(
                    url=(
                        f"/local/sources/{run_id}/{source_id}?"
                        f"{urlencode({'ticket': ticket})}#page={block.page_num}"
                    ),
                    expires_at=datetime.fromtimestamp(expires, UTC)
                    .isoformat()
                    .replace("+00:00", "Z"),
                    sha256=graph.source_sha256,
                ),
                headers={"Cache-Control": "no-store"},
            )
        except (ValueError, KeyError, OSError, sqlite3.DatabaseError) as exc:
            return failure(exc)

    @router.get("/local/sources/{run_id}/{source_id}", include_in_schema=False)
    def source_pdf(
        request: Request,
        run_id: UUID,
        source_id: UUID,
        ticket: str,
        preview: Literal["page"] | None = None,
    ):
        auth = authorize(request, "source_pdf")
        if isinstance(auth, JSONResponse):
            return auth
        try:
            grant = store._decode_cursor(
                ticket, auth.tenant_id, str(run_id), 0, int(clock()), endpoint="source_view"
            )
            if grant["user"] != auth.user_sub or grant["source"] != str(source_id):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            return _error_response(
                403, "SOURCE_VIEW_EXPIRED", "Source view authorization required."
            )
        try:
            graph = load_run_graph(
                store, uploads, parser, tenant_id=auth.tenant_id, run_id=str(run_id)
            )
            if (grant["version"], grant["manifest"], grant["sha256"]) != (
                graph.document_version_id,
                graph.parse_manifest_id,
                graph.source_sha256,
            ):
                raise ValueError("source view identity mismatch")
            block = next(
                (block for block in graph.blocks if block.source_id == str(source_id)), None
            )
            if block is None:
                raise ValueError("source view identity mismatch")
            original = uploads.read_original(auth.tenant_id, graph.document_version_id)
            headers = {
                "Cache-Control": "no-store, private",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
            }
            if preview == "page":
                candidate = (
                    block.candidates[block.winner]
                    if block.winner is not None
                    else block.candidates[0]
                )
                png, width_pt, height_pt = render_page_preview(
                    original, candidate.source.physical_page, candidate.geometry
                )
                headers.update(
                    {
                        "X-Page-Width-Pt": str(width_pt),
                        "X-Page-Height-Pt": str(height_pt),
                        "X-Source-Highlight": "allowed"
                        if _highlight_allowed(block, graph.issues)
                        else "unavailable",
                    }
                )
                return Response(png, media_type="image/png", headers=headers)
            # ponytail: one bounded full PDF response; add ranges if large-file viewing needs them.
            return Response(
                original,
                media_type="application/pdf",
                headers={
                    "Content-Disposition": 'inline; filename="source.pdf"',
                    **headers,
                },
            )
        except (SourcePreviewFailure, ValueError, KeyError, OSError, sqlite3.DatabaseError) as exc:
            return failure(exc)

    @router.get(
        "/v1/runs/{run_id}/quality",
        response_model=QualityIssuePage,
        operation_id="quality_get",
        openapi_extra=read_contract,
    )
    def quality(
        request: Request,
        run_id: UUID,
        cursor: str | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ):
        try:
            graph = load(request, str(run_id), "quality_get")
            if isinstance(graph, JSONResponse):
                return graph
            now = int(clock())
            page = (
                store._decode_cursor(
                    cursor, graph.tenant_id, str(run_id), limit, now, endpoint="quality"
                )
                if cursor
                else dict(
                    scope=[graph.tenant_id, str(run_id), "quality", limit],
                    expires=now + 900,
                    after=0,
                    manifest=graph.parse_manifest_id,
                    epoch=store.get(graph.tenant_id, str(run_id))["mutation_epoch"],
                )
            )
            if page["manifest"] != graph.parse_manifest_id:
                raise RunRejected("INVALID_CURSOR", 400)
            discovery = None
            if store.jobs.get_run(graph.tenant_id, str(run_id)).get("claim_snapshot_sha256"):
                _, discovery, graph = LocalClaimStore(store, uploads, parser).load_evidence(
                    graph.tenant_id, str(run_id)
                )
            issues = _quality_items(graph, discovery)
            after = page["after"] + limit
            return JSONResponse(
                dict(
                    items=issues[page["after"] : after],
                    next_cursor=store._encode_cursor(dict(page, after=after))
                    if after < len(issues)
                    else None,
                    snapshot_epoch=page["epoch"],
                )
            )
        except (ValueError, KeyError, OSError, sqlite3.DatabaseError) as exc:
            return failure(exc)

    return router


def _highlight_allowed(block, issues) -> bool:
    """Do not turn a readable image into a verified source highlight."""
    if (
        block.bbox is None
        or block.winner is None
        or any(
            issue.kind == "parse_conflict"
            and issue.state == "open"
            and block.source_id in issue.source_ids
            for issue in issues
        )
    ):
        return False
    source_ref = block.source_ref()
    candidate = block.candidates[block.winner]
    return (
        source_ref.location_quality == "located"
        and source_ref.verification_state == "verified"
        and source_ref.bbox == candidate.bbox
        and all(item.geometry == candidate.geometry for item in block.candidates)
    )


def _quality_items(graph, discovery=None):
    """Read-time warnings; never rewrite the pinned graph or its coverage."""
    items = {issue.issue_id: issue.to_dict() for issue in graph.issues}
    for block in graph.blocks:
        if block.kind == "figure" and not any(s.raw_text.strip() for s in block.sources):
            issue_id = str(uuid5(UUID(block.source_id), "image-text-not-extracted-v1"))
            items.setdefault(
                issue_id,
                dict(
                    issue_id=issue_id,
                    kind="image_text_not_extracted",
                    page_num=block.page_num,
                    source_ids=[block.source_id],
                    state="open",
                    reason=(
                        "이미지 영역에서 텍스트가 추출되지 않았습니다. "
                        "사진일 수도 있으므로 원문을 열어 글·표 누락 여부를 확인하세요. "
                        "근거 부재나 판독 불가가 확정된 것은 아닙니다."
                    ),
                ),
            )
    if discovery is not None:
        for exclusion in discovery.exclusions:
            if exclusion.reason != "unprocessed_span" or exclusion.state != "unknown":
                continue
            issue_id = str(uuid5(UUID(exclusion.source_id), "extraction-span-unprocessed-v1"))
            ref = exclusion.source_ref
            excerpt = ref.quote[:120] if ref is not None else ""
            items.setdefault(
                issue_id,
                dict(
                    issue_id=issue_id,
                    kind="extraction_span_unprocessed",
                    page_num=exclusion.page_num,
                    source_ids=[exclusion.source_id],
                    state="open",
                    reason=(
                        "추출기가 처리하지 않은 텍스트 구간이 있습니다. "
                        "주장 여부가 미확정이며 근거 부재를 뜻하지 않습니다. "
                        "원문에서 확인하세요."
                        + (f" 미처리 구간 일부: {excerpt}" if excerpt else "")
                    ),
                ),
            )
    return sorted(items.values(), key=lambda item: item["issue_id"])
