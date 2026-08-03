from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

from agent_memory_service.auth import InMemoryTokenStore, TokenService
from agent_memory_service.http import create_http_app
from agent_memory_service.memory import MemoryModule
from agent_memory_service.models import PrincipalKind, TenantSession
from agent_memory_service.pseudonyms import TelemetryPseudonymizer
from agent_memory_service.stores.memory import InMemoryTenantMemoryRouter
from agent_memory_service.telemetry import (
    ContentSafeTracer,
    SafeTelemetry,
    start_metrics_server,
)
from agent_memory_service.worker import _log

_TELEMETRY_KEY = b"0123456789abcdef0123456789abcdef"


def _pseudonymizer() -> TelemetryPseudonymizer:
    return TelemetryPseudonymizer(_TELEMETRY_KEY)


def _session() -> TenantSession:
    return TenantSession(
        tenant_id="tenant-product-a",
        actor_id="user-alice",
        actor_kind=PrincipalKind.USER,
        roles=frozenset({"tenant_member"}),
        token_id="secret-token-id",
    )


def test_shadow_request_threshold_records_signal_without_rejecting() -> None:
    telemetry = SafeTelemetry(
        pseudonymizer=_pseudonymizer(),
        token_requests_per_minute=2,
        tenant_requests_per_minute=3,
    )

    for _ in range(4):
        telemetry.observe_authenticated_request(_session())

    metrics = telemetry.render_metrics()
    assert 'limit_kind="token_requests_per_minute"' in metrics
    assert 'limit_kind="tenant_requests_per_minute"' in metrics
    assert "429" not in metrics


@pytest.mark.asyncio
async def test_concurrency_is_informative_and_telemetry_never_contains_domain_content() -> None:
    telemetry = SafeTelemetry(pseudonymizer=_pseudonymizer(), embedding_concurrency=1)

    async with telemetry.operation("embedding_search", _session()):
        async with telemetry.operation("embedding_search", _session()):
            pass

    metrics = telemetry.render_metrics()
    assert 'limit_kind="embedding_concurrency"' in metrics
    assert "sensitive memory content" not in metrics
    assert "secret-token-id" not in metrics
    assert "user-alice" not in metrics


def test_telemetry_pseudonym_is_a_stable_namespaced_keyed_hmac() -> None:
    pseudonymizer = _pseudonymizer()
    value = pseudonymizer.reference("tenant", "tenant-product-a")

    assert value == "o_c9676991a3bd6a87"
    assert value == pseudonymizer.reference("tenant", "tenant-product-a")
    assert value != pseudonymizer.reference("tenant", "tenant-product-b")
    assert value != pseudonymizer.reference("run", "tenant-product-a")
    assert value != TelemetryPseudonymizer(b"fedcba9876543210fedcba9876543210").reference(
        "tenant", "tenant-product-a"
    )
    assert "tenant-product-a" not in value


def test_telemetry_pseudonymizer_reads_only_a_regular_shared_key_file(tmp_path: Path) -> None:
    key_file = tmp_path / "telemetry_hmac_key"
    key_file.write_bytes(_TELEMETRY_KEY + b"\n")

    loaded = TelemetryPseudonymizer.from_file(key_file)

    assert loaded.reference("tenant", "tenant-product-a") == "o_c9676991a3bd6a87"
    assert _TELEMETRY_KEY.decode() not in repr(loaded)

    short_key = tmp_path / "short_key"
    short_key.write_text("predictable", encoding="utf-8")
    with pytest.raises(RuntimeError, match="at least 32 bytes"):
        TelemetryPseudonymizer.from_file(short_key)

    key_link = tmp_path / "key_link"
    key_link.symlink_to(key_file)
    with pytest.raises(RuntimeError, match="regular file"):
        TelemetryPseudonymizer.from_file(key_link)


