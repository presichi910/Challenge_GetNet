"""H1: a ticket is opened only when a person approves it (LangGraph `interrupt()` + `Command(resume=...)`).

Real SQLite (requests and paused graphs), real LangGraph, real support node with a scripted model: no network, no keys."""
import json
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage

from app.agents.support import make_support_node
from app.approvals import (PENDING_APPROVAL, ApprovalConflict, ApprovalNotFound, ApprovalService, ApprovalStore,
                           build_approval_graph, make_request_approval_node, proposals_in)
from app.graph import build_graph
from app.handoff_store import HandoffStore
from app.main import app
from app.memory import Memory
from app.service import ChatService
from app.tools.customer_tools import make_support_tools
from tests.conftest import bearer, make_jwt, operator_jwt, scripted

A = "cliente1988"
ALL = frozenset({"terminal", "payout", "billing", "account", "other"})
SUMMARY = "Terminal does not connect since this morning, NET_TIMEOUT"
TTL = 3600.0


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


def ticket_call(summary=SUMMARY, category="terminal", call_id="c1"):
    return AIMessage(content="", tool_calls=[{"name": "create_support_ticket", "args": {"category": category, "summary": summary}, "id": call_id}])


class World:
    """The conversation graph (stub router and synthesizer, the REAL support node and request_approval node), the approval graph,
    the stores. `restart()` builds new objects on the same files, like a process that stopped and started again."""

    def __init__(self, tmp_path, repo, llm_messages=(), categories=ALL, language="English"):
        self.tmp, self.repo, self.clock, self.language = tmp_path, repo, Clock(), language
        self.queue = HandoffStore(tmp_path / "handoffs.sqlite", clock=self.clock)
        self.mount(llm_messages, categories)

    def mount(self, llm_messages, categories=ALL):
        self.store = ApprovalStore(self.tmp / "approvals.sqlite", clock=self.clock)
        self.service = ApprovalService(self.store, build_approval_graph(self.store, self.repo, notifier=self.queue))
        self.llm = scripted(*llm_messages)
        language = self.language

        def router(state):
            return {"plan": ["support"], "step": 0, "language": language, "routing_reasoning": "stub", "agent_outputs": {},
                    "final_answer": "", "wants_human": False, "handoff": None, "approvals": []}

        def synthesizer(state):
            text = state["agent_outputs"]["support"]["answer"] or "ok"
            return {"final_answer": text, "messages": [AIMessage(content=text)]}

        graph = build_graph(router, lambda s: {}, make_support_node(llm=self.llm, repo=self.repo, approval_categories=categories),
                            synthesizer, lambda s: {}, request_approval=make_request_approval_node(self.store, TTL))
        self.chat = ChatService(graph, approvals=self.service)

    def restart(self, llm_messages=(), categories=ALL):
        self.store.close()
        self.mount(llm_messages, categories)

    def close(self):
        self.store.close()
        self.queue.close()

    def pending(self):
        return self.store.list("pending")


@pytest.fixture
def world(tmp_path, repo):
    made = []

    def make(*llm_messages, **kw):
        w = World(tmp_path, repo, llm_messages, **kw)
        made.append(w)
        return w

    yield make
    for w in made:
        w.close()


def request_a_ticket(w, summary=SUMMARY, message="my terminal does not connect, please open a ticket"):
    """Turn 1: the model calls create_support_ticket and then answers."""
    return w.chat.handle(message, A, "c1")


def tickets_in_file(tmp_path):
    f = tmp_path / "tickets.jsonl"
    return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines()] if f.exists() else []


# ---------------- the store ----------------

@pytest.fixture
def store(tmp_path):
    s = ApprovalStore(tmp_path / "a.sqlite", clock=Clock())
    yield s
    s.close()


def make_request(store, summary=SUMMARY, category="terminal", conv="c1", user=A):
    return store.create(user, conv, category, summary, "English", TTL)


