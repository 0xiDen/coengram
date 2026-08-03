"""Content-safe metrics and iteration-1 informational shadow limits."""

from __future__ import annotations

import asyncio
import re
import threading
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import SpanContext, SpanKind, TraceState
from opentelemetry.trace.status import Status

from agent_memory_service.models import TenantSession
from agent_memory_service.pseudonyms import TelemetryPseudonymizer

_AUTH_OUTCOMES = frozenset({"success", "missing", "invalid", "error"})
_ROUTE_OUTCOMES = frozenset({"resolved", "unavailable", "error"})
_DEPENDENCIES = frozenset(
    {
        "control_postgres",
        "tenant_postgres",
        "neo4j",
        "rabbitmq",
        "anthropic",
        "telegram",
    }
)
_AGENT_OUTCOMES = frozenset(
    {
        "queued",
        "running",
        "cancel_requested",
        "cancelled",
        "completed",
        "budget_exhausted",
        "failed",
    }
)
_BUDGET_DIMENSIONS = frozenset({"model_calls", "tool_calls", "events", "seconds", "cost_usd"})
_OUTBOX_OUTCOMES = frozenset({"claimed", "dispatched", "failed", "projection_retry"})
_WORKER_OUTCOMES = frozenset({"started", "stopped", "cycle_success", "cycle_failed"})
_TELEGRAM_OUTCOMES = frozenset({"accepted", "rejected", "failed"})
_TELEGRAM_COMMANDS = frozenset(
    {
        "whoami",
        "recall",
        "remember",
        "propose",
        "synthesize",
        "status",
        "cancel",
        "help",
    }
)
_TOKEN_SIGNALS = frozenset({"never_used", "inactive_30d"})
_SAFE_SPAN_ATTRIBUTES = frozenset(
    {
        "memory.tenant_ref",
        "memory.run_ref",
        "memory.actor_kind",
        "memory.agent.outcome",
        "memory.budget.dimension",
        "gen_ai.request.model",
        "agent.tool.name",
    }
)
_SAFE_SPAN_NAMES = frozenset(
    {
        "memory.embedding_search",
        "memory.synthesis",
        "activegraph.run",
        "activegraph.tool",
        "langchain.completion",
        "memory.test",
    }
)
_SAFE_TOOL_NAMES = frozenset(
    {
        "read_selected_private_memory",
        "read_tenant_knowledge",
        "propose_tenant_knowledge",
    }
)
_OPAQUE_REF = re.compile(r"^o_[0-9a-f]{16}$")
_SAFE_SERVICE_NAMES = frozenset({"memory-gateway", "memory-worker"})
_HTTP_METHODS = frozenset(
    {"CONNECT", "DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT", "TRACE"}
)