def test_operational_metrics_cover_auth_routes_agents_workers_and_channels() -> None:
    telemetry = SafeTelemetry(pseudonymizer=_pseudonymizer())

    telemetry.observe_authentication("success", 0.012)
    telemetry.observe_authentication("invalid", 0.004)
    telemetry.observe_route("resolved")
    telemetry.set_dependency_health("control_postgres", True)
    telemetry.set_dependency_health("neo4j", False)
    telemetry.observe_agent_run(
        "agent-run-sensitive-id",
        "budget_exhausted",
        model_calls=3,
        tool_calls=2,
        events=8,
        cost_usd=0.2,
        budget_dimension="model_calls",
    )
    # Terminal Agent observations are idempotent per opaque run reference.
    telemetry.observe_agent_run(
        "agent-run-sensitive-id",
        "budget_exhausted",
        model_calls=3,
        budget_dimension="model_calls",
    )
    telemetry.observe_outbox("claimed", lag_seconds=12.5)
    telemetry.observe_outbox("dispatched", count=2)
    telemetry.observe_worker("cycle_success")
    telemetry.observe_telegram("accepted", "remember")
    telemetry.set_unused_token_signal("never_used", 4)
    telemetry.set_unused_token_signal("inactive_30d", 2)

    metrics = telemetry.render_metrics()

    for expected in (
        'memory_authentication_total{outcome="success"} 1',
        "memory_authentication_duration_seconds_sum",
        'memory_route_resolution_total{outcome="resolved"} 1',
        'memory_dependency_healthy{dependency="control_postgres"} 1',
        'memory_dependency_healthy{dependency="neo4j"} 0',
        'memory_agent_runs_total{outcome="budget_exhausted"} 1',
        'memory_agent_budget_exhaustions_total{dimension="model_calls"} 1',
        "memory_outbox_oldest_claim_seconds 12.500000",
        'memory_worker_cycles_total{outcome="cycle_success"} 1',
        'memory_telegram_updates_total{outcome="accepted",command="remember"} 1',
        'memory_unused_access_tokens{signal="never_used"} 4',
    ):
        assert expected in metrics
    assert "agent-run-sensitive-id" not in metrics


def test_logs_spans_and_metrics_drop_every_forbidden_payload_class(
    caplog: pytest.LogCaptureFixture,
) -> None:
    forbidden = (
        "mem1.token-id.raw-secret",
        "private memory bridge code 1234",
        "system prompt with confidential policy",
        "model output containing customer data",
        "telegram text from a human",
        "postgres-password-super-secret",
        "telegram-external-id-998877",
        "tenant-product-a",
        "user-alice",
    )
    exporter = InMemorySpanExporter()
    tracer = ContentSafeTracer(
        "memory-leakage-test",
        None,
        pseudonymizer=_pseudonymizer(),
        exporter=exporter,
    )
    telemetry = SafeTelemetry(pseudonymizer=_pseudonymizer(), tracer=tracer)
    telemetry.observe_authenticated_request(_session())
    telemetry.observe_authentication("success", 0.001)
    telemetry.observe_telegram("accepted", "remember")
    telemetry.set_unused_token_signal("never_used", 1)

    with tracer.span(
        forbidden[2],
        {
            "authorization": forbidden[0],
            "memory.content": forbidden[1],
            "prompt": forbidden[2],
            "output": forbidden[3],
            "telegram.text": forbidden[4],
            "database.password": forbidden[5],
            "external.identity": forbidden[6],
            "memory.tenant_ref": forbidden[7],
            "gen_ai.request.model": forbidden[3],
        },
    ):
        tracer.annotate_current({"memory.actor_kind": "user"})
    try:
        with tracer.span(
            "memory.test",
            {"memory.tenant_ref": _pseudonymizer().reference("tenant", "tenant-product-a")},
        ):
            raise RuntimeError(forbidden[1])
    except RuntimeError:
        pass

    caplog.set_level(logging.INFO, logger="memory.worker")
    _log(
        forbidden[2],
        token=forbidden[0],
        content=forbidden[1],
        prompt=forbidden[2],
        output=forbidden[3],
        telegram_text=forbidden[4],
        database_secret=forbidden[5],
        external_identity=forbidden[6],
    )

    spans = exporter.get_finished_spans()
    span_document = json.dumps(
        [
            {
                "name": span.name,
                "attributes": dict(span.attributes or {}),
                "events": [dict(event.attributes or {}) for event in span.events],
                "status": span.status.description,
            }
            for span in spans
        ],
        sort_keys=True,
    )
    exported = "\n".join((telemetry.render_metrics(), caplog.text, span_document))

    for value in forbidden:
        assert value not in exported
    assert "memory.operation" in span_document
    assert _pseudonymizer().reference("tenant", "tenant-product-a") in exported
    tracer.shutdown()


