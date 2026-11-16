"""The LangGraph graph.

    router -> agents in the plan, one by one -> synthesizer
    router (user wants a human) or agents (nobody resolved it) -> handoff

`next_node` picks where to go next: the next agent in the plan, then handoff or synthesizer.
Each agent saves its result in `agent_outputs`, so later agents can read earlier ones.
"""
from __future__ import annotations

from typing import Callable, Hashable

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from app.handoff import handoff_reason
from app.state import GraphState

AGENT_NODES = ("knowledge", "support")


def next_node(state: GraphState) -> str:
    if state.get("wants_human"):
        return "handoff"  # user asked for a human, skip the agents
    plan, step = state.get("plan", []), state.get("step", 0)
    if step < len(plan):
        return plan[step]
    return "handoff" if handoff_reason(state) else "synthesizer"


def build_graph(router: Callable, knowledge: Callable, support: Callable, synthesizer: Callable,
                handoff: Callable, checkpointer=None, summarizer: Callable | None = None,
                verify: Callable | None = None, observe: Callable | None = None,
                request_approval: Callable | None = None):
    """Nodes are passed in so tests can use stubs. With a `summarizer`, it runs before the router
    every turn (it only acts above the message threshold). Routing itself is unchanged."""
    g = StateGraph(GraphState)
    g.add_node("router", router)
    g.add_node("knowledge", knowledge)
    g.add_node("support", support)
    g.add_node("synthesizer", synthesizer)
    g.add_node("handoff", handoff)

    if summarizer is not None:
        g.add_node("summarize", summarizer)
        g.add_edge(START, "summarize")
        g.add_edge("summarize", "router")
    else:
        g.add_edge(START, "router")
    targets: dict[Hashable, str] = {**{n: n for n in AGENT_NODES}, "synthesizer": "synthesizer", "handoff": "handoff"}
    for src in ("router", *AGENT_NODES):
        g.add_conditional_edges(src, next_node, targets)
    if verify is not None:  # synthesizer -> [observe ->] verify -> END; without it the graph is exactly as before
        g.add_node("verify", verify)
        if observe is not None:  # the observe step may send the case to a human instead of the (unbacked) answer
            g.add_node("observe", observe)
            g.add_edge("synthesizer", "observe")
            g.add_conditional_edges("observe", lambda state: "handoff" if (state.get("observe") or {}).get("escalate") else "verify",
                                    {"handoff": "handoff", "verify": "verify"})
        else:
            g.add_edge("synthesizer", "verify")
    last = "verify" if verify is not None else "synthesizer"  # the node that ends a normal turn
    if request_approval is not None:  # a turn that asked for a ticket ends by recording the request for a person to approve
        from app.approvals import proposals_in

        g.add_node("request_approval", request_approval)
        g.add_conditional_edges(last, lambda state: "request_approval" if proposals_in(state) else END,
                                {"request_approval": "request_approval", END: END})
        g.add_edge("request_approval", END)
    else:
        g.add_edge(last, END)
    g.add_edge("handoff", END)

    # thread_id = "<user_id>:<conversation_id>" (see service.thread_id_for), so each customer
    # has their own memory and nobody can reach another customer's thread
    return g.compile(checkpointer=checkpointer or MemorySaver())


def build_default_graph(checkpointer=None, handoff_notifier=None, keep_verify_raw=False, verify_node=None, observe_node=None,
                        repo=None, approvals=None, on_approval_created=None):
    """`checkpointer=None` keeps the memory in process (the eval does that, so it never writes
    conversations to disk). The API passes the persistent one, see `app.main.lifespan`.
    `handoff_notifier=None` only appends a redacted audit line to the JSONL file; the API passes one that also queues the
    handoff in the SQLite store the operators work from.
    `repo`: the customer data and tickets (shared with the approval graph, which opens the ticket once it is approved).
    `approvals`: an `ApprovalStore`. With it, `create_support_ticket` only PROPOSES the categories in
    APPROVAL_REQUIRED_CATEGORIES and the turn ends by recording the request; without it (the eval) tickets open at once."""
    from app.agents.knowledge import make_knowledge_node
    from app.approvals import make_request_approval_node
    from app.agents.observe import make_observe_node
    from app.agents.router import make_router_node
    from app.agents.summarizer import make_summarizer_node
    from app.agents.support import make_support_node
    from app.agents.synthesizer import make_synthesizer_node
    from app.agents.verifier import make_verify_node
    from app.config import get_settings
    from app.data.repository import CustomerRepository
    from app.handoff import make_handoff_node

    repo = repo or CustomerRepository(tickets_file=get_settings().tickets_file, dedup_window_s=get_settings().ticket_dedup_window_s)  # shared, so handoff tickets show up for Support
    categories = get_settings().approval_categories if approvals is not None else frozenset()
    return build_graph(make_router_node(), make_knowledge_node(), make_support_node(repo=repo, approval_categories=categories),
                       make_synthesizer_node(), make_handoff_node(repo=repo, notifier=handoff_notifier), checkpointer=checkpointer,
                       summarizer=make_summarizer_node(), verify=verify_node or make_verify_node(keep_raw=keep_verify_raw),
                       observe=observe_node or make_observe_node(keep_raw=keep_verify_raw),
                       request_approval=(make_request_approval_node(approvals, get_settings().approval_ttl_minutes * 60, on_approval_created)
                                         if approvals is not None else None))
