"""The observe step: one test per branch, with a scripted checker and a scripted knowledge base. No network."""
import pytest
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage
from langgraph.graph.message import add_messages

from app.agents.observe import OFFERS, format_hits, getnet_context, has_steps, is_step, make_observe_node
from app.agents.verifier import Statement, Verification, make_verify_node
from app.graph import build_graph
from app.guard import REFUSALS
from app.handoff import make_handoff_node

EVIDENCE = ('[kb] Receivables advance lets you receive today the money of sales that would be paid later. A fee applies. '
            '[tool] {"status":"offline","signal_strength":"weak","last_error":"NET_TIMEOUT"}')
ANSWER = ("Receivables advance lets you receive today the money of sales that would be paid later.\n"
          "1. Restart the terminal.\n2. Use the 2.4 GHz band.")
FACT = "receive today the money of sales that would be paid later"


class Doc:
    def __init__(self, text, url="https://site.getnet.com.br/help", title="Help"):
        self.page_content, self.metadata = text, {"url": url, "title": title}


class Scripted:
    """A model that returns the audits and the rewrites it was given, in order, and remembers what it was asked."""

    def __init__(self, audits=(), replies=()):
        self.audits, self.replies, self.calls, self.bodies = list(audits), list(replies), [], []

    def with_structured_output(self, schema, method=None):
        outer = self

        class _Checker:
            def invoke(self, messages, config=None):
                outer.calls.append("audit")
                outer.bodies.append(messages[-1].content)
                item = outer.audits.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item

        return _Checker()

    def invoke(self, messages, config=None):
        self.calls.append("write")
        self.bodies.append(messages[0].content)
        return AIMessage(content=self.replies.pop(0))


class KB:
    """The knowledge base: counts the searches (each is ONE Voyage call)."""

    def __init__(self, docs=(), fail=None):
        self.docs, self.fail, self.queries = list(docs), fail, []

    def __call__(self, query, k):
        self.queries.append((query, k))
        if self.fail:
            raise self.fail
        return [(d, 0.4) for d in self.docs]


def audit(*pairs):
    return Verification(statements=[Statement(statement=s, evidence_quote=q) for s, q in pairs])


def state(question=ANSWER and "My terminal is offline, what do I do?", answer=ANSWER, contexts=(EVIDENCE,), **extra):
    messages = add_messages([], [HumanMessage(content=question), AIMessage(content=answer)])
    outputs = {"knowledge": {"answer": answer, "contexts": list(contexts), "sources": [],
                             "tool_calls": [{"name": "search_getnet_kb", "args": {"query": "q"}}]}} if contexts else {}
    return {"messages": messages, "final_answer": answer, "agent_outputs": outputs, "language": "English", "plan": ["knowledge"], **extra}


def observe(model, kb=None, cap=2, **kw):
    return make_observe_node(llm=model, retrieve_fn=kb or KB(), replan_max=cap, keep_raw=kw.pop("keep_raw", False))


UNSUPPORTED_STEPS = audit((FACT, FACT), ("restart the terminal", ""), ("2.4 GHz band", ""))


# ---------- when it does nothing ----------

def test_with_replan_max_zero_it_does_nothing_and_asks_nobody():
    model, kb = Scripted(), KB()
    assert make_observe_node(llm=model, retrieve_fn=kb, replan_max=0)(state()) == {}
    assert model.calls == [] and kb.queries == []


def test_the_setting_defaults_to_off(monkeypatch):
    from app.config import get_settings

    monkeypatch.delenv("REPLAN_MAX", raising=False)
    get_settings.cache_clear()
    assert get_settings().replan_max == 0


@pytest.mark.parametrize("bad", [dict(contexts=()), dict(answer=""), dict(synth_error="boom"), dict(handoff={"id": "H"})])
def test_without_evidence_an_answer_or_with_a_handoff_it_leaves_the_turn_alone(bad):
    model = Scripted()
    assert observe(model)(state(**bad)) == {} and model.calls == []


def test_a_leaked_prompt_is_left_to_the_guard_of_the_verify_node():
    from app.prompts import KNOWLEDGE_PROMPT
    from app.guard import words

    leak = " ".join(words(KNOWLEDGE_PROMPT)[3:25])
    model = Scripted()
    assert observe(model)(state(answer=leak)) == {} and model.calls == []


def test_a_checker_that_fails_leaves_the_answer_to_the_verify_node_which_fails_open_too():
    model = Scripted(audits=[RuntimeError("provider down")])
    assert observe(model)(state()) == {}


