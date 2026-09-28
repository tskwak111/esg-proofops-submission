"""Version 1 boundary and explicit local-only multipart POST transport."""

from __future__ import annotations

import json
import secrets
import time
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request, Security
from fastapi.responses import JSONResponse, Response
from fastapi.security import APIKeyCookie
from proofops.application.uploads import UploadService
from proofops.application.uploads_security import UploadRejected
from proofops_api.auth import (
    SESSION_COOKIE_NAME,
    AuthStore,
    _error_response,
    _request_limit_response,
    _verify_csrf,
)
from proofops_api.middleware import RequestBodyTooLarge, read_bounded_json
from proofops_api.request_limits import READ_REQUESTS_PER_MINUTE, WRITE_REQUESTS_PER_MINUTE
from proofops_api.routers.registry import _authorize
from proofops_api.rulepacks import _ERROR_RESPONSES
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException
from starlette.formparsers import MultiPartException


class StrictDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class DocumentCreate(StrictDTO):
    company_id: UUID
    title: Annotated[str, Field(min_length=1, max_length=200)]
    document_type: Literal["sustainability_report", "annual_report_section"]


class Document(StrictDTO):
    document_id: UUID
    company_id: UUID
    title: str
    document_type: Literal["sustainability_report", "annual_report_section"]
    latest_version_id: UUID | None = None
    revision: Annotated[int, Field(ge=1)]
    created_at: datetime


class UploadComplete(StrictDTO):
    sha256: Annotated[str, Field(pattern="^[0-9a-f]{64}$")]
    size_bytes: Annotated[int, Field(ge=1, le=104857600)]


class VersionCreate(UploadComplete):
    filename: Annotated[str, Field(min_length=1, max_length=200)]
    report_year: Annotated[int, Field(ge=1900, le=2200)]
    industry_system: Literal["gics", "sasb", "custom", "unknown"]
    industry_code: str | None = None
    consolidation_scope: str | None = None
    period_start: Annotated[str, Field(json_schema_extra={"format": "date"})]
    period_end: Annotated[str, Field(json_schema_extra={"format": "date"})]
    rights_profile_id: UUID


class UploadTicket(StrictDTO):
    upload_id: UUID
    document_id: UUID
    post_url: str
    post_fields: dict[str, str]
    expires_at: datetime


class JobAccepted(StrictDTO):
    job_id: UUID
    resource_id: UUID
    status: Literal["queued", "running", "ready"]
    status_url: str


class DocumentVersion(StrictDTO):
    version_id: UUID
    document_id: UUID
    sha256: Annotated[str, Field(pattern="^[0-9a-f]{64}$")]
    report_year: Annotated[int, Field(ge=1900)]
    page_count: Annotated[int, Field(ge=1)] | None = None
    status: Literal["validating", "ready", "rejected"]
    created_at: datetime


class DocumentPage(StrictDTO):
    items: list[Document]
    next_cursor: str | None
    snapshot_epoch: Annotated[int, Field(ge=0)] | None


class DocumentVersionPage(StrictDTO):
    items: list[DocumentVersion]
    next_cursor: str | None
    snapshot_epoch: Annotated[int, Field(ge=0)] | None


def _request_schema(model: type[BaseModel]) -> dict:
    return {
        "x-minimum-role": "editor",
        "x-idempotency-required": True,
        "x-rate-limit": "10/min/user",
        "parameters": [
            {
                "name": "X-CSRF-Token",
                "in": "header",
                "required": True,
                "schema": {"type": "string"},
            },
            {
                "name": "Idempotency-Key",
                "in": "header",
                "required": True,
                "schema": {"type": "string", "minLength": 16, "maxLength": 128},
            },
        ],
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": model.model_json_schema()}},
        },
    }


def _failure(exc: UploadRejected) -> JSONResponse:
    code = str(exc)
    status = (
        400
        if code == "INVALID_CURSOR"
        else 404
        if code == "NOT_FOUND"
        else 409
        if code
        in ("IDEMPOTENCY_CONFLICT", "VERSION_CONFLICT", "UPLOAD_EXPIRED", "UPLOAD_NOT_READY")
        else 422
    )
    return _error_response(
        status,
        "RESOURCE_NOT_FOUND" if code == "NOT_FOUND" else code,
        "요청한 문서 또는 업로드를 처리할 수 없습니다.",
    )


