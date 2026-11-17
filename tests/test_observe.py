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


# ---------- replan 2, the add_support action ----------

PAYOUT = "Your next payout is R$ 10."
ACCOUNT_ANSWER = f"{ANSWER.splitlines()[0]}\n{PAYOUT}"
SUPPORT_CTX = '[get_customer_profile] {"next_payout": "R$ 10 on Friday"}'
MERGED = "Receivables advance lets you receive today the money of sales that would be paid later. Your next payout is R$ 10 on Friday."
ACCOUNT_AUDIT = audit((FACT, FACT), (PAYOUT, ""))
SHORT = "Receivables advance lets you receive today the money of sales."


class Support:
    """The graph's support node: records the state it was given and returns a scripted result (or raises)."""

    def __init__(self, result=None, boom=None):
        self.states, self.boom = [], boom
        self.result = result if result is not None else {"agent": "support", "answer": "Your next payout is R$ 10 on Friday.",
                                                         "contexts": [SUPPORT_CTX], "sources": [], "unresolved": False,
                                                         "tool_calls": [{"name": "get_customer_profile", "args": {}}]}

    def __call__(self, st):
        self.states.append(st)
        if self.boom:
            raise self.boom
        return {"agent_outputs": {"support": self.result}, "step": 1}


class Synth:
    """The graph's synthesizer node: records the state it was given and returns the merged text."""

    def __init__(self, text=MERGED, synth_error=""):
        self.states, self.text, self.synth_error = [], text, synth_error

    def __call__(self, st):
        self.states.append(st)
        return {"final_answer": self.text, "messages": [AIMessage(content=self.text)], "synth_error": self.synth_error}


def account_node(model, kb=None, cap=2, support=None, synth=None):
    return make_observe_node(llm=model, retrieve_fn=kb or KB([Doc("Unrelated page about Pix.")]), replan_max=cap,
                             support_fn=support or Support(), synth_fn=synth or Synth())


def account_state(**extra):
    return state(answer=ACCOUNT_ANSWER, user_id="cliente1988", **extra)


MERGE_CLEAN = audit((FACT, FACT), ("next payout is R$ 10 on Friday", "next_payout: R$ 10 on Friday"))


def merge_happy_path(cap=2):
    model = Scripted(audits=[ACCOUNT_AUDIT, ACCOUNT_AUDIT, MERGE_CLEAN])
    sup, syn, kb = Support(), Synth(), KB([Doc("Unrelated page about Pix.")])
    st = account_state()
    return st, model, sup, syn, kb, account_node(model, kb, cap, sup, syn)(st)


def test_replan_two_adds_the_support_agent_when_an_unbacked_claim_is_about_the_account():
    st, model, sup, syn, kb, update = merge_happy_path()
    assert len(sup.states) == 1 and len(syn.states) == 1 and len(kb.queries) == 1
    assert update["final_answer"] == MERGED and update["verify_note"] == "added_support"
    assert list(update["agent_outputs"]) == ["support", "knowledge"]  # support first, as when the router plans both
    assert update["messages"][0].id == st["messages"][-1].id and update["messages"][0].content == MERGED  # the draft is replaced
    assert "plan" not in update  # routing.plan is what the router decided
    replans = update["observe"]["replans"]
    assert [r.split(":")[0] for r in replans] == ["retry_kb", "add_support"]
    assert replans[-1].startswith("add_support: 1 unsupported -> support ran (1 tool calls)")
    assert "escalate" not in update["observe"] and update["observe"]["claims"] == []
    assert "next_payout" in model.bodies[2] and "Unrelated page about Pix" in model.bodies[2]  # the merge is audited against ALL the evidence
    assert model.calls == ["audit", "audit", "audit"]  # no rewrite: nothing is left unsupported