def test_a_web_answer_is_left_to_the_verify_node_no_getnet_search_and_no_ticket_offer():
    st = state(answer="The euro trades around BRL 6.24.", contexts=("[web] euro today 6.30",))
    st["agent_outputs"]["knowledge"]["tool_calls"] = [{"name": "web_search", "args": {"query": "euro"}}]
    model, kb = Scripted(), KB([Doc("x")])
    assert observe(model, kb)(st) == {} and model.calls == [] and kb.queries == []


def test_a_support_only_turn_is_observed_even_without_a_knowledge_search():
    st = state(answer="Your terminal is offline.\n1. Restart the terminal.", contexts=('[tool] {"status":"offline"}',))
    st["agent_outputs"] = {"support": {"answer": "x", "contexts": ['[tool] {"status":"offline"}'], "tool_calls": [{"name": "get_terminal_status", "args": {}}]}}
    model = Scripted(audits=[audit(("terminal is offline", "status offline"), ("restart the terminal", ""))] * 2, replies=["Your terminal is offline."])
    update = observe(model, KB([Doc("Unrelated.")]))(st)
    assert update["observe"]["replans"][0].startswith("retry_kb: 1 unsupported") and update["final_answer"].startswith("Your terminal is offline.")


# ---------- continue: nothing unsupported ----------

def test_an_answer_with_everything_backed_just_continues_and_the_verify_node_does_not_audit_it_again():
    model, kb = Scripted(audits=[audit((FACT, FACT), ("a fee applies", "A fee applies."))]), KB()
    update = observe(model, kb)(state(answer="Receivables advance lets you receive today the money of sales that would be paid later. A fee applies."))
    assert update["observe"]["claims"] == [] and update["observe"]["replans"] == [] and kb.queries == []
    assert update["verify_note"] == "" and "final_answer" not in update and model.calls == ["audit"]


# ---------- replan 1: retry the knowledge base ONCE with the claims grouped ----------

def test_unsupported_claims_make_one_search_with_all_of_them_and_only_what_is_still_unsupported_is_deleted():
    new = Doc("To reconnect, restart the terminal and wait two minutes.", url="https://site.getnet.com.br/reconnect", title="Reconnect")
    model = Scripted(audits=[UNSUPPORTED_STEPS, audit((FACT, FACT), ("restart the terminal", "restart the terminal and wait"), ("2.4 GHz band", ""))],
                     replies=["Receivables advance lets you receive today the money of sales that would be paid later.\n1. Restart the terminal."])
    kb = KB([new])
    st = state()
    update = observe(model, kb, cap=1, keep_raw=True)(st)
    assert kb.queries == [("restart the terminal ; 2.4 GHz band", 5)]  # ONE search, the unsupported claims grouped
    assert model.calls == ["audit", "audit", "write"]
    assert "restart the terminal and wait two minutes" in model.bodies[1]  # re-checked against the NEW evidence
    assert "- 2.4 GHz band" in model.bodies[2] and "- restart the terminal" not in model.bodies[2]  # only what is still unsupported goes
    obs = update["observe"]
    assert obs["claims"] == ["2.4 GHz band"] and obs["replans"] == ["retry_kb: 2 unsupported -> 1 passages -> 1 still unsupported"]
    assert obs["tool_calls"] == [{"name": "search_getnet_kb", "args": {"query": "restart the terminal ; 2.4 GHz band"}}]
    assert obs["sources"][0]["url"] == "https://site.getnet.com.br/reconnect" and "restart the terminal and wait" in obs["evidence"][0]
    assert update["verify_note"].startswith("revised:1 [2.4 GHz band]") and "verify_raw" in update
    assert update["messages"][0].id == st["messages"][-1].id and update["messages"][0].content.endswith("Restart the terminal.")  # replaces the draft


def test_if_the_new_evidence_backs_everything_nothing_is_deleted():
    model = Scripted(audits=[UNSUPPORTED_STEPS, audit((FACT, FACT), ("restart", "restart the terminal"), ("band", "use the 2.4 GHz band"))])
    update = observe(model, KB([Doc("Restart the terminal. Use the 2.4 GHz band when you can.")]), cap=1)(state())
    assert update["observe"]["claims"] == [] and "final_answer" not in update and model.calls == ["audit", "audit"]  # no rewrite
    assert update["observe"]["replans"][0].endswith("-> 1 passages -> 0 still unsupported")


def test_if_the_knowledge_base_fails_it_goes_on_with_the_original_claims():
    model = Scripted(audits=[UNSUPPORTED_STEPS], replies=["Receivables advance lets you receive today the money of sales that would be paid later."])
    update = observe(model, KB(fail=RuntimeError("voyage rate limit")), cap=1)(state())
    assert update["observe"]["replans"] == ["retry_kb_failed: RuntimeError"]
    assert update["observe"]["claims"] == ["restart the terminal", "2.4 GHz band"] and model.calls == ["audit", "write"]
    assert update["verify_note"].startswith("revised:2")


