"""Metrics (Prometheus) and traces (OTLP) of every turn: what they say, what they never say, and that they never reach the customer's turn."""
import time

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.config import get_settings
from app.graph import build_graph
from app.main import app, sweep_once
from app.schemas import ChatResponse, HandoffInfo, Routing
from app.service import ChatService
from app.telemetry import Metrics, Telemetry, Tracing, outcome_of
from tests.conftest import bearer, make_jwt

SPANS = [
    {"kind": "llm", "node": "router", "name": "m-small", "latency_ms": 400, "input_tokens": 1000, "output_tokens": 50, "cost_usd": 0.0002, "ok": True, "error_type": None,
     "started_at": 1_800_000_000.1},
    {"kind": "tool", "node": "support", "name": "get_terminal_status", "latency_ms": 20, "input_tokens": 0, "output_tokens": 0, "cost_usd": None, "ok": True,
     "error_type": None, "started_at": 1_800_000_000.6},
    {"kind": "llm", "node": "knowledge", "name": "m-big", "latency_ms": 3000, "input_tokens": 4000, "output_tokens": 300, "cost_usd": 0.0011, "ok": False,
     "error_type": "RateLimitError", "started_at": 1_800_000_000.7},
]
USAGE = {"input_tokens": 5000, "output_tokens": 350, "cost_usd": 0.0013, "total_tokens": 5350}


def sample(registry_text: str, name: str, **labels) -> float:
    """The value of one sample of the Prometheus text output."""
    for line in registry_text.splitlines():
        if line.startswith(name + "{") or line.startswith(name + " "):
            if all(f'{k}="{v}"' in line for k, v in labels.items()):
                return float(line.rsplit(" ", 1)[1])
    raise AssertionError(f"{name}{labels} not in the output")


# ---------------- metrics ----------------

def test_a_turn_updates_the_counters_the_histograms_and_the_per_node_series():
    m = Metrics()
    m.observe_turn(4200, False, None, SPANS, USAGE)
    text = m.render().decode()
    assert sample(text, "getnet_turns_total", outcome="ok") == 1
    assert sample(text, "getnet_turn_duration_seconds_count") == 1 and sample(text, "getnet_turn_duration_seconds_sum") == pytest.approx(4.2)
    assert sample(text, "getnet_node_duration_seconds_count", node="router", kind="llm") == 1
    assert sample(text, "getnet_node_duration_seconds_sum", node="knowledge", kind="llm") == pytest.approx(3.0)
    assert sample(text, "getnet_llm_tokens_total", model="m-small", direction="input") == 1000
    assert sample(text, "getnet_llm_tokens_total", model="m-big", direction="output") == 300
    assert sample(text, "getnet_llm_cost_usd_total", model="m-small") == pytest.approx(0.0002)
    assert sample(text, "getnet_turn_cost_usd_sum") == pytest.approx(0.0013)


def test_a_failed_call_counts_as_a_provider_error_and_a_429_also_as_rate_limited():
    m = Metrics()
    m.observe_turn(1000, True, None, SPANS, USAGE)
    text = m.render().decode()
    assert sample(text, "getnet_provider_errors_total", node="knowledge", error_type="RateLimitError") == 1
    assert sample(text, "getnet_rate_limited_total", node="knowledge") == 1
    assert sample(text, "getnet_turns_total", outcome="degraded") == 1


def test_a_handoff_turn_counts_by_reason_and_the_outcome_is_handoff():
    m = Metrics()
    m.observe_turn(900, False, "agent_unresolved", [], None)
    text = m.render().decode()
    assert sample(text, "getnet_handoffs_total", reason="agent_unresolved") == 1 and sample(text, "getnet_turns_total", outcome="handoff") == 1
    assert outcome_of(True, "user_requested") == "handoff" and outcome_of(True, None) == "degraded" and outcome_of(False, None) == "ok"


def test_two_metric_sets_in_one_process_do_not_collide_and_the_queue_gauges_are_set():
    a, b = Metrics(), Metrics()
    a.observe_turn(100, False, None, [], None)
    assert sample(b.render().decode(), "getnet_turn_duration_seconds_count") == 0
    class Hook:
        sent, failed, dropped = 7, 2, 1
    a.set_queues(pending_handoffs=3, pending_approvals=5, webhook=Hook())
    text = a.render().decode()
    assert sample(text, "getnet_handoffs_pending") == 3 and sample(text, "getnet_approvals_pending") == 5
    assert sample(text, "getnet_webhook_events", result="failed") == 2 and sample(text, "getnet_webhook_events", result="dropped") == 1


