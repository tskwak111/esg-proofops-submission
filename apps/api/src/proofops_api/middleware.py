"""Browser-facing security checks shared by state-changing API routes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from threading import Lock
from typing import Any

from fastapi import Request
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_JSON_BODY_BYTES = 65_536

# PDFium is process-global and unsafe even when threads read different documents.
# Keep this outside pinned verifier modules so their source hashes remain valid.
_local_pdf_request_lock = Lock()
_inflight_pdf_requests: set[asyncio.Task[None]] = set()


def _finish_pdf_request(task: asyncio.Task[None]) -> None:
    _inflight_pdf_requests.discard(task)
    if not task.cancelled():
        task.exception()  # Retrieve failures even when the HTTP caller disconnected.


class LocalPdfRequestMiddleware:
    """Protect every local API path, including frozen readers and request cleanup.

    ponytail: one HTTP request per process; use isolated PDF worker processes when
    concurrent service throughput is required. The event loop remains responsive.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # Nonblocking admission is safe across event loops and cancellation while
        # queued; a cancelled waiter cannot acquire and accidentally strand the lock.
        while not _local_pdf_request_lock.acquire(blocking=False):
            await asyncio.sleep(0.01)

        async def admitted() -> None:
            try:
                await self.app(scope, receive, send)
            finally:
                _local_pdf_request_lock.release()

        # A disconnected caller cannot cancel a running native C function.
        # Keep admission until the inner request (including its worker) finishes.
        task = asyncio.create_task(admitted())
        _inflight_pdf_requests.add(task)
        task.add_done_callback(_finish_pdf_request)
        await asyncio.shield(task)


class RequestBodyTooLarge(ValueError):
    """The received JSON body exceeded the local API transport ceiling."""


async def read_bounded_json(request: Request, *, max_bytes: int = MAX_JSON_BODY_BYTES) -> Any:
    """Receive and decode JSON without allowing an unbounded in-memory body."""
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            declared_length = None
        if declared_length is not None and declared_length > max_bytes:
            raise RequestBodyTooLarge
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > max_bytes:
            raise RequestBodyTooLarge
        data.extend(chunk)
    try:
        return json.loads(bytes(data))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("invalid JSON body") from exc


CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'; "
    "object-src 'none'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' blob: data:; "
    "connect-src 'self'; "
    "frame-src 'self' blob:"
)


def verify_csrf(
    *,
    origin: str | None,
    csrf_token: str | None,
    csrf_hash: str,
    allowed_origin: str | None,
) -> bool:
    """Require an exact Origin match and constant-time CSRF hash comparison."""
    if not allowed_origin or not origin or not csrf_token or origin != allowed_origin:
        return False
    presented_hash = hashlib.sha256(csrf_token.encode("utf-8")).hexdigest()
    return secrets.compare_digest(presented_hash, csrf_hash)


class BrowserSecurityHeadersMiddleware:
    """Attach the fixed CSP to every HTTP response."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_csp(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
                if headers.get("Content-Type", "").partition(";")[0] == "application/json":
                    headers["Cache-Control"] = "no-store"
            await send(message)

        await self.app(scope, receive, send_with_csp)