class SafeTelemetry:
    """Record operational shape without recording credentials or domain content."""

    def __init__(
        self,
        *,
        pseudonymizer: TelemetryPseudonymizer,
        token_requests_per_minute: int = 120,
        tenant_requests_per_minute: int = 600,
        embedding_concurrency: int = 10,
        synthesis_concurrency: int = 2,
        monotonic: Callable[[], float] = time.monotonic,
        tracer: ContentSafeTracer | None = None,
    ) -> None:
        self._pseudonymizer = pseudonymizer
        self._token_limit = token_requests_per_minute
        self._tenant_limit = tenant_requests_per_minute
        self._concurrency_limits = {
            "embedding_search": embedding_concurrency,
            "synthesis": synthesis_concurrency,
        }
        self._monotonic = monotonic
        self._lock = threading.RLock()
        self._token_windows: dict[str, deque[float]] = defaultdict(deque)
        self._tenant_windows: dict[str, deque[float]] = defaultdict(deque)
        self._request_counts: dict[str, int] = defaultdict(int)
        self._would_limit: dict[str, int] = defaultdict(int)
        self._concurrent: dict[str, int] = defaultdict(int)
        self._authentication_counts: dict[str, int] = defaultdict(int)
        self._authentication_latency_count: dict[str, int] = defaultdict(int)
        self._authentication_latency_sum: dict[str, float] = defaultdict(float)
        self._route_counts: dict[str, int] = defaultdict(int)
        self._dependency_health: dict[str, int] = {}
        self._agent_outcomes: dict[str, int] = defaultdict(int)
        self._agent_budget_exhaustions: dict[str, int] = defaultdict(int)
        self._agent_usage: dict[str, float] = defaultdict(float)
        self._observed_terminal_runs: set[str] = set()
        self._outbox_outcomes: dict[str, int] = defaultdict(int)
        self._outbox_lag_seconds = 0.0
        self._worker_outcomes: dict[str, int] = defaultdict(int)
        self._telegram_outcomes: dict[tuple[str, str], int] = defaultdict(int)
        self._unused_token_signals: dict[str, int] = {}
        self._tracer = tracer

    def observe_authentication(self, outcome: str, latency_seconds: float) -> None:
        _require_label(outcome, _AUTH_OUTCOMES, "authentication outcome")
        bounded_latency = max(0.0, float(latency_seconds))
        with self._lock:
            self._authentication_counts[outcome] += 1
            self._authentication_latency_count[outcome] += 1
            self._authentication_latency_sum[outcome] += bounded_latency

    def observe_route(self, outcome: str) -> None:
        _require_label(outcome, _ROUTE_OUTCOMES, "route outcome")
        with self._lock:
            self._route_counts[outcome] += 1

    def set_dependency_health(self, dependency: str, healthy: bool) -> None:
        _require_label(dependency, _DEPENDENCIES, "dependency")
        with self._lock:
            self._dependency_health[dependency] = int(healthy)

    def observe_agent_run(
        self,
        run_id: str,
        outcome: str,
        *,
        model_calls: int = 0,
        tool_calls: int = 0,
        events: int = 0,
        cost_usd: float = 0.0,
        budget_dimension: str | None = None,
    ) -> None:
        _require_label(outcome, _AGENT_OUTCOMES, "Agent outcome")
        if budget_dimension is not None:
            _require_label(budget_dimension, _BUDGET_DIMENSIONS, "Agent budget dimension")
        run_ref = self.reference("run", run_id)
        terminal = outcome in {"cancelled", "completed", "budget_exhausted", "failed"}
        with self._lock:
            if terminal and run_ref in self._observed_terminal_runs:
                return
            self._agent_outcomes[outcome] += 1
            if terminal:
                self._observed_terminal_runs.add(run_ref)
                self._agent_usage["model_calls"] += max(0, model_calls)
                self._agent_usage["tool_calls"] += max(0, tool_calls)
                self._agent_usage["events"] += max(0, events)
                self._agent_usage["cost_usd"] += max(0.0, cost_usd)
            if outcome == "budget_exhausted" and budget_dimension is not None:
                self._agent_budget_exhaustions[budget_dimension] += 1

    def observe_outbox(
        self,
        outcome: str,
        *,
        count: int = 1,
        lag_seconds: float | None = None,
    ) -> None:
        _require_label(outcome, _OUTBOX_OUTCOMES, "outbox outcome")
        with self._lock:
            self._outbox_outcomes[outcome] += max(0, count)
            if lag_seconds is not None:
                self._outbox_lag_seconds = max(0.0, float(lag_seconds))

    def observe_worker(self, outcome: str) -> None:
        _require_label(outcome, _WORKER_OUTCOMES, "worker outcome")
        with self._lock:
            self._worker_outcomes[outcome] += 1

    def observe_telegram(self, outcome: str, command: str = "help") -> None:
        _require_label(outcome, _TELEGRAM_OUTCOMES, "Telegram outcome")
        _require_label(command, _TELEGRAM_COMMANDS, "Telegram command")
        with self._lock:
            self._telegram_outcomes[(outcome, command)] += 1

    def set_unused_token_signal(self, signal: str, count: int) -> None:
        _require_label(signal, _TOKEN_SIGNALS, "unused-token signal")
        with self._lock:
            self._unused_token_signals[signal] = max(0, count)

    def observe_authenticated_request(self, session: TenantSession) -> None:
        now = self._monotonic()
        tenant = self.reference("tenant", session.tenant_id)
        token = self.reference("token", session.token_id or f"actor:{session.actor_id}")
        with self._lock:
            self._request_counts[tenant] += 1
            if self._record_window(self._token_windows[token], now) > self._token_limit:
                self._would_limit["token_requests_per_minute"] += 1
            if self._record_window(self._tenant_windows[tenant], now) > self._tenant_limit:
                self._would_limit["tenant_requests_per_minute"] += 1
        if self._tracer is not None:
            self._tracer.annotate_current(
                {
                    "memory.tenant_ref": tenant,
                    "memory.actor_kind": session.actor_kind.value,
                }
            )

    @asynccontextmanager
    async def operation(
        self,
        operation: str,
        session: TenantSession,
    ) -> AsyncIterator[None]:
        del session  # Identity is deliberately not attached to concurrency labels.
        try:
            limit = self._concurrency_limits[operation]
        except KeyError as exc:
            raise ValueError("Unknown telemetry operation") from exc
        with self._lock:
            self._concurrent[operation] += 1
            if self._concurrent[operation] > limit:
                self._would_limit[f"{operation.removesuffix('_search')}_concurrency"] += 1
        try:
            if self._tracer is None:
                yield
            else:
                with self._tracer.span(f"memory.{operation}"):
                    yield
        finally:
            with self._lock:
                self._concurrent[operation] -= 1

    def render_metrics(self) -> str:
        with self._lock:
            lines = [
                "# HELP memory_authenticated_requests_total Authenticated gateway requests.",
                "# TYPE memory_authenticated_requests_total counter",
            ]
            lines.extend(
                f'memory_authenticated_requests_total{{tenant="{tenant}"}} {count}'
                for tenant, count in sorted(self._request_counts.items())
            )
            lines.extend(
                (
                    "# HELP memory_shadow_would_limit_total Informational threshold signals; "
                    "requests are not rejected.",
                    "# TYPE memory_shadow_would_limit_total counter",
                )
            )
            lines.extend(
                f'memory_shadow_would_limit_total{{limit_kind="{kind}"}} {count}'
                for kind, count in sorted(self._would_limit.items())
            )
            lines.extend(
                (
                    "# HELP memory_concurrent_operations Current bounded operations.",
                    "# TYPE memory_concurrent_operations gauge",
                )
            )
            lines.extend(
                f'memory_concurrent_operations{{operation="{operation}"}} {count}'
                for operation, count in sorted(self._concurrent.items())
            )
            _append_counter(
                lines,
                "memory_authentication_total",
                "Authentication attempts by content-safe outcome.",
                "outcome",
                self._authentication_counts,
            )
            lines.extend(
                (
                    "# HELP memory_authentication_duration_seconds "
                    "Authentication latency by outcome.",
                    "# TYPE memory_authentication_duration_seconds summary",
                )
            )
            for outcome in sorted(self._authentication_latency_count):
                lines.append(
                    "memory_authentication_duration_seconds_count"
                    f'{{outcome="{outcome}"}} {self._authentication_latency_count[outcome]}'
                )
                lines.append(
                    "memory_authentication_duration_seconds_sum"
                    f'{{outcome="{outcome}"}} {self._authentication_latency_sum[outcome]:.9f}'
                )
            _append_counter(
                lines,
                "memory_route_resolution_total",
                "Tenant route resolution outcomes.",
                "outcome",
                self._route_counts,
            )
            _append_gauge(
                lines,
                "memory_dependency_healthy",
                "Current dependency health reported by application checks.",
                "dependency",
                self._dependency_health,
            )
            _append_counter(
                lines,
                "memory_agent_runs_total",
                "Agent Run outcomes.",
                "outcome",
                self._agent_outcomes,
            )
            _append_counter(
                lines,
                "memory_agent_budget_exhaustions_total",
                "Terminal Agent budget exhaustion dimensions.",
                "dimension",
                self._agent_budget_exhaustions,
            )
            _append_counter(
                lines,
                "memory_agent_usage_total",
                "Terminal Agent usage totals.",
                "dimension",
                self._agent_usage,
            )
            _append_counter(
                lines,
                "memory_outbox_operations_total",
                "Outbox relay and projection outcomes.",
                "outcome",
                self._outbox_outcomes,
            )
            lines.extend(
                (
                    "# HELP memory_outbox_oldest_claim_seconds "
                    "Age of the most recently claimed oldest event.",
                    "# TYPE memory_outbox_oldest_claim_seconds gauge",
                    f"memory_outbox_oldest_claim_seconds {self._outbox_lag_seconds:.6f}",
                )
            )
            _append_counter(
                lines,
                "memory_worker_cycles_total",
                "Worker lifecycle and Tenant cycle outcomes.",
                "outcome",
                self._worker_outcomes,
            )
            lines.extend(
                (
                    "# HELP memory_telegram_updates_total "
                    "Telegram outcomes without message or external identity data.",
                    "# TYPE memory_telegram_updates_total counter",
                )
            )
            lines.extend(
                f'memory_telegram_updates_total{{outcome="{outcome}",command="{command}"}} {count}'
                for (outcome, command), count in sorted(self._telegram_outcomes.items())
            )
            _append_gauge(
                lines,
                "memory_unused_access_tokens",
                "Active Access Tokens that have never been used or are inactive for 30 days.",
                "signal",
                self._unused_token_signals,
            )
        return "\n".join(lines) + "\n"

    def reference(self, namespace: str, value: str) -> str:
        """Return a content-safe reference using this process's shared telemetry key."""

        return self._pseudonymizer.reference(namespace, value)

    @staticmethod
    def _record_window(window: deque[float], now: float) -> int:
        cutoff = now - 60.0
        while window and window[0] <= cutoff:
            window.popleft()
        window.append(now)
        return len(window)


