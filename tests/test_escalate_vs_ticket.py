"""What happens when ONE turn both proposes a ticket (it waits for approval) and escalates to a human.

These tests PIN DOWN THE CURRENT behavior so a decision about precedence is made knowing it, and so changing it is a visible,
deliberate edit. They do not choose a policy. The options (see PLAN_95.md section 23) and what each would change:

  P1 escalation wins   the case goes to the human queue, the ticket proposal is dropped (today's behavior when EVERY agent escalates)
  P2 ticket wins       the escalation is ignored; the request waits for approval; no handoff (today's behavior when ANOTHER agent answered)
  P3 both              handoff AND an approval request for the proposal
  P4 neither silently  the agents' tools are not allowed to do both in one turn (the second call gets an error result)
"""
import json

import pytest
from langchain_core.messages import AIMessage

from app.agents.support import make_support_node
from app.approvals import make_request_approval_node, proposals_in
from app.graph import build_graph
from app.handoff import make_handoff_node
from app.service import ChatService
from tests.test_approvals import A, ALL, SUMMARY, TTL, Clock, ApprovalStore, tickets_in_file
from tests.conftest import scripted

BOTH = AIMessage(content="", tool_calls=[
    {"name": "create_support_ticket", "args": {"category": "terminal", "summary": SUMMARY}, "id": "c1"},
    {"name": "escalate_to_human", "args": {"reason": "needs a person"}, "id": "c2"}])


class Built:
    def __init__(self, tmp_path, repo, support_reply, other_agent_answers):
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

        graph = build_graph(router, knowledge, make_support_node(llm=scripted(BOTH, AIMessage(content=support_reply)), repo=repo,
                                                                approval_categories=ALL),
                            synthesizer, make_handoff_node(repo=repo, notifier=Notifier()),
                            request_approval=make_request_approval_node(self.store, TTL))
        self.chat = ChatService(graph)

    def close(self):
        self.store.close()


@pytest.fixture
def built(tmp_path, repo):
    made = []

    def make(**kw):
        b = Built(tmp_path, repo, "I asked for a ticket and passed this on.", **kw)
        made.append(b)
        return b

    yield make
    for b in made:
        b.close()


def test_the_support_agent_can_make_both_calls_in_one_turn_and_both_are_recorded(built):
    b = built(other_agent_answers=False)
    state, _ = b.chat.run_raw("open a ticket and get me a person", A, thread_id="t1", conversation_id="c1")
    called = [tc["name"] for tc in state["agent_outputs"]["support"]["tool_calls"]]
    assert called == ["create_support_ticket", "escalate_to_human"]
    assert state["agent_outputs"]["support"]["unresolved"] is True and len(proposals_in(state)) == 1  # the proposal is in the evidence


def test_today_when_every_agent_escalates_the_human_wins_and_the_ticket_proposal_is_dropped(built, tmp_path):
    """P1 is what happens now in that case: the handoff ends the turn, `request_approval` never runs."""
    b = built(other_agent_answers=False)
    state, _ = b.chat.run_raw("open a ticket and get me a person", A, thread_id="t1", conversation_id="c1")
    assert state["handoff"]["reason"] == "agent_unresolved"
    assert state.get("approvals") == [] and b.store.list() == []  # nobody is asked to approve the ticket the agent proposed
    assert [h["reason"] for h in b.handoffs] == ["agent_unresolved"]
    assert "passed your case to a human agent" in state["final_answer"] and "approval" not in state["final_answer"]
    # the handoff opens ITS OWN ticket directly (a system action, not the agent's): approval does not apply to it
    assert [t["category"] for t in tickets_in_file(tmp_path)] == ["human_handoff"]


def test_today_when_another_agent_answers_the_ticket_wins_and_the_escalation_is_ignored(built, tmp_path):
    """P2 is what happens now in that case: not every agent escalated, so there is no handoff; the request waits for approval."""
    b = built(other_agent_answers=True)
    state, _ = b.chat.run_raw("open a ticket and get me a person", A, thread_id="t1", conversation_id="c1")
    assert state.get("handoff") is None and [a["category"] for a in state["approvals"]] == ["terminal"]
    assert len(b.store.list("pending")) == 1 and b.handoffs == [] and tickets_in_file(tmp_path) == []
    assert "reference APR-" in state["final_answer"]


def test_the_two_outcomes_depend_only_on_whether_another_agent_answered(built):
    """The same tool calls, two different results: the precedence is an accident of the plan, which is why it is a decision."""
    only = built(other_agent_answers=False).chat.run_raw("x", A, thread_id="t1", conversation_id="c1")[0]
    mixed = built(other_agent_answers=True).chat.run_raw("x", A, thread_id="t2", conversation_id="c1")[0]
    assert bool(only.get("handoff")) != bool(mixed.get("handoff"))
    assert bool(only.get("approvals")) != bool(mixed.get("approvals"))