def test_the_same_issue_while_pending_is_one_request_and_a_new_one_after_it_is_decided(store):
    a, created = make_request(store)
    b, created_again = make_request(store, summary=SUMMARY.upper() + "!")  # same issue, other spelling
    assert created and not created_again and b["id"] == a["id"] and a["id"].startswith("APR-")
    store.decide(a["id"], "ops1", "rejected")
    c, created_third = make_request(store)
    assert created_third and c["id"] != a["id"]


def test_two_decisions_on_one_request_only_one_wins(store):
    a, _ = make_request(store)
    won = store.decide(a["id"], "ops1", "approved", "ok")
    assert won["status"] == "approved" and won["decided_by"] == "ops1"
    with pytest.raises(ApprovalConflict, match="already approved"):
        store.decide(a["id"], "ops2", "rejected")
    with pytest.raises(ApprovalConflict):
        store.decide(a["id"], "ops1", "approved")  # not even the same operator again
    with pytest.raises(ApprovalNotFound):
        store.decide("APR-ZZZZZZ", "ops1", "approved")
    with pytest.raises(ValueError):
        store.decide(a["id"], "ops1", "expired")  # only the clock expires a request


def test_simultaneous_decisions_have_exactly_one_winner(tmp_path):
    store = ApprovalStore(tmp_path / "a.sqlite")
    a, _ = make_request(store)
    results, barrier = [], threading.Barrier(8)

    def go(i):
        barrier.wait()
        try:
            store.decide(a["id"], f"ops{i}", "approved" if i % 2 else "rejected")
            results.append("won")
        except ApprovalConflict:
            results.append("lost")

    threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    store.close()
    assert results.count("won") == 1 and results.count("lost") == 7


def test_after_the_deadline_nobody_can_approve_and_the_clock_expires_it(tmp_path):
    clock = Clock()
    store = ApprovalStore(tmp_path / "a.sqlite", clock=clock)
    a, _ = make_request(store)
    clock.now += TTL - 1
    assert store.expire_due() == []
    clock.now += 2
    with pytest.raises(ApprovalConflict, match="expired"):  # past the deadline, not swept yet: still refused
        store.decide(a["id"], "ops1", "approved")
    expired = store.expire_due()
    assert [e["id"] for e in expired] == [a["id"]] and expired[0]["status"] == "expired" and expired[0]["decided_by"] == "system"
    assert store.expire_due() == []  # once
    store.close()


def test_what_the_customer_has_not_been_told_is_taken_once_and_can_be_given_back(store):
    a, _ = make_request(store)
    store.decide(a["id"], "ops1", "approved")
    assert store.pop_undelivered(A, "c1") == []  # decided, but the graph has not recorded the outcome yet
    store.mark_resumed(a["id"], "TKT-AAAAAA", "Your ticket is open")
    got = store.pop_undelivered(A, "c1")
    assert [g["id"] for g in got] == [a["id"]] and got[0]["outcome"] == "Your ticket is open"
    assert store.pop_undelivered(A, "c1") == [] and store.pop_undelivered("cliente2024", "c1") == []
    store.restore_undelivered([a["id"]])
    assert len(store.pop_undelivered(A, "c1")) == 1


def test_deleting_a_conversation_erases_its_requests_and_retention_purges_old_decided_ones(tmp_path):
    clock = Clock()
    store = ApprovalStore(tmp_path / "a.sqlite", clock=clock)
    a, _ = make_request(store, conv="c1")
    b, _ = make_request(store, summary="Payout is missing for last Friday", category="payout", conv="c2")
    assert store.delete_conversation(A, "c1") == [a["id"]] and store.get(a["id"]) is None and store.get(b["id"])
    store.decide(b["id"], "ops1", "rejected")
    store.mark_resumed(b["id"], None, "no")
    store.pop_undelivered(A, "c2")
    assert store.purge_expired(30) == 0
    clock.now += 31 * 86400
    assert store.purge_expired(30) == 1 and store.get(b["id"]) is None
    store.close()


