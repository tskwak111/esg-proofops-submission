"""ASGI lifecycle observations and a runtime config that never formats raw messages."""

from __future__ import annotations

from contextvars import ContextVar
from time import perf_counter
from typing import Any

from proofops.application.telemetry import Telemetry, TraceContext, traceparent
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_request_context: ContextVar[TraceContext | None] = ContextVar("telemetry_context", default=None)


def current_request_id() -> str | None:
    context = _request_context.get()
    return context.request_id if context is not None else None


class TelemetryMiddleware:
    def __init__(self, app: ASGIApp, *, telemetry: Telemetry) -> None:
        self.app, self.telemetry = app, telemetry

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # External headers/paths are untrusted, including syntactically valid identifiers.
        context = TraceContext.new()
        scope.setdefault("state", {})["telemetry_context"] = context
        token = _request_context.set(context)
        started, status, code = perf_counter(), 500, "OK"

        async def observed_send(message: Message) -> None:
            nonlocal status, code
            if message["type"] == "http.response.start":
                status = message["status"]
                if status >= 400 and code == "OK":
                    code = "UNKNOWN"
                headers = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k.lower() not in (b"x-request-id", b"traceparent")
                ]
                headers.extend(
                    [
                        (b"x-request-id", context.request_id.encode()),
                        (b"traceparent", traceparent(context).encode()),
                    ]
                )
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, observed_send)
        except BaseException:
            code = "INTERNAL_ERROR"
            raise
        finally:
            try:
                # Authenticated handlers enrich context using dataclasses.replace.
                enriched = scope["state"].get("telemetry_context")
                if isinstance(enriched, TraceContext) and (
                    enriched.request_id,
                    enriched.trace_id,
                    enriched.span_id,
                ) == (context.request_id, context.trace_id, context.span_id):
                    context = enriched
                self.telemetry.emit(
                    {
                        "event": "api_request",
                        "stage": "API",
                        "code": code,
                        "http_status": status,
                        "latency_ms": (perf_counter() - started) * 1000,
                    },
                    context=context,
                )
            finally:
                _request_context.reset(token)


def logging_config(*, env: str, service: str = "api") -> dict[str, Any]:
    """Pass to uvicorn.run(log_config=..., access_log=False); also usable by workers."""
    return {
        "version": 1,
        "disable_existing_loggers": True,
        "formatters": {
            "safe": {
                "()": "proofops.application.telemetry.SafeRuntimeFormatter",
                "service": service,
                "env": env,
            }
        },
        "handlers": {
            "safe": {
                "class": "proofops.application.telemetry._QuietHandler",
                "formatter": "safe",
                "stream": "ext://sys.stderr",
            },
            "discard": {"class": "logging.NullHandler"},
        },
        "root": {"handlers": ["safe"], "level": "INFO"},
        "loggers": {
            "uvicorn": {"handlers": ["safe"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"handlers": ["safe"], "level": "INFO", "propagate": False},
            "uvicorn.access": {"handlers": ["discard"], "level": "CRITICAL", "propagate": False},
        },
    }