def test_the_output_never_holds_a_customer_id_or_text_only_bounded_labels():
    m = Metrics()
    m.observe_turn(1000, False, "user_requested", SPANS, USAGE)
    text = m.render().decode()
    for forbidden in ("cliente1988", "user_id", "conversation", "my terminal"):
        assert forbidden not in text
    labels = {part.split("=")[0] for line in text.splitlines() if "{" in line and not line.startswith("#")
              for part in line[line.index("{") + 1:line.index("}")].split(",") if "=" in part}
    assert labels <= {"outcome", "node", "kind", "model", "direction", "error_type", "reason", "result", "le"}


# ---------------- traces ----------------

def exported(spans=SPANS, **kw):
    mem = InMemorySpanExporter()
    Tracing.in_memory(mem).export_turn(trace_id="abc123", started_at=1_800_000_000.0, latency_ms=4200, plan=["knowledge", "support"], degraded=kw.get("degraded", False),
                                       handoff_reason=kw.get("handoff"), spans=spans, usage=USAGE)
    return {s.name: s for s in mem.get_finished_spans()}, mem.get_finished_spans()


def test_one_trace_per_turn_a_root_and_a_child_per_call_with_the_real_position_and_duration():
    by_name, finished = exported()
    root = by_name["chat.turn"]
    kids = [s for s in finished if s.name != "chat.turn"]
    assert len(finished) == 4 and len(kids) == 3 and all(k.parent.span_id == root.context.span_id and k.context.trace_id == root.context.trace_id for k in kids)
    assert (root.end_time - root.start_time) / 1e6 == pytest.approx(4200, abs=1)
    router = by_name["llm router"]
    assert (router.start_time - root.start_time) / 1e6 == pytest.approx(100, abs=1) and (router.end_time - router.start_time) / 1e6 == pytest.approx(400, abs=1)
    assert router.attributes["gen_ai.request.model"] == "m-small" and router.attributes["gen_ai.usage.input_tokens"] == 1000
    assert root.attributes["getnet.plan"] == "knowledge,support" and root.attributes["getnet.trace_id"] == "abc123"


def test_a_failed_call_marks_its_span_as_an_error_with_the_error_type():
    by_name, _ = exported()
    bad = by_name["llm knowledge"]
    assert bad.status.status_code.name == "ERROR" and bad.status.description == "RateLimitError"
    assert by_name["tool support"].status.status_code.name == "UNSET"


def test_a_handoff_or_degraded_turn_is_visible_on_the_root_span():
    by_name, _ = exported(handoff="agent_unresolved")
    assert by_name["chat.turn"].attributes["getnet.handoff_reason"] == "agent_unresolved"
    by_name, _ = exported(degraded=True)
    assert by_name["chat.turn"].status.status_code.name == "ERROR"


def test_the_traces_carry_no_text_and_no_customer_id():
    _, finished = exported()
    dump = " ".join(str(dict(s.attributes)) + s.name + str(s.status.description) for s in finished)
    for forbidden in ("cliente1988", "user_id", "my terminal", "message"):
        assert forbidden not in dump


# ---------------- the facade and the service ----------------

def response(**kw):
    base = dict(response="hi", user_id="cliente1988", conversation_id="c1", agents_used=[], routing=Routing(plan=["support"]), latency_ms=1500, trace_id="t1")
    return ChatResponse(**{**base, **kw})


def test_a_failing_metrics_or_tracing_backend_never_raises_into_the_turn():
    class Boom:
        def observe_turn(self, *a, **k):
            raise RuntimeError("down")

        export_turn = observe_turn

    Telemetry(Boom(), Boom()).observe_turn(response(), SPANS, USAGE, time.time())  # does not raise