# ---------------- the tool only proposes ----------------

def tool_of(repo, categories):
    return {t.name: t for t in make_support_tools(A, repo, categories)}["create_support_ticket"]


def test_a_category_that_needs_approval_is_proposed_and_nothing_is_written(repo, tmp_path):
    out = json.loads(tool_of(repo, ALL).invoke({"category": "terminal", "summary": SUMMARY}))
    assert out["status"] == PENDING_APPROVAL and out["category"] == "terminal" and "ticket_id" not in out
    assert "NOT open yet" in out["message"] and "do not invent a number" in out["message"]
    assert tickets_in_file(tmp_path) == []


def test_only_the_configured_categories_wait_and_without_any_the_ticket_opens_at_once(repo, tmp_path):
    only_billing = frozenset({"billing"})
    assert json.loads(tool_of(repo, only_billing).invoke({"category": "billing", "summary": SUMMARY}))["status"] == PENDING_APPROVAL
    opened = json.loads(tool_of(repo, only_billing).invoke({"category": "terminal", "summary": SUMMARY}))
    assert opened["ticket_id"].startswith("TKT-") and len(tickets_in_file(tmp_path)) == 1
    other = json.loads(tool_of(repo, frozenset()).invoke({"category": "payout", "summary": "Payout is late, missing since Monday"}))
    assert other["ticket_id"].startswith("TKT-")


def test_an_issue_that_already_has_an_open_ticket_needs_no_approval(repo, tmp_path):
    first = json.loads(tool_of(repo, frozenset()).invoke({"category": "terminal", "summary": SUMMARY}))
    again = json.loads(tool_of(repo, ALL).invoke({"category": "terminal", "summary": SUMMARY.lower()}))
    assert again["ticket_id"] == first["ticket_id"] and again["already_existed"] is True and again["status"] == "open"
    assert len(tickets_in_file(tmp_path)) == 1


def test_an_unknown_customer_is_not_found_not_pending(repo):
    tool = {t.name: t for t in make_support_tools("ghost", repo, ALL)}["create_support_ticket"]
    assert json.loads(tool.invoke({"category": "other", "summary": SUMMARY}))["error"] == "customer_not_found"


def test_only_what_the_tool_reports_as_pending_counts_as_a_proposal():
    pending = f"[create_support_ticket] {json.dumps({'status': PENDING_APPROVAL, 'category': 'terminal', 'summary': SUMMARY})}"
    opened = '[create_support_ticket] {"ticket_id": "TKT-AAAAAA", "category": "terminal", "summary": "x"}'
    state = {"agent_outputs": {"support": {"contexts": [pending, pending, opened, "[create_support_ticket] not json", "[get_terminal_status] {}"]}}}
    assert [p["category"] for p in proposals_in(state)] == ["terminal"]  # twice the same request is one
    assert proposals_in({}) == [] and proposals_in({"agent_outputs": {"knowledge": {"contexts": [pending]}}}) == []


# ---------------- a turn that asks for a ticket ----------------

def test_the_turn_answers_at_once_with_the_request_pending_and_opens_no_ticket(world, tmp_path):
    w = world(ticket_call(), AIMessage(content="I've asked for your ticket."))
    resp = request_a_ticket(w)
    assert len(resp.approvals) == 1 and resp.approvals[0].status == "pending" and resp.approvals[0].category == "terminal"
    ref = resp.approvals[0].id
    assert "I've asked for your ticket." in resp.response and f"reference {ref}" in resp.response and "not open yet" in resp.response
    assert tickets_in_file(tmp_path) == [] and len(w.pending()) == 1
    snap = w.service.graph.get_state(w.service._config(ref))
    assert snap.next == ("approve",)  # the approval graph is paused at interrupt()