def test_if_the_search_finds_nothing_the_claims_stand_and_the_answer_is_not_audited_twice():
    model = Scripted(audits=[UNSUPPORTED_STEPS], replies=["Rewritten."])
    update = observe(model, KB([]), cap=1)(state())
    assert update["observe"]["replans"] == ["retry_kb: 2 unsupported -> 0 passages -> 2 still unsupported"]
    assert model.calls == ["audit", "write"] and update["final_answer"] == "Rewritten."


# ---------- replan 2: offer a ticket, or escalate ----------

@pytest.mark.parametrize("question,code", [("My terminal is offline, can you help me with that please?", "en"),
                                           ("Meu terminal não conecta, você pode me ajudar com isso?", "pt"),
                                           ("Mi terminal no conecta, ¿puedes ayudarme con eso? Tengo un problema, gracias.", "es")])
def test_when_no_step_is_left_but_something_is_backed_a_ticket_is_offered_in_the_customers_language(question, code):
    answer = "Your terminal is offline.\n1. Restart the terminal.\n2. Check the 2.4 GHz band."
    audit_ = audit(("your terminal is offline", "status offline"), ("restart the terminal", ""), ("2.4 GHz band", ""))
    model = Scripted(audits=[audit_, audit_], replies=["Your terminal is offline."])
    update = observe(model, KB([Doc("Unrelated page about Pix.")]))(state(question=question, answer=answer))
    assert update["final_answer"] == "Your terminal is offline.\n\n" + OFFERS[code]
    assert update["observe"]["replans"][-1] == "offer_ticket: no step is left" and update["verify_note"].endswith("offered_ticket")
    assert "escalate" not in update["observe"]


def test_when_nothing_in_the_answer_is_backed_the_answer_is_not_sent_the_case_goes_to_a_human():
    model = Scripted(audits=[UNSUPPORTED_STEPS, UNSUPPORTED_STEPS.model_copy(update={"statements": [Statement(statement="restart the terminal"),
                                                                                                    Statement(statement="2.4 GHz band")]})])
    st = state(answer="1. Restart the terminal.\n2. Use the 2.4 GHz band.")
    update = observe(model, KB([Doc("Unrelated page about Pix.")]))(st)
    assert update["observe"]["escalate"] is True and model.calls == ["audit", "audit"]  # no rewrite: nothing is worth keeping
    assert isinstance(update["messages"][0], RemoveMessage) and update["messages"][0].id == st["messages"][-1].id
    assert update["observe"]["replans"][-1] == "escalate: nothing in the answer is backed by the evidence"
    assert update["verify_note"].startswith("escalated; revised:2")


def test_with_replan_max_one_it_retries_but_never_offers_a_ticket_or_escalates():
    answer = "Your terminal is offline.\n1. Restart the terminal."
    audit_ = audit(("your terminal is offline", "status offline"), ("restart the terminal", ""))
    model = Scripted(audits=[audit_, audit_], replies=["Your terminal is offline."])
    update = observe(model, KB([Doc("Unrelated.")]), cap=1)(state(answer=answer))
    assert update["final_answer"] == "Your terminal is offline." and "escalate" not in update["observe"]
    assert [r.split(":")[0] for r in update["observe"]["replans"]] == ["retry_kb"]  # the cap is two, and this is the first


def test_an_answer_without_steps_or_figures_gets_no_ticket_offer_when_a_claim_is_deleted():
    answer = "Getnet supports Pix by approximation and also Pix Automático."
    model = Scripted(audits=[audit(("pix by approximation", "A fee applies."), ("Pix Automático", "")),
                             audit(("pix by approximation", "A fee applies."), ("Pix Automático", ""))], replies=["Getnet supports Pix by approximation."])
    update = observe(model, KB([Doc("Nothing about it.")]))(state(answer=answer))
    assert "offer_ticket" not in " ".join(update["observe"]["replans"]) and update["final_answer"] == "Getnet supports Pix by approximation."


# ---------- the pieces ----------

@pytest.mark.parametrize("text,expected", [("Restart the terminal.", True), ("Please press the green key", True), ("Open Menu > Support", True),
                                           ("Reinicie o terminal e aguarde", True), ("Boost signal strength - Move the terminal closer.", True),
                                           ("Your next payout is R$ 10", False), ("Terminal GS-88231-RS was last seen 95 minutes ago.", False),
                                           ("Getnet supports Pix by approximation.", False), ("The terminal is offline.", False)])