def test_the_telemetry_reads_the_handoff_reason_and_skips_the_trace_without_a_start_time():
    m, mem = Metrics(), InMemorySpanExporter()
    t = Telemetry(m, Tracing.in_memory(mem))
    t.observe_turn(response(handoff=HandoffInfo(id="HND-AAAAAA", reason="technical_failure", priority="high")), [], None, None)
    assert sample(m.render().decode(), "getnet_handoffs_total", reason="technical_failure") == 1 and mem.get_finished_spans() == ()


def stub_graph():
    def router(state):
        return {"plan": [], "step": 0, "language": "English", "routing_reasoning": "", "agent_outputs": {}, "final_answer": "", "wants_human": False, "handoff": None}

    def synthesizer(state):
        return {"final_answer": "hello", "messages": [AIMessage(content="hello")]}

    return build_graph(router, lambda s: {}, lambda s: {}, synthesizer, lambda s: {})


def test_every_turn_of_the_service_is_observed_once_and_a_broken_telemetry_does_not_fail_it():
    seen = []

    class Spy:
        def observe_turn(self, resp, spans, usage, started_at):
            seen.append((resp.user_id, started_at))

    svc = ChatService(stub_graph(), telemetry=Spy())
    assert svc.handle("hi", "cliente1988", "c1").response == "hello" and len(seen) == 1 and seen[0][1] is not None

    class Broken:
        def observe_turn(self, *a, **k):
            raise RuntimeError("a bug in the telemetry")

    assert ChatService(stub_graph(), telemetry=Broken()).handle("hi", "cliente1988", "c1").response == "hello"  # the customer still gets the answer


# ---------------- /metrics ----------------

@pytest.fixture
def client(auth_env, monkeypatch):
    monkeypatch.setenv("METRICS_TOKEN", "m" * 32)
    get_settings.cache_clear()
    metrics = Metrics()
    app.state.telemetry = Telemetry(metrics, None)
    app.state.service = ChatService(stub_graph(), telemetry=app.state.telemetry)
    with TestClient(app) as c:
        yield c, metrics
    app.state.service = None
    app.state.telemetry = None


def test_the_endpoint_needs_the_token_and_answers_with_the_prometheus_format(client):
    c, _ = client
    assert c.get("/metrics").status_code == 401 and c.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.get("/metrics", headers={"Authorization": "Basic " + "m" * 32}).status_code == 401
    ok = c.get("/metrics", headers={"Authorization": "Bearer " + "m" * 32})
    assert ok.status_code == 200 and ok.headers["content-type"].startswith("text/plain") and "getnet_turns_total" in ok.text


def test_a_merchant_token_is_not_the_metrics_token(client):
    c, _ = client
    assert c.get("/metrics", headers=bearer(make_jwt("cliente1988"))).status_code == 401


def test_a_real_request_shows_up_in_the_scrape_without_the_customers_id_or_words(client):
    c, _ = client
    c.post("/chat", json={"message": "my terminal is offline since this morning"}, headers=bearer(make_jwt("cliente1988")))
    text = c.get("/metrics", headers={"Authorization": "Bearer " + "m" * 32}).text
    assert sample(text, "getnet_turns_total", outcome="ok") == 1
    assert "cliente1988" not in text and "terminal is offline" not in text and "c-" not in text.split("getnet_turns_total")[1][:60]


def test_without_a_token_configured_the_endpoint_does_not_exist(auth_env, monkeypatch):
    monkeypatch.setenv("METRICS_TOKEN", "")
    get_settings.cache_clear()
    app.state.telemetry = Telemetry(Metrics(), None)
    app.state.service = ChatService(stub_graph())
    try:
        with TestClient(app) as c:
            assert c.get("/metrics").status_code == 404 and c.get("/metrics", headers={"Authorization": "Bearer "}).status_code == 404
    finally:
        app.state.service = None
        app.state.telemetry = None


# ---------------- the sweep feeds the gauges ----------------

def test_the_sweep_sets_the_queue_gauges_and_counts_sla_breaches(tmp_path):
    from app.handoff_store import HandoffStore

    store = HandoffStore(tmp_path / "h.sqlite")
    store.notify({"handoff_id": "HND-AAAAAA", "user_id": "u", "conversation_id": "c", "reason": "agent_unresolved", "priority": "normal"})
    m = Metrics()
    sweep_once(None, store, 0.0, None, Telemetry(m, None))
    assert sample(m.render().decode(), "getnet_handoffs_pending") == 1
    store.close()