def test_the_notice_follows_the_customers_language(world):
    w = world(ticket_call(), AIMessage(content="Pedi a aprovacao."), language="Portuguese")
    assert "aprovação da nossa equipe" in request_a_ticket(w).response
    w2 = world(ticket_call(), AIMessage(content="Listo."), language="Spanish")
    assert "para su aprobación" in w2.chat.handle("abre un ticket", "cliente2024", "c9").response


def test_the_paused_graph_holds_the_id_only_never_what_the_customer_wrote(world):
    w = world(ticket_call(), AIMessage(content="done"))
    ref = request_a_ticket(w).approvals[0].id
    conn = sqlite3.connect(str(w.tmp / "approvals.sqlite"))
    blobs = b"".join(bytes(c) for tbl in ("checkpoints", "writes") for row in conn.execute(f"SELECT * FROM {tbl}") for c in row
                     if isinstance(c, (bytes, bytearray)))
    conn.close()
    assert ref.encode() in blobs and b"NET_TIMEOUT" not in blobs and b"does not connect" not in blobs


def test_asking_twice_in_a_row_does_not_stack_requests(world):
    w = world(ticket_call(), AIMessage(content="first"), ticket_call(call_id="c2"), AIMessage(content="second"))
    a = request_a_ticket(w)
    b = w.chat.handle("did you open it? please do", A, "c1")
    assert len(w.pending()) == 1 and a.approvals[0].id == b.approvals[0].id


def test_a_turn_without_a_ticket_request_has_no_approvals_and_a_stale_one_is_not_repeated(world):
    w = world(ticket_call(), AIMessage(content="asked"), AIMessage(content="You're welcome."))
    assert len(request_a_ticket(w).approvals) == 1
    later = w.chat.handle("thanks", A, "c1")
    assert later.approvals == [] and "reference APR" not in later.response  # the router resets it every turn


# ---------------- the operator decides: approve, reject, expire ----------------

def decide(w, approval_id, decision, operator="ops1", reason="checked"):
    return w.service.decide(approval_id, operator, decision, reason)


def test_approve_opens_the_ticket_once_and_the_customer_is_told_on_the_next_turn(world, tmp_path):
    w = world(ticket_call(), AIMessage(content="asked"), AIMessage(content="Anything else?"), AIMessage(content="Glad to help."))
    ref = request_a_ticket(w).approvals[0].id
    row = decide(w, ref, "approved")
    assert row["status"] == "approved" and row["ticket_id"].startswith("TKT-") and row["resumed_at"]
    assert [t["ticket_id"] for t in tickets_in_file(tmp_path)] == [row["ticket_id"]] and tickets_in_file(tmp_path)[0]["user_id"] == A
    with pytest.raises(ApprovalConflict):  # a second approval (a double click, a replay) cannot open a second ticket
        decide(w, ref, "approved")
    assert len(tickets_in_file(tmp_path)) == 1

    nxt = w.chat.handle("any news?", A, "c1")
    assert [u.status for u in nxt.approval_updates] == ["approved"] and nxt.approval_updates[0].ticket_id == row["ticket_id"]
    assert row["ticket_id"] in nxt.approval_updates[0].message
    seen = [m.content for m in w.chat.graph.get_state({"configurable": {"thread_id": f"{A}:c1"}}).values["messages"]]
    assert any(row["ticket_id"] in str(c) for c in seen)  # the agents read the outcome as part of the conversation
    assert w.chat.handle("thanks", A, "c1").approval_updates == []  # told once


def test_reject_opens_nothing_and_the_reason_stays_with_the_operators(world, tmp_path):
    w = world(ticket_call(), AIMessage(content="asked"), AIMessage(content="ok"))
    ref = request_a_ticket(w).approvals[0].id
    row = decide(w, ref, "rejected", reason="duplicate of case 4411, internal note")
    assert row["status"] == "rejected" and row["ticket_id"] is None and tickets_in_file(tmp_path) == []
    update = w.chat.handle("any news?", A, "c1").approval_updates[0]
    assert update.status == "rejected" and update.ticket_id is None and "did not approve" in update.message
    assert "4411" not in update.message and "internal note" not in update.message


