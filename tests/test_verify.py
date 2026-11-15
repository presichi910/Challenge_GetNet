from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph.message import add_messages

from app.agents.verifier import Statement, Verification, make_verify_node, quote_in
from app.graph import build_graph

PT_ANSWER = "Você pode receber hoje o valor das vendas, mediante uma taxa. Sua próxima liquidação é em 18/11."
EN_ANSWER = "You can receive today the money of sales that would be paid later, for a fee."
EVIDENCE = "[kb] Receivables advance lets you receive today the money of sales that would be paid later. A fee applies."


class FakeModel:
    """Scripted LLM: `verification` is what the groundedness check returns, `replies` what plain calls return."""

    def __init__(self, verification=None, replies=(), fail=False):
        self.verification, self.replies, self.fail, self.calls = verification or Verification(), list(replies), fail, []

    def with_structured_output(self, schema, method=None):
        outer = self

        class _Checker:
            def invoke(self, messages, config=None):
                outer.calls.append("verify")
                if outer.fail:
                    raise RuntimeError("provider down")
                return outer.verification

        return _Checker()

    def invoke(self, messages, config=None):
        self.calls.append("write")
        if self.fail:
            raise RuntimeError("provider down")
        return AIMessage(content=self.replies.pop(0))


def _state(question, answer, contexts=(EVIDENCE,), **extra):
    messages = add_messages([], [HumanMessage(content=question), AIMessage(content=answer)])  # gives them ids
    outputs = {"knowledge": {"answer": answer, "contexts": list(contexts)}} if contexts else {}
    return {"messages": messages, "final_answer": answer, "agent_outputs": outputs, "language": "English", **extra}


def _apply(state, update):
    """What LangGraph does with the node's return value."""
    merged = {**state, **{k: v for k, v in update.items() if k != "messages"}}
    if "messages" in update:
        merged["messages"] = add_messages(state["messages"], update["messages"])
    return merged


def _supported(*quotes):
    return Verification(statements=[Statement(statement=f"claim {i}", evidence_quote=q) for i, q in enumerate(quotes)])


def test_a_statement_without_a_quote_makes_one_rewrite_that_replaces_the_answer_in_the_history():
    audit = Verification(statements=[
        Statement(statement="you can receive the money today", evidence_quote="receive today the money of sales"),
        Statement(statement="open Menu > Antecipar", evidence_quote=""),
    ])
    model = FakeModel(audit, replies=["You can receive today the money of sales, for a fee."])
    state = _state("How does receivables advance work?", EN_ANSWER + " Open Menu > Antecipar in the app.")
    update = make_verify_node(llm=model)(state)
    after = _apply(state, update)
    assert after["final_answer"] == "You can receive today the money of sales, for a fee."
    assert [m.content for m in after["messages"]][-1] == after["final_answer"] and len(after["messages"]) == 2
    assert update["verify_note"] == "revised:1 [open Menu > Antecipar]" and model.calls == ["verify", "write"]


def test_a_valid_quote_is_support_and_the_answer_is_left_alone_at_the_cost_of_one_call():
    model = FakeModel(_supported("receive today the money of sales that would be paid later", "A fee applies."))
    update = make_verify_node(llm=model)(_state("How does receivables advance work?", EN_ANSWER))
    assert "final_answer" not in update and "messages" not in update and update["verify_note"] == ""
    assert model.calls == ["verify"]


def test_an_answer_with_no_statements_to_check_is_left_alone():
    model = FakeModel()
    update = make_verify_node(llm=model)(_state("How does receivables advance work?", EN_ANSWER))
    assert update == {"verify_note": ""} and model.calls == ["verify"]


def test_a_quote_that_is_not_in_the_evidence_does_not_count_as_support():
    model = FakeModel(_supported("you can pay in 48 installments at every bank"), replies=["Rewritten."])
    update = make_verify_node(llm=model)(_state("How does receivables advance work?", EN_ANSWER))
    assert update["final_answer"] == "Rewritten." and update["verify_note"].startswith("revised:1")


def test_a_paraphrased_quote_does_not_count_as_support():
    model = FakeModel(_supported("the cash from future sales can be had immediately, a cost is charged"), replies=["Rewritten."])
    update = make_verify_node(llm=model)(_state("How does receivables advance work?", EN_ANSWER))
    assert update["verify_note"].startswith("revised:1")