def test_the_support_agent_gets_the_authenticated_user_and_none_of_the_drafts_unbacked_claims():
    _, _, sup, syn, _, _ = merge_happy_path()
    given = sup.states[0]
    assert given["user_id"] == "cliente1988" and given["agent_outputs"] == {}  # no "findings" from the draft as prior context
    assert [m.type for m in given["messages"]] == ["human"]  # and the draft is not in its dialogue
    merged_in = syn.states[0]
    assert merged_in["plan"] == ["support", "knowledge"] and list(merged_in["agent_outputs"]) == ["support", "knowledge"]
    assert [m.type for m in merged_in["messages"]] == ["human"]


def test_the_merge_that_still_has_unbacked_claims_is_rewritten_once_and_no_ticket_is_offered():
    still = audit((FACT, FACT), ("Your next payout is R$ 10 on Friday", ""))
    model = Scripted(audits=[ACCOUNT_AUDIT, ACCOUNT_AUDIT, still], replies=[SHORT])
    update = account_node(model)(account_state())
    assert update["final_answer"] == SHORT
    assert update["verify_note"].startswith("revised:1 [") and update["verify_note"].endswith("; added_support")
    assert model.calls == ["audit", "audit", "audit", "write"] and "offer_ticket" not in " ".join(update["observe"]["replans"])


def test_add_support_is_the_only_action_of_replan_two_no_offer_and_no_escalation_next_to_it():
    steps = f"{ANSWER.splitlines()[0]}\n{PAYOUT}\n1. Restart the terminal."
    audit_ = audit((FACT, FACT), (PAYOUT, ""), ("restart the terminal", ""))
    final = audit((FACT, FACT), ("restart the terminal", ""))
    model = Scripted(audits=[audit_, audit_, final], replies=[SHORT])
    update = account_node(model, synth=Synth(MERGED + "\n1. Restart the terminal."))(state(answer=steps, user_id="cliente1988"))
    kinds = [r.split(":")[0] for r in update["observe"]["replans"]]
    assert kinds == ["retry_kb", "add_support"] and "escalate" not in update["observe"] and "offered_ticket" not in update["verify_note"]


@pytest.mark.parametrize("boom,result,synth_error", [
    (RuntimeError("tool down"), None, ""),
    (None, {"agent": "support", "answer": "", "error": "TimeoutError: slow", "contexts": [], "tool_calls": []}, ""),
    (None, None, "RuntimeError: merge"),
])
def test_when_support_or_the_merge_fail_the_answer_and_the_state_are_what_replan_one_would_leave(boom, result, synth_error):
    steps = f"{ANSWER.splitlines()[0]}\n{PAYOUT}\n1. Restart the terminal."
    audit_ = audit((FACT, FACT), (PAYOUT, ""), ("restart the terminal", ""))
    model = Scripted(audits=[audit_, audit_], replies=[SHORT])
    update = account_node(model, support=Support(result=result, boom=boom), synth=Synth(synth_error=synth_error))(state(answer=steps, user_id="cliente1988"))
    assert "agent_outputs" not in update and "plan" not in update and "escalate" not in update["observe"]  # nothing of the attempt is kept
    replans = update["observe"]["replans"]
    assert [r.split(":")[0] for r in replans] == ["retry_kb", "add_support_failed", "offer_ticket"]  # the attempt did not spend the action
    assert update["final_answer"] == SHORT + "\n\n" + OFFERS["en"] and update["verify_note"].endswith("offered_ticket")  # as at 22423e2


@pytest.mark.parametrize("boom,result,synth_error", [
    (RuntimeError("tool down"), None, ""),
    (None, {"agent": "support", "answer": "", "error": "TimeoutError: slow", "contexts": [], "tool_calls": []}, ""),
    (None, None, "RuntimeError: merge"),
])
def test_when_support_fails_and_nothing_is_backed_the_case_is_escalated_as_it_was_at_22423e2(boom, result, synth_error):
    nothing = Verification(statements=[Statement(statement=PAYOUT), Statement(statement="restart the terminal")])
    model = Scripted(audits=[nothing, nothing])
    st = state(answer=f"{PAYOUT}\n1. Restart the terminal.", user_id="cliente1988")
    update = account_node(model, support=Support(result=result, boom=boom), synth=Synth(synth_error=synth_error))(st)
    replans = update["observe"]["replans"]
    assert [r.split(":")[0] for r in replans] == ["retry_kb", "add_support_failed", "escalate"] and update["observe"]["escalate"] is True
    assert "agent_outputs" not in update and isinstance(update["messages"][0], RemoveMessage) and update["verify_note"].startswith("escalated; revised:")