def test_an_unanswered_request_expires_opens_no_ticket_and_goes_to_the_human_queue(world, tmp_path):
    w = world(ticket_call(), AIMessage(content="asked"), AIMessage(content="ok"))
    ref = request_a_ticket(w).approvals[0].id
    w.clock.now += TTL + 5
    w.service.sweep()
    row = w.store.get(ref)
    assert row["status"] == "expired" and row["decided_by"] == "system" and row["ticket_id"] is None and row["resumed_at"]
    assert tickets_in_file(tmp_path) == []  # silence is never an approval
    queued = w.queue.list_queue("pending")
    assert len(queued) == 1 and queued[0]["reason"] == "approval_expired" and queued[0]["priority"] == "high" and queued[0]["merchant_id"] == A
    w.service.sweep()
    w.service.sweep()
    assert len(w.queue.list_queue("pending")) == 1  # once, however often the clock loop runs
    update = w.chat.handle("any news?", A, "c1").approval_updates[0]
    assert update.status == "expired" and "human agent" in update.message and queued[0]["id"] in update.message
    with pytest.raises(ApprovalConflict):
        decide(w, ref, "approved")  # too late
    assert tickets_in_file(tmp_path) == []


def test_the_customer_can_keep_writing_while_the_request_waits_and_it_can_still_be_decided(world, tmp_path):
    """The reason the interrupt lives in a graph of its own: a new turn in the conversation must not orphan it."""
    w = world(ticket_call(), AIMessage(content="asked"), AIMessage(content="Sure, what else?"), AIMessage(content="Your payout is fine."))
    ref = request_a_ticket(w).approvals[0].id
    w.chat.handle("by the way, what about my payout?", A, "c1")
    w.chat.handle("and the fees?", A, "c1")
    assert w.service.graph.get_state(w.service._config(ref)).next == ("approve",)
    row = decide(w, ref, "approved")
    assert row["ticket_id"].startswith("TKT-") and len(tickets_in_file(tmp_path)) == 1


def test_a_request_survives_a_restart_of_the_process(world, tmp_path):
    w = world(ticket_call(), AIMessage(content="asked"))
    ref = request_a_ticket(w).approvals[0].id
    w.restart()  # new store, new graph objects, same files
    assert w.service.graph.get_state(w.service._config(ref)).next == ("approve",)
    assert decide(w, ref, "approved")["ticket_id"].startswith("TKT-") and len(tickets_in_file(tmp_path)) == 1


def test_a_crash_between_the_decision_and_the_resume_is_finished_by_reconcile(world, tmp_path):
    w = world(ticket_call(), AIMessage(content="asked"))
    ref = request_a_ticket(w).approvals[0].id
    w.store.decide(ref, "ops1", "approved", "")  # recorded, and the process died before the graph was continued
    assert tickets_in_file(tmp_path) == [] and w.store.get(ref)["resumed_at"] is None
    w.restart()
    w.service.reconcile()
    row = w.store.get(ref)
    assert row["resumed_at"] and row["ticket_id"].startswith("TKT-") and len(tickets_in_file(tmp_path)) == 1


def test_a_crash_after_the_graph_ran_does_not_run_it_again(world, tmp_path, monkeypatch):
    w = world(ticket_call(), AIMessage(content="asked"))
    ref = request_a_ticket(w).approvals[0].id
    calls = []
    real = w.repo.create_ticket
    monkeypatch.setattr(w.repo, "create_ticket", lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    real_mark = w.store.mark_resumed
    monkeypatch.setattr(w.store, "mark_resumed", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("died before the bookkeeping")))
    decide(w, ref, "approved")  # the decision stands; the failure is logged, not raised
    assert w.store.get(ref)["resumed_at"] is None and len(tickets_in_file(tmp_path)) == 1
    monkeypatch.setattr(w.store, "mark_resumed", real_mark)
    w.service.reconcile()
    assert w.store.get(ref)["resumed_at"] and len(calls) == 1 and len(tickets_in_file(tmp_path)) == 1  # the ticket was not opened again