def test_fastapi_auto_spans_export_only_content_safe_operational_shape() -> None:
    exporter = InMemorySpanExporter()
    tracer = ContentSafeTracer(
        "memory-gateway",
        None,
        pseudonymizer=_pseudonymizer(),
        exporter=exporter,
    )
    app = FastAPI()

    @app.post("/api/v1/probe/{request_secret}")
    def probe(payload: dict[str, str]) -> dict[str, str]:
        return {"status": "ok", "received": payload["memory"]}

    tracer.instrument_fastapi(app)
    request_canaries = (
        "secret-hostname.example",
        "path-private-memory",
        "query-secret-token",
        "authorization-secret-token",
        "private-user-agent",
        "private-request-header",
        "private-request-body",
        "private-trace-state",
    )

    response = TestClient(app, base_url=f"https://{request_canaries[0]}").post(
        f"/api/v1/probe/{request_canaries[1]}?token={request_canaries[2]}",
        headers={
            "Authorization": f"Bearer {request_canaries[3]}",
            "User-Agent": request_canaries[4],
            "X-Request-Canary": request_canaries[5],
            "Traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
            "Tracestate": f"vendor={request_canaries[7]}",
        },
        json={"memory": request_canaries[6]},
    )

    assert response.status_code == 200
    exported = exporter.get_finished_spans()
    server_span = next(span for span in exported if span.kind is SpanKind.SERVER)
    exported_document = json.dumps(
        [
            {
                "name": span.name,
                "attributes": dict(span.attributes or {}),
                "events": [dict(event.attributes or {}) for event in span.events],
                "status": span.status.description,
                "resource": dict(span.resource.attributes),
                "trace_state": repr(span.context.trace_state) if span.context else None,
            }
            for span in exported
        ],
        sort_keys=True,
    )

    for canary in request_canaries:
        assert canary not in exported_document
    assert server_span.name == "http.server.request"
    assert server_span.attributes == {
        "http.request.method": "POST",
        "http.response.status_code": 200,
    }
    tracer.shutdown()


def test_production_otlp_pipeline_exports_sanitized_fastapi_spans() -> None:
    received: list[tuple[str, bytes]] = []

    class CollectorHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers["Content-Length"])
            received.append((self.path, self.rfile.read(length)))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    collector = ThreadingHTTPServer(("127.0.0.1", 0), CollectorHandler)
    collector_thread = Thread(target=collector.serve_forever, daemon=True)
    collector_thread.start()
    tracer = ContentSafeTracer(
        "memory-gateway",
        f"http://127.0.0.1:{collector.server_port}",
        pseudonymizer=_pseudonymizer(),
    )
    app = FastAPI()

    @app.post("/probe/{request_secret}")
    def probe(payload: dict[str, str]) -> dict[str, str]:
        return {"status": "ok", "received": payload["memory"]}

    tracer.instrument_fastapi(app)
    canaries = (
        "otlp-secret-hostname.example",
        "otlp-path-request-data",
        "otlp-query-token",
        "otlp-authorization-token",
        "otlp-private-user-agent",
        "otlp-private-request-body",
        "otlp-private-trace-state",
    )
    try:
        response = TestClient(app, base_url=f"https://{canaries[0]}").post(
            f"/probe/{canaries[1]}?token={canaries[2]}",
            headers={
                "Authorization": f"Bearer {canaries[3]}",
                "User-Agent": canaries[4],
                "Traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
                "Tracestate": f"vendor={canaries[6]}",
            },
            json={"memory": canaries[5]},
        )
        assert response.status_code == 200
    finally:
        tracer.shutdown()
        collector.shutdown()
        collector.server_close()
        collector_thread.join(timeout=5)

    assert len(received) == 1
    path, payload = received[0]
    assert path == "/v1/traces"
    export_request = ExportTraceServiceRequest.FromString(payload)
    exported_document = str(export_request)
    for canary in canaries:
        assert canary not in exported_document

    spans = [
        span
        for resource_spans in export_request.resource_spans
        for scope_spans in resource_spans.scope_spans
        for span in scope_spans.spans
    ]
    server_span = next(span for span in spans if span.name == "http.server.request")
    attributes = {
        attribute.key: getattr(attribute.value, attribute.value.WhichOneof("value"))
        for attribute in server_span.attributes
    }
    assert attributes == {
        "http.request.method": "POST",
        "http.response.status_code": 200,
    }