def test_an_audit_that_fails_after_the_merge_leaves_the_draft_as_it_was():
    model = Scripted(audits=[ACCOUNT_AUDIT, ACCOUNT_AUDIT, RuntimeError("checker down")], replies=[SHORT])
    update = account_node(model)(account_state())
    assert "agent_outputs" not in update and "add_support_failed: RuntimeError" in update["observe"]["replans"]
    assert update["final_answer"] == SHORT  # no steps in this answer, so no ticket offer: only the deletion of the unbacked claims


def test_it_does_not_add_support_when_it_is_planned_there_is_no_customer_or_the_claim_is_not_about_the_account():
    for st, why in [(account_state(plan=["knowledge", "support"]), "planned"),
                    (state(answer=ACCOUNT_ANSWER), "no user"),
                    (state(answer=ACCOUNT_ANSWER, user_id=""), "empty user")]:
        sup = Support()
        model = Scripted(audits=[ACCOUNT_AUDIT, ACCOUNT_AUDIT], replies=[SHORT])
        update = account_node(model, support=sup)(st)
        assert sup.states == [] and "add_support" not in " ".join(update["observe"]["replans"]), why
    sup, model = Support(), Scripted(audits=[UNSUPPORTED_STEPS, UNSUPPORTED_STEPS], replies=["Receivables advance."])
    update = account_node(model, support=sup)(state(answer=ANSWER, user_id="cliente1988"))  # the unbacked claims are steps: how-to, not account data
    assert sup.states == [] and update["observe"]["replans"][-1].startswith("offer_ticket")  # the old second action still applies


def test_with_the_support_agent_already_in_the_outputs_it_is_not_run_again():
    st = account_state()
    st["agent_outputs"]["support"] = {"answer": "x", "contexts": ['[tool] {"a": 1}'], "tool_calls": [{"name": "get_customer_profile", "args": {}}]}
    sup = Support()
    account_node(Scripted(audits=[ACCOUNT_AUDIT, ACCOUNT_AUDIT], replies=[SHORT]), support=sup)(st)
    assert sup.states == []


def test_replan_one_never_adds_support_and_a_node_built_without_the_graphs_nodes_has_no_such_action():
    sup = Support()
    account_node(Scripted(audits=[ACCOUNT_AUDIT, ACCOUNT_AUDIT], replies=[SHORT]), cap=1, support=sup)(account_state())
    assert sup.states == []
    model = Scripted(audits=[ACCOUNT_AUDIT, ACCOUNT_AUDIT], replies=[SHORT])
    update = observe(model, KB([Doc("Unrelated page about Pix.")]))(account_state())  # no support_fn / synth_fn: as before
    assert "add_support" not in " ".join(update["observe"]["replans"]) and "agent_outputs" not in update


