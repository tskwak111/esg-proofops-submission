"""AT-035: synthetic secret/body fixtures through actual logging/ASGI/worker paths."""

import io
import json
import logging
from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient
from proofops.application.telemetry import (
    SafeRuntimeFormatter,
    Telemetry,
    TraceContext,
    emit_sanitized_event,
    queue_trace,
)
from proofops_api.telemetry import TelemetryMiddleware, logging_config
from proofops_worker.telemetry import observe_job, worker_context

API_KEY = "sk-synthetic-KEY-MUST-NOT-APPEAR"
BODY = "기밀 공시 본문 ONLY_SYNTHETIC_DOCUMENT_TEXT"
TENANT, RUN, JOB = (str(UUID(int=i)) for i in range(1, 4))


def CLOCK():
    return datetime(2026, 9, 9, tzinfo=UTC)


def setup(service="api"):
    stream = io.StringIO()
    emitter = Telemetry(
        service=service,
        env="test",
        stream=stream,
        hash_key=b"synthetic-test-key-32-bytes-long!!",
        clock=CLOCK,
    )
    return emitter, stream


def rows(stream):
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_collected_logs_metrics_and_traces_exclude_secrets_and_untrusted_values():
    emitter, stream = setup()
    context = TraceContext.new(
        tenant_id=TENANT,
        run_id=RUN,
        job_id=JOB,
        model_binding_hash="a" * 64,
        request_signature="b" * 64,
    )
    event = {
        "event": "model_call",
        "stage": "TAG",
        "code": "MODEL_THROTTLED",
        "role": "tagger",
        "input_tokens": 12,
        "output_tokens": 3,
        "cached_tokens": 4,
        "latency_ms": 25,
        "api_key": API_KEY,
        "body": BODY,
        "request_id": API_KEY,
        "trace_id": API_KEY,
        "model_binding_hash": API_KEY,
        "provider_request_id": API_KEY,
        "message": BODY,
        "exception": RuntimeError(BODY),
        "headers": {"authorization": f"Bearer {API_KEY}"},
        "nested": [{"prompt": BODY, "secret": API_KEY}],
        "url": f"/?code={API_KEY}",
    }
    result = emit_sanitized_event(emitter, event, context=context)
    text = stream.getvalue()
    assert API_KEY not in text and BODY not in text and TENANT not in text
    assert result["trace_id"] == context.trace_id and result["request_id"] == context.request_id
    assert result["model_binding_hash"] == "a" * 64
    assert result["provider_request_id"] != API_KEY
    assert result["input_tokens"] == 12
    metrics = [row for row in rows(stream) if row["kind"] == "metric"]
    assert any(row["name"] == "model_tokens" and row["value"] == 15 for row in metrics)
    assert all(set(row["labels"]) == {"env", "stage", "error_code"} for row in metrics)
    assert any(
        row["kind"] == "trace" and row["trace_id"] == context.trace_id for row in rows(stream)
    )
    assert event["body"] == BODY  # no caller mutation or destruction of source data


def test_allowed_names_cannot_smuggle_text_nonfinite_or_nested_values():
    emitter, stream = setup()
    result = emitter.emit(
        {
            "event": API_KEY,
            "stage": BODY,
            "code": API_KEY,
            "role": BODY,
            "input_tokens": True,
            "output_tokens": -1,
            "latency_ms": float("nan"),
            "request_signature": "c" * 64,
            "job_id": API_KEY,
            "fencing_token": {"message": BODY},
        },
        context=TraceContext.new(),
    )
    assert API_KEY not in stream.getvalue() and BODY not in stream.getvalue()
    assert result["event"] == "unknown" and result["stage"] == "UNKNOWN"
    assert result["code"] == "UNKNOWN"
    assert result["request_signature"] is None
    assert result["input_tokens"] is None and result["output_tokens"] is None
    assert result["latency_ms"] is None


def test_runtime_formatter_never_formats_message_args_or_exception():
    stream = io.StringIO()
    logger = logging.Logger("synthetic-third-party", logging.DEBUG)
    handler = logging.StreamHandler(stream)
    handler.setFormatter(SafeRuntimeFormatter(service="api", env="test"))
    logger.addHandler(handler)
    try:
        raise RuntimeError(f"{API_KEY} {BODY}")
    except RuntimeError:
        logger.exception(
            "GET /v1/auth/callback?code=%s %s",
            API_KEY,
            BODY,
            extra={"prompt": BODY, "authorization": API_KEY},
            stack_info=True,
        )
    assert API_KEY not in stream.getvalue() and BODY not in stream.getvalue()
    assert json.loads(stream.getvalue())["code"] == "INTERNAL_ERROR"