def test_content_safe_tracer_can_be_disabled_without_exporting() -> None:
    tracer = ContentSafeTracer("memory-test", None, pseudonymizer=_pseudonymizer())

    with tracer.span(
        "memory.test",
        {"memory.tenant_ref": _pseudonymizer().reference("tenant", "tenant-a")},
    ):
        tracer.annotate_current({"memory.actor_kind": "user"})

    assert tracer.provider is None


def test_http_authentication_records_success_invalid_missing_and_latency() -> None:
    token_store = InMemoryTokenStore()
    tokens = TokenService(token_store)
    credential = tokens.issue(_session(), lifetime=timedelta(days=1))
    telemetry = SafeTelemetry(pseudonymizer=_pseudonymizer())
    client = TestClient(
        create_http_app(
            MemoryModule(InMemoryTenantMemoryRouter(["tenant-product-a"])),
            tokens,
            telemetry=telemetry,
        )
    )

    missing = client.get("/api/v1/memories")
    invalid = client.get("/api/v1/memories", headers={"Authorization": "Bearer mem1.invalid.token"})
    success = client.get(
        "/api/v1/memories",
        headers={"Authorization": f"Bearer {credential.access_token}"},
    )
    metrics = client.get("/metrics").text

    assert missing.status_code == 401
    assert invalid.status_code == 401
    assert success.status_code == 200
    assert 'memory_authentication_total{outcome="missing"} 1' in metrics
    assert 'memory_authentication_total{outcome="invalid"} 1' in metrics
    assert 'memory_authentication_total{outcome="success"} 1' in metrics
    assert "memory_authentication_duration_seconds_sum" in metrics
    assert credential.access_token not in metrics


@pytest.mark.asyncio
async def test_internal_metrics_server_exposes_only_safe_prometheus_data(
    monkeypatch: Any,
) -> None:
    telemetry = SafeTelemetry(pseudonymizer=_pseudonymizer())
    telemetry.observe_authenticated_request(_session())

    class RecordingWriter:
        def __init__(self) -> None:
            self.body = b""

        def write(self, data: bytes) -> None:
            self.body += data

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    writer = RecordingWriter()

    async def fake_start_server(
        handler: Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]],
        *,
        host: str,
        port: int,
    ) -> asyncio.Server:
        assert host == "127.0.0.1"
        assert port == 0
        reader = asyncio.StreamReader()
        reader.feed_data(b"GET /metrics HTTP/1.1\r\nHost: worker\r\n\r\n")
        reader.feed_eof()
        await handler(reader, cast(asyncio.StreamWriter, writer))
        return cast(asyncio.Server, object())

    monkeypatch.setattr("agent_memory_service.telemetry.asyncio.start_server", fake_start_server)
    await start_metrics_server(telemetry, host="127.0.0.1", port=0)
    response = writer.body

    assert b"HTTP/1.1 200 OK" in response
    assert b"memory_authenticated_requests_total" in response
    assert b"secret-token-id" not in response
    assert b"tenant-product-a" not in response