def test_replan_max_above_two_behaves_exactly_like_two_each_action_runs_at_most_once():
    results = {}
    for cap in (2, 3, 5):
        _, model, sup, syn, kb, update = merge_happy_path(cap)
        assert (len(sup.states), len(syn.states), len(kb.queries), model.calls) == (1, 1, 1, ["audit", "audit", "audit"]), cap
        results[cap] = {**update, "messages": [(m.type, m.content) for m in update["messages"]]}  # each run has its own draft id
    assert results[3] == results[2] and results[5] == results[2]  # same update, field for field
    for cap in (2, 5):  # and the old second action is the same at 5 as at 2
        answer = "Your terminal is offline.\n1. Restart the terminal.\n2. Check the 2.4 GHz band."
        audit_ = audit(("your terminal is offline", "status offline"), ("restart the terminal", ""), ("2.4 GHz band", ""))
        model, sup = Scripted(audits=[audit_, audit_], replies=["Your terminal is offline."]), Support()
        update = account_node(model, KB([Doc("Unrelated page about Pix.")]), cap, support=sup)(state(answer=answer, user_id="cliente1988"))
        assert update["final_answer"].endswith(OFFERS["en"]) and sup.states == [], cap
        assert [r.split(":")[0] for r in update["observe"]["replans"]] == ["retry_kb", "offer_ticket"], cap


def account_graph(node, repo):
    def router(s):
        return {"plan": ["knowledge"], "step": 0, "language": "English", "agent_outputs": {}, "final_answer": "", "wants_human": False,
                "handoff": None, "synth_error": "", "verify_note": "", "observe": {}}

    def knowledge(s):
        return {"agent_outputs": {"knowledge": {"answer": ACCOUNT_ANSWER, "contexts": [EVIDENCE], "sources": [],
                                                "tool_calls": [{"name": "search_getnet_kb", "args": {}}]}}, "step": 1}

    def synthesizer(s):
        return {"final_answer": ACCOUNT_ANSWER, "messages": [AIMessage(content=ACCOUNT_ANSWER)]}

    notifier = Notifier()
    return build_graph(router, knowledge, lambda s: {}, synthesizer, make_handoff_node(repo=repo, notifier=notifier),
                       verify=make_verify_node(llm=NoAudit()), observe=node), notifier


def test_in_the_graph_the_merged_answer_replaces_the_draft_and_the_plan_is_still_the_routers(repo):
    g, _ = account_graph(account_node(Scripted(audits=[ACCOUNT_AUDIT, ACCOUNT_AUDIT, MERGE_CLEAN])), repo)
    out = g.invoke({"messages": [HumanMessage(content="When is my next payout?")], "user_id": "cliente1988"}, {"configurable": {"thread_id": "t10"}})
    assert out["final_answer"] == MERGED and [m.content for m in out["messages"] if m.type == "ai"] == [MERGED]
    assert out["plan"] == ["knowledge"] and list(out["agent_outputs"]) == ["support", "knowledge"]  # visible in agents_used and replans; the plan is untouched
    assert out["observe"]["replans"][-1].startswith("add_support:") and out["verify_note"] == "added_support"


def test_a_support_agent_that_cannot_resolve_it_sends_the_case_to_a_human_through_the_existing_edge(repo):
    import json

    from app.approvals import PENDING_APPROVAL, TOOL_CONTEXT_PREFIX

    pending = TOOL_CONTEXT_PREFIX + json.dumps({"status": PENDING_APPROVAL, "category": "billing", "summary": "Payout is wrong"})
    sup = Support(result={"agent": "support", "answer": "escalated", "unresolved": True, "contexts": [pending],
                          "tool_calls": [{"name": "escalate_to_human", "args": {}}]})
    syn = Synth()
    g, notifier = account_graph(account_node(Scripted(audits=[ACCOUNT_AUDIT, ACCOUNT_AUDIT]), support=sup, synth=syn), repo)
    out = g.invoke({"messages": [HumanMessage(content="When is my next payout?")], "user_id": "cliente1988"}, {"configurable": {"thread_id": "t9"}})
    assert syn.states == []  # no merge: a person takes the case
    assert out["handoff"]["reason"] == "agent_unresolved" and "passed your case to a human agent" in out["final_answer"]
    assert [m.type for m in out["messages"]] == ["human", "ai"] and len(notifier.records) == 1 and out["plan"] == ["knowledge"]
    assert out["observe"]["escalate"] is True and out["verify_note"] == "escalated; added_support"