def test_approving_what_was_opened_in_the_meantime_does_not_open_a_second_ticket(world, tmp_path):
    w = world(ticket_call(), AIMessage(content="asked"))
    ref = request_a_ticket(w).approvals[0].id
    existing = w.repo.create_ticket(A, "terminal", SUMMARY)  # somebody opened the same issue by another route
    row = decide(w, ref, "approved")
    assert row["ticket_id"] == existing["ticket_id"] and len(tickets_in_file(tmp_path)) == 1
    assert "no second one" in w.store.get(ref)["outcome"]


def test_deleting_the_conversation_cancels_what_it_was_waiting_for(world, tmp_path):
    w = world(ticket_call(), AIMessage(content="asked"))
    ref = request_a_ticket(w).approvals[0].id
    assert w.chat.delete_conversation(A, "c1") is True
    assert w.store.get(ref) is None and w.service.graph.get_state(w.service._config(ref)).next == ()
    with pytest.raises(ApprovalNotFound):
        decide(w, ref, "approved")
    assert tickets_in_file(tmp_path) == []


def test_without_the_approval_feature_the_ticket_opens_at_once(world, tmp_path):
    w = world(ticket_call(), AIMessage(content="Your ticket is open."), categories=frozenset())
    resp = request_a_ticket(w)
    assert resp.approvals == [] and len(tickets_in_file(tmp_path)) == 1 and w.pending() == []


# ---------------- the API ----------------

@pytest.fixture
def api(auth_env, world, tmp_path):
    w = world(ticket_call(), AIMessage(content="asked"), AIMessage(content="ok"), AIMessage(content="ok"))
    mem = Memory(tmp_path / "checkpoints.sqlite", ttl_days=30, max_threads_per_user=5)
    w.chat.memory = mem
    app.state.service = w.chat
    with TestClient(app) as c:
        yield c, w
    app.state.service = None
    mem.close()


def m(user=A):
    return bearer(make_jwt(user))


def op(name="ops1"):
    return bearer(operator_jwt(name))


def test_the_whole_loop_through_the_api(api, tmp_path):
    client, w = api
    r = client.post("/chat", json={"message": "open a ticket please", "conversation_id": "c1"}, headers=m())
    assert r.status_code == 200
    body = r.json()
    ref = body["approvals"][0]["id"]
    assert body["approvals"][0]["status"] == "pending" and ref in body["response"]

    listed = client.get("/operator/approvals?status=pending", headers=op()).json()
    assert listed["count"] == 1 and listed["approvals"][0]["id"] == ref
    assert listed["approvals"][0]["merchant_id"] == A and listed["approvals"][0]["summary"] == SUMMARY

    done = client.post(f"/operator/approvals/{ref}/approve", headers=op(), json={"reason": "verified the terminal logs"})
    assert done.status_code == 200 and done.json()["status"] == "approved" and done.json()["decided_by"] == "ops1"
    ticket = done.json()["ticket_id"]
    assert ticket.startswith("TKT-") and len(tickets_in_file(tmp_path)) == 1

    assert client.post(f"/operator/approvals/{ref}/approve", headers=op("ops2")).status_code == 409  # already decided
    assert client.post(f"/operator/approvals/{ref}/reject", headers=op("ops2")).status_code == 409

    nxt = client.post("/chat", json={"message": "any news?", "conversation_id": "c1"}, headers=m()).json()
    assert nxt["approval_updates"][0]["ticket_id"] == ticket and nxt["approval_updates"][0]["status"] == "approved"
    assert client.get("/operator/approvals?status=approved", headers=op()).json()["count"] == 1