def test_quote_in_ignores_case_punctuation_and_accents_and_tolerates_a_slightly_off_quote():
    ev = "O Antecipação de Recebíveis permite receber hoje, com taxa. Sua liquidação é em 18/11."
    assert quote_in("receber HOJE com taxa", ev) and quote_in("liquidacao e em 18 11", ev)
    long_quote = "o antecipação de recebíveis permite receber hoje, com uma pequena taxa"  # one word added: 4 of 5 triplets
    assert quote_in(long_quote, ev)
    assert not quote_in("", ev) and not quote_in("permite receber ontem sem taxa nenhuma", ev)


def test_a_quote_of_fewer_than_three_words_must_be_literal():
    ev = "the fee is 3.5% per advance"
    assert quote_in("3 5", ev) and quote_in("fee is", ev)
    assert not quote_in("fee 3", ev)  # not contiguous in the evidence, and too short to be judged by overlap
    assert not quote_in("fees", ev)


def test_without_evidence_there_is_nothing_to_check_and_no_call_is_made():
    model = FakeModel()
    update = make_verify_node(llm=model)(_state("Hi there!", "Hello! How can I help you today?", contexts=()))
    assert update == {"verify_note": ""} and model.calls == []


def test_an_answer_in_another_language_than_the_customers_is_translated():
    model = FakeModel(replies=["You can receive today the money of your sales, for a fee. Your next payout is on 11/18."])
    state = _state("When is my next payout?", PT_ANSWER, contexts=())
    update = make_verify_node(llm=model)(state)
    assert update["final_answer"].startswith("You can receive") and update["verify_note"] == "translated:pt->en"
    assert model.calls == ["write"]


def test_a_portuguese_customer_is_not_translated_to_english():
    model = FakeModel()
    update = make_verify_node(llm=model)(_state("Quando vou receber o dinheiro das vendas de ontem?", PT_ANSWER, contexts=()))
    assert update == {"verify_note": ""} and model.calls == []


def test_when_the_customers_language_is_unclear_nothing_is_translated():
    model = FakeModel()
    update = make_verify_node(llm=model)(_state("Pix", PT_ANSWER, contexts=()))
    assert update == {"verify_note": ""} and model.calls == []


def test_a_provider_error_leaves_the_answer_as_it_was():
    state = _state("How does receivables advance work?", EN_ANSWER)
    update = make_verify_node(llm=FakeModel(fail=True))(state)
    assert update == {"verify_note": ""}


def test_the_fallback_message_and_synth_errors_are_not_touched():
    from app.prompts import FALLBACK_MESSAGE
    model = FakeModel()
    node = make_verify_node(llm=model)
    assert node(_state("q", FALLBACK_MESSAGE)) == {"verify_note": ""}
    assert node(_state("q", EN_ANSWER, synth_error="X")) == {"verify_note": ""} and model.calls == []


def test_the_graph_runs_synthesizer_then_verify_and_the_user_sees_the_verified_answer():
    unsupported = Verification(statements=[Statement(statement="48x at every bank", evidence_quote="")])
    model = FakeModel(unsupported, replies=["Up to 48 installments, depending on the card issuer."])

    def router(state):
        return {"plan": ["knowledge"], "step": 0, "language": "English", "agent_outputs": {}, "final_answer": "",
                "wants_human": False, "handoff": None, "synth_error": ""}

    def knowledge(state):
        out = {"knowledge": {"answer": "Up to 48 installments at every bank.", "contexts": ["[kb] up to 48x, depends on issuer"]}}
        return {"agent_outputs": out, "step": state["step"] + 1}

    def synthesizer(state):
        answer = state["agent_outputs"]["knowledge"]["answer"]
        return {"final_answer": answer, "messages": [AIMessage(content=answer)], "synth_error": ""}

    g = build_graph(router, knowledge, lambda s: {}, synthesizer, lambda s: {}, verify=make_verify_node(llm=model))
    cfg = {"configurable": {"thread_id": "u:c"}}
    out = g.invoke({"messages": [HumanMessage(content="How many installments can I use with the crediário?")]}, cfg)
    assert out["final_answer"] == "Up to 48 installments, depending on the card issuer."
    assert out["messages"][-1].content == out["final_answer"] and len(out["messages"]) == 2
    assert out["verify_note"] == "revised:1 [48x at every bank]"