def test_what_counts_as_a_step_figures_and_serial_numbers_are_facts_not_fixes(text, expected):
    assert is_step(text) is expected


def test_has_steps_looks_at_every_segment():
    assert has_steps("The terminal is offline.\n1. Restart the terminal.") and not has_steps("The terminal is offline, last seen 95 minutes ago.")


def test_a_ticket_is_offered_when_the_steps_go_even_if_figures_and_serial_numbers_remain():
    answer = "Terminal GS-88231-RS is offline, last seen 95 minutes ago.\n1. Restart the terminal.\n2. Check the 2.4 GHz band."
    audit_ = audit(("terminal is offline", "status offline"), ("restart the terminal", ""), ("2.4 GHz band", ""))
    model = Scripted(audits=[audit_, audit_], replies=["Terminal GS-88231-RS is offline, last seen 95 minutes ago."])
    update = observe(model, KB([Doc("Unrelated.")]))(state(answer=answer))
    assert update["final_answer"].endswith(OFFERS["en"]) and update["observe"]["replans"][-1].startswith("offer_ticket")


def test_getnet_context_means_the_knowledge_base_or_the_support_agent():
    assert getnet_context({"agent_outputs": {"knowledge": {"tool_calls": [{"name": "search_getnet_kb"}]}}})
    assert getnet_context({"agent_outputs": {"support": {"tool_calls": []}}})
    assert not getnet_context({"agent_outputs": {"knowledge": {"tool_calls": [{"name": "web_search"}]}}}) and not getnet_context({})


def test_the_passages_are_formatted_like_the_knowledge_tool_and_their_sources_are_deduplicated():
    blocks, sources = format_hits([(Doc("text A", "https://x/a", "A"), 0.4), (Doc("text B", "https://x/a", "A"), 0.5), (Doc("text C", "https://x/c", "C"), 0.3)])
    assert blocks[0] == "[1] (relevance=0.60) A <https://x/a>\ntext A" and [s["url"] for s in sources] == ["https://x/a", "https://x/c"]


# ---------- the verify node does not audit again what observe already audited ----------

class NoAudit:
    def with_structured_output(self, *a, **k):
        raise AssertionError("the verify node must not audit an answer the observe step already audited")

    def invoke(self, *a, **k):
        raise AssertionError("no model call expected")


def test_the_verify_node_keeps_the_note_of_the_observe_step_and_does_not_call_the_checker():
    st = state(answer="Your terminal is offline.", verify_note="revised:1 [2.4 GHz band]", observe={"claims": ["2.4 GHz band"], "replans": []})
    assert make_verify_node(llm=NoAudit())(st) == {"verify_note": "revised:1 [2.4 GHz band]"}


# ---------- in the graph ----------

class Notifier:
    def __init__(self):
        self.records = []

    def notify(self, record):
        self.records.append(record)


def graph_for(observe_node, repo, verify_model=None):
    def router(s):
        return {"plan": ["knowledge"], "step": 0, "language": "English", "agent_outputs": {}, "final_answer": "", "wants_human": False,
                "handoff": None, "synth_error": "", "verify_note": "", "observe": {}}

    def knowledge(s):
        return {"agent_outputs": {"knowledge": {"answer": ANSWER, "contexts": [EVIDENCE], "sources": [],
                                                "tool_calls": [{"name": "search_getnet_kb", "args": {}}]}}, "step": 1}

    def synthesizer(s):
        return {"final_answer": ANSWER, "messages": [AIMessage(content=ANSWER)]}

    notifier = Notifier()
    g = build_graph(router, knowledge, lambda s: {}, synthesizer, make_handoff_node(repo=repo, notifier=notifier),
                    verify=make_verify_node(llm=verify_model or NoAudit()), observe=observe_node)
    return g, notifier


def test_in_the_graph_an_escalation_replaces_the_draft_with_the_handoff_notice(repo):
    nothing_backed = Verification(statements=[Statement(statement="restart the terminal"), Statement(statement="2.4 GHz band")])
    model = Scripted(audits=[nothing_backed, nothing_backed])
    g, notifier = graph_for(observe(model, KB([Doc("Unrelated.")])), repo)
    out = g.invoke({"messages": [HumanMessage(content="My terminal is offline, what do I do?")], "user_id": "cliente1988"}, {"configurable": {"thread_id": "t1"}})
    assert out["handoff"]["reason"] == "agent_unresolved" and "passed your case to a human agent" in out["final_answer"]
    assert [m.type for m in out["messages"]] == ["human", "ai"] and out["messages"][-1].content == out["final_answer"]  # the draft is gone
    assert "Restart the terminal" not in out["final_answer"] and len(notifier.records) == 1