@pytest.mark.parametrize("claim", [
    "Your next payout is R$ 10 on Friday",
    "You have the Pro plan",
    "Your terminal is offline",
    "Seu saldo disponível é de R$ 200,00",
    "Sua conta bancária está vinculada ao Itaú",
    "Você tem uma taxa de 2,5% no seu plano",
    "Tu próximo pago es de $ 10",
    "Tienes el Pix habilitado en tu cuenta",
    "Su plan actual es Pro",
])
def test_the_account_lexicon_recognizes_claims_about_the_customers_own_data_in_three_languages(claim):
    from app.agents.observe import is_account_claim

    assert is_account_claim(claim)


@pytest.mark.parametrize("claim", [
    "Receivables advance lets you receive today the money of sales that would be paid later.",
    "Check your plan in the app",
    "Restart your terminal and wait",
    "Reinicie o terminal e aguarde",
    "Abra o menu > Suporte",
    "A fee applies to each advance.",
    "Getnet supports Pix by approximation.",
    "Pix payments are settled in seconds.",
    "Puedes configurar la cuenta desde el menú",
])
def test_the_account_lexicon_leaves_how_to_steps_and_general_facts_alone(claim):
    from app.agents.observe import is_account_claim

    assert not is_account_claim(claim)


# ---------- REPLAN_MAX=0: the graph it was ----------

def test_with_replan_max_zero_the_state_is_field_for_field_the_one_of_the_graph_without_the_observe_step(repo):
    def run(node, thread):
        g, _ = graph_for(node, repo, verify_model=Scripted(audits=[Verification()]))
        out = g.invoke({"messages": [HumanMessage(content="How does advance work?")], "user_id": "cliente1988"}, {"configurable": {"thread_id": thread}})
        out["messages"] = [(m.type, m.content) for m in out["messages"]]  # the ids are random
        return out

    sup, syn, kb, model = Support(), Synth(), KB([Doc("x")]), Scripted()
    with_it = run(make_observe_node(llm=model, retrieve_fn=kb, replan_max=0, support_fn=sup, synth_fn=syn), "a")
    without = run(None, "b")
    assert with_it == without  # every key, every value
    assert sup.states == [] and syn.states == [] and kb.queries == [] and model.calls == []


def test_the_early_return_of_replan_max_zero_comes_before_anything_the_add_support_action_added():
    """Pinned in the source: only the two assignments that feed the guard precede it, and none of the new names appears in it."""
    import ast
    from pathlib import Path

    import app.agents.observe as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    maker = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "make_observe_node")
    node = next(n for n in maker.body if isinstance(n, ast.FunctionDef) and n.name == "observe_node")
    guard = next(i for i, s in enumerate(node.body) if isinstance(s, ast.If))
    first_if = node.body[guard]
    assert isinstance(first_if.test, ast.BoolOp) and ast.unparse(first_if.test.values[0]) == "cap <= 0"
    assert [ast.unparse(s) for s in first_if.body] == ["return {}"]
    before = node.body[:guard]
    assert [type(s) for s in before] == [ast.Assign, ast.Assign] and [ast.unparse(s.targets[0]) for s in before] == ["cap", "original"]
    new_names = {"support_fn", "synth_fn", "_add_support", "can_add_support", "is_account_claim", "handoff_reason", "audit_claims", "get_llm"}
    seen = {n.id for s in [*before, first_if] for n in ast.walk(s) if isinstance(n, ast.Name)}
    assert not (seen & new_names)


def test_build_default_graph_hands_the_observe_step_the_graphs_own_support_and_synthesizer(monkeypatch, repo):
    import app.agents.observe as observe_module
    from app.graph import build_default_graph

    seen = {}
    real = observe_module.make_observe_node

    def spy(**kw):
        seen.update(kw)
        return real(**kw)

    monkeypatch.setattr(observe_module, "make_observe_node", spy)
    build_default_graph(repo=repo)
    assert callable(seen.get("support_fn")) and callable(seen.get("synth_fn")) and "replan_max" not in seen  # the cap still comes from REPLAN_MAX