def test_without_a_verify_node_the_graph_is_what_it_was():
    def router(state):
        return {"plan": [], "step": 0, "wants_human": False, "handoff": None, "final_answer": ""}

    def synthesizer(state):
        return {"final_answer": "Hello!", "messages": [AIMessage(content="Hello!")]}

    g = build_graph(router, lambda s: {}, lambda s: {}, synthesizer, lambda s: {})
    out = g.invoke({"messages": [HumanMessage(content="Hi there!")]}, {"configurable": {"thread_id": "u:c"}})
    assert out["final_answer"] == "Hello!" and "verify_note" not in out


# ---- verify_raw: the checker's raw output, for the eval rows only ----

def test_verify_raw_is_off_by_default_so_production_state_and_the_api_never_carry_it():
    import inspect

    from app.graph import build_default_graph

    model = FakeModel(Verification(statements=[Statement(statement="open Menu", evidence_quote="")]), replies=["Rewritten."])
    update = make_verify_node(llm=model)(_state("How does receivables advance work?", EN_ANSWER + " Open Menu."))
    assert "verify_raw" not in update and update["verify_note"].startswith("revised:1")
    assert "verify_raw" not in make_verify_node(llm=FakeModel())(_state("q", "", contexts=()))
    assert inspect.signature(build_default_graph).parameters["keep_verify_raw"].default is False


def test_verify_raw_keeps_every_statement_with_its_quote_and_whether_the_code_found_it():
    import json

    audit = Verification(statements=[Statement(statement="you receive the money today", evidence_quote="receive today the money of sales"),
                                     Statement(statement="open Menu > Antecipar", evidence_quote="Menu > Antecipar")])
    update = make_verify_node(llm=FakeModel(audit, replies=["Rewritten."]), keep_raw=True)(
        _state("How does receivables advance work?", EN_ANSWER + " Open Menu > Antecipar."))
    raw = json.loads(update["verify_raw"])
    assert [(s["statement"], s["quote_found"]) for s in raw["statements"]] == [
        ("you receive the money today", True), ("open Menu > Antecipar", False)]  # the second quote is not in the evidence
    assert raw["statements"][1]["evidence_quote"] == "Menu > Antecipar"


def test_verify_raw_is_always_set_when_asked_even_with_nothing_to_check_or_a_provider_error():
    import json

    node = make_verify_node(llm=FakeModel(), keep_raw=True)
    assert node(_state("q", "", contexts=()))["verify_raw"] == ""  # no answer: nothing to audit
    assert node(_state("Hi there!", "Hello! How can I help you today?", contexts=()))["verify_raw"] == ""  # no evidence: no audit
    failed = make_verify_node(llm=FakeModel(fail=True), keep_raw=True)(_state("How does receivables advance work?", EN_ANSWER))
    assert json.loads(failed["verify_raw"]) == {"error": "RuntimeError"} and failed["verify_note"] == ""


# ---- the rewrite leaves nothing pointing at deleted content ----

class RecordingModel(FakeModel):
    def invoke(self, messages, config=None):
        self.written_with = [m.content for m in messages]
        return super().invoke(messages, config)


def test_the_rewrite_prompt_forbids_dangling_references_and_says_what_to_do_when_nothing_is_left():
    from app.agents.verifier import REVISE_PROMPT

    model = RecordingModel(Verification(statements=[Statement(statement="restart the terminal", evidence_quote="")]),
                           replies=["There are no documented steps for this. I can help further."])
    node = make_verify_node(llm=model)
    update = node(_state("My terminal is offline, what should I do?", "Restart the terminal. After these steps, call us."))
    system = model.written_with[0]
    assert "- restart the terminal" in system and "English" in system  # the claims to delete and the language
    assert "after these actions" in system and "refers to missing content" in system  # no dangling references
    assert "no documented steps" in system and "offer to help further" in system  # nothing actionable left
    assert "facts the evidence supports" in system and "must stay" in system  # the supported facts are not dropped with the rest
    assert "Say nothing new" in system and update["final_answer"].startswith("There are no documented steps")
    assert REVISE_PROMPT.count("{") == 2 and REVISE_PROMPT.count("}") == 2  # only {language} and {claims}


def test_the_support_prompt_offers_a_ticket_instead_of_creating_it_and_still_formats():
    from app.prompts import SUPPORT_PROMPT

    text = SUPPORT_PROMPT.format(user_id="u1", language="English", prior_context="")  # no stray braces
    assert "move closer to the router" not in text  # the example the model used to copy
    assert "offer to open a support ticket" in text and "only after the customer asks for it or accepts your offer" in text
    assert "TKT" not in text and "existing ticket" not in text  # duplicates are solved in code, not in the prompt