def test_in_the_graph_a_clean_answer_goes_through_observe_and_verify_untouched(repo):
    model = Scripted(audits=[audit((FACT, FACT), ("restart", "restart"), ("band", "band"))])
    # every quote must be in the evidence: reuse words that are
    clean = Verification(statements=[Statement(statement="x", evidence_quote="A fee applies.")])
    model = Scripted(audits=[clean])
    g, notifier = graph_for(observe(model, KB()), repo)
    out = g.invoke({"messages": [HumanMessage(content="How does advance work?")], "user_id": "cliente1988"}, {"configurable": {"thread_id": "t2"}})
    assert out["final_answer"] == ANSWER and not out.get("handoff") and out["observe"]["claims"] == [] and notifier.records == []


def test_in_the_graph_replan_max_zero_is_the_graph_it_was(repo):
    g, _ = graph_for(make_observe_node(llm=Scripted(), retrieve_fn=KB(), replan_max=0), repo, verify_model=Scripted(audits=[Verification()]))
    out = g.invoke({"messages": [HumanMessage(content="How does advance work?")], "user_id": "cliente1988"}, {"configurable": {"thread_id": "t3"}})
    assert out["final_answer"] == ANSWER and out.get("observe") in (None, {})  # the verify node did its own audit


# ---------- what the API and the eval see ----------

def test_the_response_shows_the_replans_and_the_passages_the_answer_rests_on():
    from app.service import ChatService

    class FakeGraph:
        def invoke(self, inputs, config):
            return {"final_answer": "ok", "plan": ["knowledge"], "agent_outputs": {"knowledge": {"answer": "x", "sources": [], "tool_calls": []}},
                    "observe": {"replans": ["retry_kb: 1 unsupported -> 1 passages -> 0 still unsupported"], "claims": [],
                                "sources": [{"url": "https://x/a", "title": "A", "type": "kb", "relevance": 0.6}],
                                "tool_calls": [{"name": "search_getnet_kb", "args": {"query": "q"}}]}}

    resp = ChatService(FakeGraph()).handle("hi", "cliente1988")
    assert resp.routing.replans == ["retry_kb: 1 unsupported -> 1 passages -> 0 still unsupported"]
    assert [s.url for s in resp.sources] == ["https://x/a"] and [(t.agent, t.name) for t in resp.tool_calls] == [("observe", "search_getnet_kb")]

    class Plain:
        def invoke(self, inputs, config):
            return {"final_answer": "ok", "plan": [], "agent_outputs": {}}

    assert ChatService(Plain()).handle("hi", "u").routing.replans == []  # nothing observed: nothing listed


def test_the_eval_row_carries_the_replans_and_the_judge_sees_the_new_passages():
    from eval.run_eval import evaluate

    class Judge:
        def __init__(self):
            self.evidence = None

        def score(self, question, answer, evidence, criteria="", callbacks=None):
            self.evidence = evidence
            return {"verdict": "pass", "relevance": 5, "groundedness": 5, "completeness": 5, "tone": 5}

    class Service:
        def run_raw(self, message, user_id, thread_id=None, **kw):
            return {"plan": ["knowledge"], "final_answer": "a", "router_error": "", "synth_error": "",
                    "agent_outputs": {"knowledge": {"answer": "a", "error": None, "tool_calls": [], "contexts": ["old evidence"]}},
                    "observe": {"replans": ["retry_kb: 1 unsupported -> 1 passages -> 0 still unsupported"], "evidence": ["new passage"],
                                "tool_calls": [{"name": "search_getnet_kb", "args": {"query": "q"}}]}}, 5

    judge = Judge()
    rows = evaluate([{"id": "c", "user_id": "u", "message": "hi", "expected_agents": ["knowledge"]}], Service(), judge, pause=0)
    assert judge.evidence == ["old evidence", "new passage"] and rows[0]["replans"] == ["retry_kb: 1 unsupported -> 1 passages -> 0 still unsupported"]
    assert rows[0]["tools_called"] == ["search_getnet_kb"] and rows[0]["tool_calls"][0]["agent"] == "observe"


def test_regular_only_leaves_the_holdout_out_and_keeps_the_injection_cases():
    from eval.run_eval import load_cases

    ids = [c["id"] for c in load_cases(None, regular_only=True)]
    assert not any(i.startswith("ho") for i in ids) and any(i.startswith("inj") for i in ids)
    assert len(load_cases(None)) > len(ids)
    assert REFUSALS  # (the guard's refusals are untouched by this step)