def test_real_asgi_lifecycle_never_logs_headers_query_body_or_errors():
    emitter, stream = setup()
    app = FastAPI()
    app.add_middleware(TelemetryMiddleware, telemetry=emitter)

    @app.get("/v1/runs/{run_id}/cost")
    def cost(request: Request, run_id: str):
        assert isinstance(request.state.telemetry_context, TraceContext)
        return {"cost_status": "unknown_cost", "amount": None}

    @app.post("/failure")
    async def failure(request: Request):
        payload = await request.body()
        raise RuntimeError(payload.decode())

    @app.get("/unavailable")
    def unavailable():
        return Response(status_code=503)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(
            f"/v1/runs/{RUN}/cost?code={API_KEY}",
            headers={
                "Authorization": f"Bearer {API_KEY}",
                "X-Request-ID": API_KEY,
                "traceparent": API_KEY,
            },
        )
        assert response.status_code == 200
        assert str(UUID(response.headers["x-request-id"])) == response.headers["x-request-id"]
        assert client.get("/unavailable").status_code == 503
        assert client.post("/failure", content=f"{API_KEY} {BODY}").status_code == 500
    assert API_KEY not in stream.getvalue() and BODY not in stream.getvalue()
    logs = [row for row in rows(stream) if row["kind"] == "log"]
    assert [row["http_status"] for row in logs] == [200, 503, 500]
    assert len({row["trace_id"] for row in logs}) == 3
    assert logs[1]["code"] != "OK"
    assert logs[-1]["code"] == "INTERNAL_ERROR"


def test_worker_hook_preserves_result_failure_and_trace_without_source_logging(tmp_path):
    from proofops.adapters.local.job_store import LocalSQLiteJobStore
    from proofops.application.ports.jobs import JobMessage
    from proofops_worker.consumer import StageFailure

    store = LocalSQLiteJobStore(tmp_path / "jobs.sqlite")
    version = str(UUID(int=4))
    store.create_run(TENANT, RUN, version)
    message = JobMessage(TENANT, RUN, version, JOB, "TAG", "replica-1", "a" * 64)
    store.enqueue(message, now=0)
    lease = store.claim_job(message, owner="synthetic-worker", now=0, lease_seconds=10)
    assert lease is not None
    parent = TraceContext.new(tenant_id=TENANT, run_id=RUN)
    propagated = queue_trace(parent)
    assert set(propagated) == {"traceparent", "run_id"}
    context = worker_context(lease, propagated)
    assert context.trace_id == parent.trace_id and context.parent_span_id == parent.span_id
    emitter, stream = setup("worker")
    payload = (BODY + API_KEY).encode()
    assert observe_job(
        emitter, lease, lambda _: (payload, {"input_tokens": 7, "prompt": BODY}), context=context
    ) == (payload, {"input_tokens": 7, "prompt": BODY})
    failure = StageFailure(API_KEY, usage={"input_tokens": 2, "response": BODY})

    def fail(_):
        raise failure

    with pytest.raises(StageFailure) as captured:
        observe_job(emitter, lease, fail, context=context)
    assert captured.value is failure
    assert API_KEY not in stream.getvalue() and BODY not in stream.getvalue()
    assert {row["fencing_token"] for row in rows(stream) if row["kind"] == "log"} == {1}
    with pytest.raises(ValueError):
        worker_context(lease, {**propagated, "run_id": str(UUID(int=9))})


def test_runtime_logging_configuration_in_real_subprocess():
    import subprocess
    import sys

    script = """
import logging, logging.config
from proofops_api.telemetry import logging_config
logging.config.dictConfig(logging_config(env="test"))
logging.getLogger("uvicorn.access").info(
    "GET /v1/auth/callback?code=sk-synthetic-KEY-MUST-NOT-APPEAR")
try:
    raise RuntimeError("기밀 공시 본문 ONLY_SYNTHETIC_DOCUMENT_TEXT")
except RuntimeError:
    logging.getLogger("uvicorn.error").exception("sk-synthetic-KEY-MUST-NOT-APPEAR")
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    collected = result.stdout + result.stderr
    assert API_KEY not in collected and BODY not in collected
    assert json.loads(collected)["code"] == "INTERNAL_ERROR"
    assert logging_config(env="test")["loggers"]["uvicorn.access"]["propagate"] is False


def test_sink_failure_never_prints_exception_or_changes_operation(capsys):
    class BrokenStream(io.StringIO):
        def write(self, value):
            raise OSError(API_KEY + BODY)

    emitter = Telemetry(
        service="api",
        env="test",
        stream=BrokenStream(),
        hash_key=b"synthetic-test-key-32-bytes-long!!",
        clock=CLOCK,
    )
    emitter.emit({"event": "api_request", "stage": "API", "code": "OK"}, context=TraceContext.new())
    assert emitter.dropped_events > 0
    assert capsys.readouterr().err == ""


def test_critical_access_log_is_not_forwarded_to_logging_last_resort():
    import subprocess
    import sys

    script = f"""