class ContentSafeTracer:
    """Emit operational spans while accepting only explicitly safe attributes."""

    def __init__(
        self,
        service_name: str,
        endpoint: str | None,
        *,
        pseudonymizer: TelemetryPseudonymizer,
        exporter: SpanExporter | None = None,
    ) -> None:
        self._pseudonymizer = pseudonymizer
        self._provider: TracerProvider | None = None
        if endpoint is None and exporter is None:
            self._tracer = trace.NoOpTracerProvider().get_tracer(service_name)
            return
        provider = TracerProvider(
            resource=Resource.create({"service.name": service_name}),
        )
        if exporter is not None:
            provider.add_span_processor(SimpleSpanProcessor(_ContentSafeSpanExporter(exporter)))
        if endpoint is not None:
            normalized = endpoint.strip().rstrip("/")
            if not normalized.startswith(("http://", "https://")):
                raise ValueError("OpenTelemetry endpoint must be HTTP or HTTPS")
            provider.add_span_processor(
                BatchSpanProcessor(
                    _ContentSafeSpanExporter(OTLPSpanExporter(endpoint=f"{normalized}/v1/traces"))
                )
            )
        self._provider = provider
        self._tracer = provider.get_tracer("agent_memory_service")

    @property
    def provider(self) -> TracerProvider | None:
        return self._provider

    def reference(self, namespace: str, value: str) -> str:
        """Return a content-safe reference using the process's telemetry key."""

        return self._pseudonymizer.reference(namespace, value)

    @contextmanager
    def span(
        self,
        name: str,
        attributes: dict[str, str | int | float | bool] | None = None,
    ) -> Iterator[None]:
        with self._tracer.start_as_current_span(
            _safe_span_name(name),
            attributes=_safe_span_attributes(attributes or {}),
            record_exception=False,
            set_status_on_exception=False,
        ):
            yield

    def annotate_current(self, attributes: dict[str, str | int | float | bool]) -> None:
        span = trace.get_current_span()
        if span.is_recording():
            for name, value in _safe_span_attributes(attributes).items():
                span.set_attribute(name, value)

    def instrument_fastapi(self, app: FastAPI) -> None:
        if self._provider is None:
            return
        FastAPIInstrumentor.instrument_app(
            app,
            tracer_provider=self._provider,
            excluded_urls="healthz,metrics",
            exclude_spans=["receive", "send"],
        )

    def shutdown(self) -> None:
        if self._provider is not None:
            self._provider.shutdown()


