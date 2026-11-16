"""Metrics (Prometheus) and traces (OpenTelemetry / OTLP) of every turn.

Both are built from the spans `TurnCollector` already keeps (node, model, tokens, cost, latency, ok): one source, so the numbers on the dashboard, the
trace in Jaeger and the row in the trace store are the same turn. Neither carries text: no message, no answer, no tool argument. And no customer id
either: a metric label or a span attribute with a user id would be unbounded (cardinality) and personal data in a monitoring system.

    Telemetry(Metrics(), Tracing.from_endpoint("http://jaeger:4317"))     # what the API builds
    telemetry.observe_turn(response, collector.spans, usage, started_at)  # after each turn; it never raises into the turn

`/metrics` (app/main.py) serves `Metrics.render()`, behind a bearer token (`METRICS_TOKEN`); without a token the endpoint does not exist.
"""
from __future__ import annotations

import logging
from typing import Any

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram, generate_latest

log = logging.getLogger("telemetry")

SECONDS = (0.25, 0.5, 1, 2, 3, 5, 8, 12, 20, 30, 60, 120)
COST = (0.0005, 0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1)
CONTENT_TYPE = CONTENT_TYPE_LATEST


def outcome_of(degraded: bool, handoff_reason: str | None) -> str:
    return "handoff" if handoff_reason else "degraded" if degraded else "ok"


class Metrics:
    """Prometheus metrics in their OWN registry (not the global one): two instances in one process (the tests) never collide."""

    def __init__(self, registry: CollectorRegistry | None = None):
        self.registry = registry or CollectorRegistry()
        r = self.registry
        self.turns = Counter("getnet_turns_total", "Customer turns answered", ["outcome"], registry=r)
        self.turn_seconds = Histogram("getnet_turn_duration_seconds", "End-to-end latency of a turn", buckets=SECONDS, registry=r)
        self.turn_cost = Histogram("getnet_turn_cost_usd", "Estimated model cost of a turn", buckets=COST, registry=r)
        self.node_seconds = Histogram("getnet_node_duration_seconds", "Latency of one model or tool call, by graph node", ["node", "kind"],
                                      buckets=SECONDS, registry=r)
        self.tokens = Counter("getnet_llm_tokens_total", "Model tokens", ["model", "direction"], registry=r)
        self.cost = Counter("getnet_llm_cost_usd_total", "Estimated model cost in USD (list prices)", ["model"], registry=r)
        self.provider_errors = Counter("getnet_provider_errors_total", "Failed model or tool calls", ["node", "error_type"], registry=r)
        self.rate_limited = Counter("getnet_rate_limited_total", "Calls that hit a provider rate limit (429)", ["node"], registry=r)
        self.handoffs = Counter("getnet_handoffs_total", "Turns handed to a human", ["reason"], registry=r)
        self.sla_breaches = Counter("getnet_handoff_sla_breaches_total", "Handoffs escalated because nobody claimed them in time", registry=r)
        self.pending_handoffs = Gauge("getnet_handoffs_pending", "Handoffs waiting for an operator", registry=r)
        self.pending_approvals = Gauge("getnet_approvals_pending", "Ticket requests waiting for approval", registry=r)
        self.webhook = Gauge("getnet_webhook_events", "Operator webhook events by result since the process started", ["result"], registry=r)

    def observe_turn(self, latency_ms: float, degraded: bool, handoff_reason: str | None, spans: list[dict], usage: dict | None) -> None:
        self.turns.labels(outcome_of(degraded, handoff_reason)).inc()
        self.turn_seconds.observe(max(0.0, latency_ms) / 1000)
        if handoff_reason:
            self.handoffs.labels(handoff_reason).inc()
        for s in spans:
            node, kind = s.get("node") or "unknown", s.get("kind") or "unknown"
            self.node_seconds.labels(node, kind).observe(max(0, s.get("latency_ms", 0)) / 1000)
            if kind == "llm":
                model = s.get("name") or "unknown"
                self.tokens.labels(model, "input").inc(s.get("input_tokens") or 0)
                self.tokens.labels(model, "output").inc(s.get("output_tokens") or 0)
                if s.get("cost_usd"):
                    self.cost.labels(model).inc(s["cost_usd"])
            if not s.get("ok", True):
                err = s.get("error_type") or "unknown"
                self.provider_errors.labels(node, err).inc()
                if "ratelimit" in err.lower().replace("_", ""):
                    self.rate_limited.labels(node).inc()
        if usage and usage.get("cost_usd") is not None:
            self.turn_cost.observe(usage["cost_usd"])

    def set_queues(self, pending_handoffs: int | None = None, pending_approvals: int | None = None, webhook: Any = None) -> None:
        if pending_handoffs is not None:
            self.pending_handoffs.set(pending_handoffs)
        if pending_approvals is not None:
            self.pending_approvals.set(pending_approvals)
        if webhook is not None:
            for result in ("sent", "failed", "dropped"):
                self.webhook.labels(result).set(getattr(webhook, result, 0))

    def render(self) -> bytes:
        return generate_latest(self.registry)