def build_documents_router(
    service: UploadService,
    auth_store: AuthStore,
    *,
    allowed_origin: str,
    app_env: str,
    model_adapter: str,
    clock: Any = None,
) -> APIRouter:
    """Fail closed unless explicitly mounted in local-synthetic composition."""
    if app_env != "local" or model_adapter != "synthetic" or not service.local_synthetic:
        raise ValueError("local-synthetic upload adapter cannot serve this environment")
    now_fn = clock if clock is not None else time.time
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

    def authorize(request: Request, operation_id: str | None = None, write: bool = False):
        now = now_fn()
        auth = _authorize(request, auth_store, now, "editor" if write else "viewer")
        if isinstance(auth, JSONResponse):
            return auth
        if write:
            session = auth_store.sessions.get(auth.session_id)
            if session is None or not _verify_csrf(
                request, session.csrf_hash, allowed_origin=allowed_origin
            ):
                return _error_response(403, "CSRF_INVALID", "세션과 요청 출처를 확인하세요.")
        if operation_id is not None:
            limited = _request_limit_response(
                auth_store,
                user_sub=auth.user_sub,
                operation_id=operation_id,
                requests=WRITE_REQUESTS_PER_MINUTE if write else READ_REQUESTS_PER_MINUTE,
                now=now,
            )
            if limited is not None:
                return limited
        return auth

    async def payload(request: Request, model: type[BaseModel]) -> dict:
        key = request.headers.get("Idempotency-Key", "")
        if not 16 <= len(key) <= 128:
            raise UploadRejected("IDEMPOTENCY_KEY_INVALID")
        try:
            data = await read_bounded_json(request)
        except (RequestBodyTooLarge, ValueError):
            raise UploadRejected("VALIDATION_ERROR") from None
        return model.model_validate_json(json.dumps(data)).model_dump(
            mode="json", exclude_unset=True
        )

    @router.post(
        "/v1/documents",
        status_code=201,
        response_model=Document,
        operation_id="document_create",
        openapi_extra=_request_schema(DocumentCreate),
    )
    async def create_document(request: Request):
        auth = authorize(request, "document_create", True)
        if isinstance(auth, JSONResponse):
            return auth
        try:
            body = await payload(request, DocumentCreate)
            result = await run_in_threadpool(
                service.create_document,
                auth.tenant_id,
                body,
                request.headers["Idempotency-Key"],
                actor_sub=auth.user_sub,
            )
            return JSONResponse(
                result, status_code=201, headers={"ETag": f'"{result["revision"]}"'}
            )
        except ValidationError:
            return _failure(UploadRejected("VALIDATION_ERROR"))
        except UploadRejected as exc:
            return _failure(exc)

    read_contract = {
        "x-minimum-role": "viewer",
        "x-idempotency-required": False,
        "x-rate-limit": "120/min/user",
    }

    @router.get(
        "/v1/documents",
        response_model=DocumentPage,
        operation_id="documents_list",
        openapi_extra=read_contract,
    )
    def list_documents(
        request: Request,
        cursor: str | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ):
        auth = authorize(request, "documents_list")
        if isinstance(auth, JSONResponse):
            return auth
        try:
            return JSONResponse(
                service.list_documents(auth.tenant_id, cursor=cursor, limit=limit, now=now_fn()),
                headers={"Cache-Control": "no-store"},
            )
        except UploadRejected as exc:
            return _failure(exc)

    @router.get(
        "/v1/documents/{document_id}",
        response_model=Document,
        operation_id="document_get",
        openapi_extra=read_contract,
    )
    def get_document(request: Request, document_id: UUID):
        auth = authorize(request, "document_get")
        if isinstance(auth, JSONResponse):
            return auth
        try:
            result = service.get_document(auth.tenant_id, str(document_id))
            return JSONResponse(result, headers={"ETag": f'"{result["revision"]}"'})
        except UploadRejected as exc:
            return _failure(exc)

    @router.get(
        "/v1/documents/{document_id}/versions",
        response_model=DocumentVersionPage,
        operation_id="versions_list",
        openapi_extra=read_contract,
    )
    def list_versions(
        request: Request,
        document_id: UUID,
        cursor: str | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ):
        auth = authorize(request, "versions_list")
        if isinstance(auth, JSONResponse):
            return auth
        try:
            return JSONResponse(
                service.list_versions(
                    auth.tenant_id,
                    str(document_id),
                    cursor=cursor,
                    limit=limit,
                    now=now_fn(),
                ),
                headers={"Cache-Control": "no-store"},
            )
        except UploadRejected as exc:
            return _failure(exc)

    @router.post(
        "/v1/documents/{document_id}/versions",
        status_code=201,
        response_model=UploadTicket,
        operation_id="version_create",
        openapi_extra=_request_schema(VersionCreate),
    )
    async def initiate_upload(request: Request, document_id: UUID):
        auth = authorize(request, "version_create", True)
        if isinstance(auth, JSONResponse):
            return auth
        try:
            body = await payload(request, VersionCreate)
            result = await run_in_threadpool(
                service.initiate_upload,
                auth.tenant_id,
                str(document_id),
                body,
                request.headers["Idempotency-Key"],
                actor_sub=auth.user_sub,
            )
            return JSONResponse(result, status_code=201)
        except ValidationError:
            return _failure(UploadRejected("VALIDATION_ERROR"))
        except UploadRejected as exc:
            return _failure(exc)

    @router.post("/local/uploads/{upload_id}/content", include_in_schema=False)
    async def receive_content(request: Request, upload_id: UUID):
        auth = authorize(request, write=True)
        if isinstance(auth, JSONResponse):
            return auth
        try:
            ticket = service.check_receipt(auth.tenant_id, str(upload_id))
            maximum = service.limits.max_bytes + 65536  # bounded multipart envelope overhead
            size_header = request.headers.get("content-length")
            if size_header is not None and not 0 <= int(size_header) <= maximum:
                raise UploadRejected("UPLOAD_LIMIT_EXCEEDED")
            received = 0

            async def bounded_receive():
                nonlocal received
                message = await request.receive()
                received += len(message.get("body", b""))
                if received > maximum:
                    # Starlette closes its temporary files on MultiPartException.
                    raise MultiPartException("UPLOAD_LIMIT_EXCEEDED")
                return message

            bounded_request = Request(request.scope, receive=bounded_receive)
            async with bounded_request.form(max_files=1, max_fields=1, max_part_size=512) as form:
                if len(form.multi_items()) != 2 or set(form) != {"ticket", "file"}:
                    raise UploadRejected("VALIDATION_ERROR")
                token, file = form["ticket"], form["file"]
                if not isinstance(token, str) or not secrets.compare_digest(
                    token.encode(), ticket["post_fields"]["ticket"].encode()
                ):
                    return _error_response(
                        403, "UPLOAD_TICKET_INVALID", "업로드 티켓을 확인하세요."
                    )
                if not isinstance(file, UploadFile) or file.content_type != "application/pdf":
                    raise UploadRejected("PDF_INVALID")
                content = await file.read(service.limits.max_bytes + 1)
                await run_in_threadpool(
                    service.receive_content,
                    auth.tenant_id,
                    str(upload_id),
                    content,
                    file.content_type,
                )
            return Response(status_code=204)
        except UploadRejected as exc:
            return _failure(exc)
        except (HTTPException, MultiPartException, ValueError):
            return _failure(UploadRejected("VALIDATION_ERROR"))

    @router.post(
        "/v1/uploads/{upload_id}/complete",
        status_code=202,
        response_model=JobAccepted,
        operation_id="upload_complete",
        openapi_extra=_request_schema(UploadComplete),
    )
    async def complete_upload(request: Request, upload_id: UUID):
        auth = authorize(request, "upload_complete", True)
        if isinstance(auth, JSONResponse):
            return auth
        try:
            body = await payload(request, UploadComplete)
            result = await run_in_threadpool(
                service.complete_upload,
                auth.tenant_id,
                str(upload_id),
                body,
                request.headers["Idempotency-Key"],
                actor_sub=auth.user_sub,
            )
            return JSONResponse(
                dict(
                    job_id=service.validation_job(auth.tenant_id, str(upload_id))["job_id"],
                    resource_id=result["version_id"],
                    status="ready",
                    status_url=f"/v1/versions/{result['version_id']}",
                ),
                status_code=202,
            )
        except ValidationError:
            return _failure(UploadRejected("VALIDATION_ERROR"))
        except UploadRejected as exc:
            return _failure(exc)

    @router.get(
        "/v1/versions/{version_id}",
        response_model=DocumentVersion,
        operation_id="version_get",
        openapi_extra={
            "x-minimum-role": "viewer",
            "x-idempotency-required": False,
            "x-rate-limit": "120/min/user",
        },
    )
    def get_version(request: Request, version_id: UUID):
        auth = authorize(request, "version_get")
        if isinstance(auth, JSONResponse):
            return auth
        try:
            return JSONResponse(service.get_version(auth.tenant_id, str(version_id)))
        except UploadRejected as exc:
            return _failure(exc)

    return router