class _ContentSafeSpanExporter(SpanExporter):
    """Strip request-derived data at the final boundary before spans leave the process."""

    def __init__(self, delegate: SpanExporter) -> None:
        self._delegate = delegate

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return self._delegate.export(tuple(_content_safe_export_span(span) for span in spans))

    def shutdown(self) -> None:
        self._delegate.shutdown()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return self._delegate.force_flush(timeout_millis)


async def start_metrics_server(
    telemetry: SafeTelemetry,
    *,
    host: str = "0.0.0.0",
    port: int = 9090,
) -> asyncio.Server:
    """Serve the content-safe Prometheus snapshot on an internal-only socket."""

    if not 0 <= port <= 65535:
        raise ValueError("Metrics port must be between 0 and 65535")

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            response: bytes | None = None
            request_line = await reader.readline()
            if len(request_line) > 8_192:
                response = _metrics_response(431, b"")
            else:
                while True:
                    header = await reader.readline()
                    if header in {b"\r\n", b"\n", b""}:
                        break
                    if len(header) > 8_192:
                        response = _metrics_response(431, b"")
                        break
                if response is None:
                    if request_line == b"GET /metrics HTTP/1.1\r\n":
                        response = _metrics_response(
                            200,
                            telemetry.render_metrics().encode("utf-8"),
                            content_type=b"text/plain; version=0.0.4",
                        )
                    else:
                        response = _metrics_response(404, b"")
            assert response is not None
            writer.write(response)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    return await asyncio.start_server(handle, host=host, port=port)