class Tracing:
    """One trace per turn: a root span `chat.turn` and one child per model or tool call, with the real start and duration."""

    def __init__(self, tracer, provider=None):
        self.tracer, self.provider = tracer, provider

    @classmethod
    def from_endpoint(cls, endpoint: str, service_name: str = "getnet-agents", insecure: bool = True) -> "Tracing":
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=insecure)))
        return cls(provider.get_tracer("getnet.turns"), provider)

    @classmethod
    def in_memory(cls, exporter, service_name: str = "getnet-agents") -> "Tracing":
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor

        provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        return cls(provider.get_tracer("getnet.turns"), provider)

    def export_turn(self, *, trace_id: str, started_at: float, latency_ms: float, plan: list[str], degraded: bool, handoff_reason: str | None,
                    spans: list[dict], usage: dict | None) -> None:
        from opentelemetry import trace
        from opentelemetry.trace import Status, StatusCode

        ns = lambda t: int(t * 1e9)  # noqa: E731
        root = self.tracer.start_span("chat.turn", start_time=ns(started_at), attributes={
            "getnet.trace_id": trace_id, "getnet.plan": ",".join(plan), "getnet.degraded": degraded, "getnet.handoff_reason": handoff_reason or "",
            "getnet.latency_ms": int(latency_ms), "gen_ai.usage.input_tokens": (usage or {}).get("input_tokens", 0),
            "gen_ai.usage.output_tokens": (usage or {}).get("output_tokens", 0), "getnet.cost_usd": (usage or {}).get("cost_usd") or 0.0})
        ctx = trace.set_span_in_context(root)
        for s in spans:
            begin = s.get("started_at") or started_at
            child = self.tracer.start_span(f"{s.get('kind', 'span')} {s.get('node') or s.get('name')}", context=ctx, start_time=ns(begin), attributes={
                "getnet.node": s.get("node") or "unknown", "getnet.kind": s.get("kind") or "unknown", "getnet.name": s.get("name") or "unknown",
                "gen_ai.request.model": s.get("name") if s.get("kind") == "llm" else "", "gen_ai.usage.input_tokens": s.get("input_tokens") or 0,
                "gen_ai.usage.output_tokens": s.get("output_tokens") or 0, "getnet.cost_usd": s.get("cost_usd") or 0.0})
            if not s.get("ok", True):
                child.set_status(Status(StatusCode.ERROR, s.get("error_type") or "error"))
            child.end(end_time=ns(begin + s.get("latency_ms", 0) / 1000))
        if degraded or handoff_reason:
            root.set_status(Status(StatusCode.ERROR if degraded else StatusCode.OK, handoff_reason or "degraded"))
        root.end(end_time=ns(started_at + latency_ms / 1000))

    def shutdown(self) -> None:
        if self.provider is not None:
            self.provider.shutdown()  # flushes what is still queued


class Telemetry:
    """What the service holds: metrics, and traces if an OTLP endpoint is configured. Nothing here may fail a turn."""

    def __init__(self, metrics: Metrics | None = None, tracing: Tracing | None = None):
        self.metrics, self.tracing = metrics, tracing

    def observe_turn(self, resp, spans: list[dict], usage: dict | None, started_at: float | None) -> None:
        handoff = resp.handoff.reason if resp.handoff else None
        try:
            if self.metrics is not None:
                self.metrics.observe_turn(resp.latency_ms, resp.degraded, handoff, spans, usage)
        except Exception:  # noqa: BLE001
            log.warning("metrics failed for trace=%s", resp.trace_id, exc_info=True)
        try:
            if self.tracing is not None and started_at is not None:
                self.tracing.export_turn(trace_id=resp.trace_id, started_at=started_at, latency_ms=resp.latency_ms, plan=resp.routing.plan,
                                         degraded=resp.degraded, handoff_reason=handoff, spans=spans, usage=usage)
        except Exception:  # noqa: BLE001
            log.warning("trace export failed for trace=%s", resp.trace_id, exc_info=True)

    def close(self) -> None:
        if self.tracing is not None:
            try:
                self.tracing.shutdown()
            except Exception:  # noqa: BLE001
                log.warning("tracing shutdown failed", exc_info=True)