def test_reject_through_the_api_and_unknown_or_malformed_ids(api, tmp_path):
    client, w = api
    ref = client.post("/chat", json={"message": "open a ticket", "conversation_id": "c1"}, headers=m()).json()["approvals"][0]["id"]
    r = client.post(f"/operator/approvals/{ref}/reject", headers=op(), json={"reason": "not a terminal problem"})
    assert r.status_code == 200 and r.json()["status"] == "rejected" and r.json()["ticket_id"] is None
    assert tickets_in_file(tmp_path) == []
    assert client.post("/operator/approvals/APR-ZZZZZZ/approve", headers=op()).status_code == 404
    assert client.post("/operator/approvals/not-an-id/approve", headers=op()).status_code == 404
    assert client.post(f"/operator/approvals/{ref}/approve", headers=op(), json={"reason": "x" * 501}).status_code == 422


def test_the_list_expires_what_ran_out_of_time(api, tmp_path):
    client, w = api
    ref = client.post("/chat", json={"message": "open a ticket", "conversation_id": "c1"}, headers=m()).json()["approvals"][0]["id"]
    w.clock.now += TTL + 1
    rows = client.get("/operator/approvals", headers=op()).json()["approvals"]
    assert rows[0]["id"] == ref and rows[0]["status"] == "expired" and rows[0]["decided_by"] == "system"
    assert client.post(f"/operator/approvals/{ref}/approve", headers=op()).status_code == 409
    assert tickets_in_file(tmp_path) == []


@pytest.mark.parametrize("method, path", [("GET", "/operator/approvals"), ("POST", "/operator/approvals/APR-AAAAAA/approve"),
                                          ("POST", "/operator/approvals/APR-AAAAAA/reject")])
def test_a_merchant_cannot_use_the_approval_endpoints_and_anonymous_is_401(api, method, path):
    client, _ = api
    assert client.request(method, path, headers=m()).status_code == 403
    assert client.request(method, path).status_code == 401


def test_without_the_approval_service_the_endpoints_say_so(auth_env, tmp_path, repo):
    app.state.service = ChatService(build_graph(lambda s: {}, lambda s: {}, lambda s: {}, lambda s: {}, lambda s: {}))
    try:
        with TestClient(app) as c:
            assert c.get("/operator/approvals", headers=op()).status_code == 503
    finally:
        app.state.service = None


def test_a_customer_cannot_see_or_decide_another_customers_request_through_chat(api):
    client, w = api
    client.post("/chat", json={"message": "open a ticket", "conversation_id": "c1"}, headers=m(A))
    other = client.post("/chat", json={"message": "hello", "conversation_id": "c1"}, headers=m("cliente2024")).json()
    assert other["approvals"] == [] and other["approval_updates"] == []


# ---------------- configuration and wiring ----------------

def test_by_default_every_ticket_category_needs_approval_and_the_setting_can_narrow_or_turn_it_off():
    from app.config import Settings

    assert Settings(_env_file=None).approval_categories == ALL
    assert Settings(_env_file=None, approval_required_categories=" Billing , payout,").approval_categories == {"billing", "payout"}
    assert Settings(_env_file=None, approval_required_categories="").approval_categories == frozenset()
    assert Settings(_env_file=None).approval_ttl_minutes == 60.0


def test_the_default_graph_has_the_approval_step_only_when_it_is_given_a_store(tmp_path):
    from app.graph import build_default_graph

    store = ApprovalStore(tmp_path / "a.sqlite")
    try:
        assert "request_approval" in build_default_graph(approvals=store).get_graph().nodes
        assert "request_approval" not in build_default_graph().get_graph().nodes  # the eval: tickets open at once, as before
    finally:
        store.close()
