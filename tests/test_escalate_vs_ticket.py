"""Precedence P1 (decided 2026-11-15): when ONE turn both proposes a ticket (it would wait for approval) and escalates to a human, the
ESCALATION ALWAYS WINS. The proposal is dropped (nobody is asked to approve it), the case goes to the human queue, and the handoff
opens its own ticket (`human_handoff`, a system action). Before, it depended on the plan: only when every agent escalated.

The rule is narrow on purpose: it applies only to a turn that PROPOSED a ticket. An escalation by one agent while another answered, with no
ticket proposed, still does not hand off (the customer got something useful), as it never did.
"""
import pytest
from langchain_core.messages import AIMessage

from app.agents.support import make_support_node
from app.approvals import make_request_approval_node, proposals_in
from app.graph import build_graph
from app.handoff import handoff_reason, make_handoff_node
from app.service import ChatService
from tests.conftest import scripted
from tests.test_approvals import A, ALL, SUMMARY, TTL, ApprovalStore, Clock, tickets_in_file

TICKET = {"name": "create_support_ticket", "args": {"category": "terminal", "summary": SUMMARY}, "id": "c1"}
ESCALATE = {"name": "escalate_to_human", "args": {"reason": "needs a person"}, "id": "c2"}


def calls(*tool_calls):
    return AIMessage(content="", tool_calls=list(tool_calls))


class Built:
    def __init__(self, tmp_path, repo, first_call, other_agent_answers):
        self.store = ApprovalStore(tmp_path / "approvals.sqlite", clock=Clock())
        self.handoffs = []
        plan = ["support", "knowledge"] if other_agent_answers else ["support"]

        def router(state):
            return {"plan": plan, "step": 0, "language": "English", "routing_reasoning": "stub", "agent_outputs": {},
                    "final_answer": "", "wants_human": False, "handoff": None, "approvals": []}

        def knowledge(state):  # another agent that answers normally (not an escalation)
            outs = state.get("agent_outputs", {})
            return {"agent_outputs": {**outs, "knowledge": {"agent": "knowledge", "answer": "Here is the official guidance.", "sources": [],
                                                              "tool_calls": [], "contexts": [], "error": None, "unresolved": False}},
                    "step": state.get("step", 0) + 1}

        def synthesizer(state):
            text = " ".join(o["answer"] for o in state["agent_outputs"].values() if o.get("answer"))
            return {"final_answer": text, "messages": [AIMessage(content=text)]}

        class Notifier:
            def notify(_, record):
                self.handoffs.append(record)

        llm = scripted(first_call, AIMessage(content="I asked for a ticket and passed this on."))
        graph = build_graph(router, knowledge, make_support_node(llm=llm, repo=repo, approval_categories=ALL), synthesizer,
                            make_handoff_node(repo=repo, notifier=Notifier()),
                            request_approval=make_request_approval_node(self.store, TTL))
        self.chat = ChatService(graph)

    def run(self, thread="t1"):
        return self.chat.run_raw("open a ticket and get me a person", A, thread_id=thread, conversation_id="c1")[0]

    def close(self):
        self.store.close()


@pytest.fixture
def built(tmp_path, repo):
    made = []

    def make(first_call, other_agent_answers):
        b = Built(tmp_path, repo, first_call, other_agent_answers)
        made.append(b)
        return b

    yield make
    for b in made:
        b.close()


@pytest.mark.parametrize("other_agent_answers", [False, True], ids=["only support", "another agent also answered"])
def test_the_escalation_wins_whether_or_not_another_agent_answered(built, tmp_path, other_agent_answers):
    b = built(calls(TICKET, ESCALATE), other_agent_answers)
    state = b.run()
    assert [tc["name"] for tc in state["agent_outputs"]["support"]["tool_calls"]] == ["create_support_ticket", "escalate_to_human"]
    assert len(proposals_in(state)) == 1  # the proposal WAS made...
    assert state["handoff"]["reason"] == "agent_unresolved" and [h["reason"] for h in b.handoffs] == ["agent_unresolved"]
    assert state.get("approvals") == [] and b.store.list() == []  # ...and nobody is asked to approve it
    assert "passed your case to a human agent" in state["final_answer"] and "approval" not in state["final_answer"]
    assert [t["category"] for t in tickets_in_file(tmp_path)] == ["human_handoff"]  # the handoff opens ITS OWN ticket, once


def test_the_order_of_the_two_calls_does_not_matter(built, tmp_path):
    b = built(calls(ESCALATE, TICKET), other_agent_answers=True)
    state = b.run()
    assert state["handoff"]["reason"] == "agent_unresolved" and b.store.list() == []
    assert [t["category"] for t in tickets_in_file(tmp_path)] == ["human_handoff"]


def test_a_ticket_proposal_without_an_escalation_still_waits_for_approval(built, tmp_path):
    b = built(calls(TICKET), other_agent_answers=True)
    state = b.run()
    assert state.get("handoff") is None and b.handoffs == [] and len(b.store.list("pending")) == 1 and tickets_in_file(tmp_path) == []
    assert "reference APR-" in state["final_answer"]


def test_an_escalation_by_one_agent_without_a_ticket_proposal_still_does_not_hand_off_when_another_answered(built):
    """Unchanged: the precedence rule is about the ticket proposal only."""
    b = built(calls(ESCALATE), other_agent_answers=True)
    state = b.run()
    assert state.get("handoff") is None and b.handoffs == [] and b.store.list() == []


def test_an_escalation_alone_by_the_only_agent_hands_off_as_always(built):
    b = built(calls(ESCALATE), other_agent_answers=False)
    assert b.run()["handoff"]["reason"] == "agent_unresolved"


def pending(category="terminal"):
    import json

    return "[create_support_ticket] " + json.dumps({"status": "pending_approval", "category": category, "summary": SUMMARY})


def agent(unresolved, contexts=()):
    return {"answer": "x", "unresolved": unresolved, "contexts": list(contexts)}


def test_handoff_reason_unit_cases():
    some = {"plan": ["support", "knowledge"], "agent_outputs": {"support": agent(True, [pending()]), "knowledge": agent(False)}}
    assert handoff_reason(some) == "agent_unresolved"  # P1
    no_proposal = {"plan": ["support", "knowledge"], "agent_outputs": {"support": agent(True), "knowledge": agent(False)}}
    assert handoff_reason(no_proposal) is None
    no_escalation = {"plan": ["support", "knowledge"], "agent_outputs": {"support": agent(False, [pending()]), "knowledge": agent(False)}}
    assert handoff_reason(no_escalation) is None
    opened = '[create_support_ticket] {"ticket_id": "TKT-AAAAAA", "category": "terminal", "summary": "x"}'  # a ticket that was really opened
    assert handoff_reason({"plan": ["support", "knowledge"], "agent_outputs": {"support": agent(True, [opened]), "knowledge": agent(False)}}) is None