import logging, logging.config
from proofops_api.telemetry import logging_config
logging.config.dictConfig(logging_config(env="test"))
logging.getLogger("uvicorn.access").critical({API_KEY!r})
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0
    assert API_KEY not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "field,value",
    [("trace_id", None), ("span_id", None), ("request_id", None), ("trace_id", "0" * 32)],
)
def test_invalid_server_context_is_rejected(field, value):
    with pytest.raises(ValueError):
        TraceContext.new(**{field: value})


def test_operational_metrics_keep_only_bounded_counts_and_low_cardinality_labels():
    emitter, stream = setup("worker")
    emitter.emit(
        {
            "event": "operation_completed",
            "stage": "PARSE",
            "code": "OK",
            "parser_page_count": 7,
            "unreadable_count": 2,
            "cost_unknown": 1,
            "grade_counts": BODY,
            "review_age": float("inf"),
            "metric_labels": {"claim_id": API_KEY},
        },
        context=TraceContext.new(),
    )
    metrics = {row["name"]: row for row in rows(stream) if row["kind"] == "metric"}
    assert metrics["parser_page_count"]["value"] == 7
    assert metrics["unreadable_count"]["value"] == 2
    assert metrics["cost_unknown"]["value"] == 1
    assert "grade_counts" not in metrics and "review_age" not in metrics
    assert all(set(row["labels"]) == {"env", "stage", "error_code"} for row in metrics.values())
    assert API_KEY not in stream.getvalue() and BODY not in stream.getvalue()


def test_server_route_can_enrich_context_after_authorization():
    from dataclasses import replace

    emitter, stream = setup()
    app = FastAPI()
    app.add_middleware(TelemetryMiddleware, telemetry=emitter)

    @app.get("/authorized")
    def authorized(request: Request):
        # Synthetic trusted server identity, standing in for completed auth/resource lookup.
        request.state.telemetry_context = replace(
            request.state.telemetry_context, tenant_id=TENANT, run_id=RUN
        )
        return {"ok": True}

    with TestClient(app) as client:
        assert client.get("/authorized").status_code == 200
    log = next(row for row in rows(stream) if row["kind"] == "log")
    assert log["run_id"] == RUN and log["tenant_hash"] is not None
    assert TENANT not in stream.getvalue()


def test_concurrent_requests_and_threadpool_keep_request_correlation_isolated():
    import asyncio

    import httpx
    from proofops_api.telemetry import current_request_id

    emitter, stream = setup()
    app = FastAPI()
    app.add_middleware(TelemetryMiddleware, telemetry=emitter)

    @app.get("/async")
    async def async_request():
        before = current_request_id()
        await asyncio.sleep(0)
        assert before == current_request_id()
        return {"request_id": current_request_id()}

    @app.get("/threadpool")
    def threadpool_request():
        return {"request_id": current_request_id()}

    async def exercise():
        assert current_request_id() is None
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://synthetic.test"
        ) as client:
            responses = await asyncio.gather(
                *(client.get(path) for path in ["/async"] * 8 + ["/threadpool"] * 4)
            )
        assert current_request_id() is None
        ids = [response.headers["x-request-id"] for response in responses]
        assert len(set(ids)) == 12
        assert all(
            response.json()["request_id"] == response.headers["x-request-id"]
            for response in responses
        )
        assert set(ids) == {row["request_id"] for row in rows(stream) if row["kind"] == "log"}

    asyncio.run(exercise())


def test_runtime_sink_failure_cannot_dump_original_log_record():
    import subprocess
    import sys

    script = f"""
import io, logging, logging.config
from proofops_api.telemetry import logging_config
class BrokenStream(io.StringIO):
    def write(self, value):
        raise OSError({BODY!r})
logging.config.dictConfig(logging_config(env="test"))
logger = logging.getLogger("uvicorn.error")
logger.handlers[0].setStream(BrokenStream())
logger.error({API_KEY!r})
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0
    assert result.stdout + result.stderr == ""