def _metrics_response(
    status: int,
    body: bytes,
    *,
    content_type: bytes = b"text/plain",
) -> bytes:
    reason = {200: b"OK", 400: b"Bad Request", 404: b"Not Found", 431: b"Too Large"}[status]
    return b"\r\n".join(
        (
            b"HTTP/1.1 " + str(status).encode("ascii") + b" " + reason,
            b"Content-Type: " + content_type,
            b"Content-Length: " + str(len(body)).encode("ascii"),
            b"Connection: close",
            b"",
            body,
        )
    )


def _append_counter(
    lines: list[str],
    name: str,
    help_text: str,
    label: str,
    values: Mapping[str, int | float],
) -> None:
    lines.extend((f"# HELP {name} {help_text}", f"# TYPE {name} counter"))
    lines.extend(f'{name}{{{label}="{key}"}} {value}' for key, value in sorted(values.items()))


def _append_gauge(
    lines: list[str],
    name: str,
    help_text: str,
    label: str,
    values: Mapping[str, int],
) -> None:
    lines.extend((f"# HELP {name} {help_text}", f"# TYPE {name} gauge"))
    lines.extend(f'{name}{{{label}="{key}"}} {value}' for key, value in sorted(values.items()))


def _require_label(value: str, allowed: frozenset[str], label_name: str) -> None:
    if value not in allowed:
        raise ValueError(f"Unknown {label_name}")


def _content_safe_export_span(span: ReadableSpan) -> ReadableSpan:
    service_name = span.resource.attributes.get("service.name")
    if service_name not in _SAFE_SERVICE_NAMES:
        service_name = "memory-service"
    return ReadableSpan(
        name="http.server.request" if span.kind is SpanKind.SERVER else _safe_span_name(span.name),
        context=_content_safe_span_context(span.context),
        parent=_content_safe_span_context(span.parent),
        resource=Resource({"service.name": service_name}),
        attributes=_safe_export_attributes(span.attributes or {}),
        events=(),
        links=(),
        kind=span.kind,
        status=Status(span.status.status_code),
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


def _content_safe_span_context(context: SpanContext | None) -> SpanContext | None:
    if context is None:
        return None
    return SpanContext(
        trace_id=context.trace_id,
        span_id=context.span_id,
        is_remote=context.is_remote,
        trace_flags=context.trace_flags,
        trace_state=TraceState(),
    )


def _safe_export_attributes(
    attributes: Mapping[str, object],
) -> dict[str, str | int | float | bool]:
    scalar_attributes = {
        name: value
        for name, value in attributes.items()
        if isinstance(value, str | int | float | bool)
    }
    safe = _safe_span_attributes(scalar_attributes)

    method = attributes.get("http.request.method", attributes.get("http.method"))
    if isinstance(method, str) and method in _HTTP_METHODS:
        safe["http.request.method"] = method

    status_code = attributes.get("http.response.status_code", attributes.get("http.status_code"))
    if isinstance(status_code, int) and not isinstance(status_code, bool):
        if 100 <= status_code <= 599:
            safe["http.response.status_code"] = status_code
    return safe


def _safe_span_name(name: str) -> str:
    return name if name in _SAFE_SPAN_NAMES else "memory.operation"


def _safe_span_attributes(
    attributes: Mapping[str, str | int | float | bool],
) -> dict[str, str | int | float | bool]:
    safe: dict[str, str | int | float | bool] = {}
    for name, value in attributes.items():
        if name not in _SAFE_SPAN_ATTRIBUTES:
            continue
        if name in {"memory.tenant_ref", "memory.run_ref"}:
            if isinstance(value, str) and _OPAQUE_REF.fullmatch(value):
                safe[name] = value
            continue
        if name == "memory.actor_kind":
            if value in {"user", "agent"}:
                safe[name] = value
            continue
        if name == "memory.agent.outcome":
            if isinstance(value, str) and value in _AGENT_OUTCOMES:
                safe[name] = value
            continue
        if name == "memory.budget.dimension":
            if isinstance(value, str) and value in _BUDGET_DIMENSIONS:
                safe[name] = value
            continue
        if name == "gen_ai.request.model":
            if value == "claude-sonnet-5":
                safe[name] = value
            continue
        if name == "agent.tool.name":
            if isinstance(value, str) and value in _SAFE_TOOL_NAMES:
                safe[name] = value
            continue
    return safe
