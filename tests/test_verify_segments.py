"""The segment verifier (VERIFIER_MODE=segments): the code splits the answer and the model must answer for every segment.
Scripted model, no network."""
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph.message import add_messages

from app.agents.verifier import (SegmentAudit, SegmentedVerification, audit_segments, make_verify_node)
from app.segments import MAX_SEGMENTS, may_skip_evidence, split_segments

EVIDENCE = ('[kb] Receivables advance lets you receive today the money of sales that would be paid later. A fee applies. '
            '[tool] {"status":"offline","signal_strength":"weak","last_error":"NET_TIMEOUT: could not reach the acquirer gateway"}')


class SegModel:
    """Scripted: `audit` is what the checker returns, `replies` what plain calls return."""

    def __init__(self, audit=None, replies=(), fail=False):
        self.audit, self.replies, self.fail, self.calls, self.bodies = audit, list(replies), fail, [], []

    def with_structured_output(self, schema, method=None):
        outer = self

        class _Checker:
            def invoke(self, messages, config=None):
                outer.calls.append(schema.__name__)
                outer.bodies.append(messages[-1].content)
                if outer.fail:
                    raise RuntimeError("provider down")
                return outer.audit

        return _Checker()

    def invoke(self, messages, config=None):
        self.calls.append("write")
        self.bodies.append(messages[0].content)
        return AIMessage(content=self.replies.pop(0))


def state(question, answer, contexts=(EVIDENCE,)):
    messages = add_messages([], [HumanMessage(content=question), AIMessage(content=answer)])
    outputs = {"knowledge": {"answer": answer, "contexts": list(contexts)}} if contexts else {}
    return {"messages": messages, "final_answer": answer, "agent_outputs": outputs, "language": "English"}


def entries(*pairs):
    """pairs of (id, is_claim, quote)"""
    return SegmentedVerification(segments=[SegmentAudit(id=i, is_claim=c, evidence_quote=q) for i, c, q in pairs])


ANSWER = ("Receivables advance lets you receive today the money of sales that would be paid later.\n"
          "A fee applies.\n"
          "Open Menu > Antecipar and pick the sale.\n"
          "Let me know if you need anything else!")


# ---------- the split ----------

def test_the_answer_is_split_into_sentences_list_items_and_table_rows_without_headings():
    text = ("**What to try right now**\n\n1. **Verify the Wi-Fi** – Make sure the router is on. Check the cable (e.g. the blue one).\n"
            "- Restart the terminal: power it off for ~10 seconds.\n\n| Date | Amount |\n|---|---|\n| 2026-11-14 | R$ 129.90 |\n"
            "It costs R$ 1.234,56 per month. Thanks!")
    assert split_segments(text) == [
        "Verify the Wi-Fi – Make sure the router is on.", "Check the cable (e.g. the blue one).",
        "Restart the terminal: power it off for ~10 seconds.", "2026-11-14 | R$ 129.90",
        "It costs R$ 1.234,56 per month.", "Thanks!"]


def test_a_portuguese_answer_keeps_its_accents_and_short_answers_like_yes_are_still_audited():
    assert split_segments("Olá! Sua venda foi recusada. Código 51.") == ["Olá!", "Sua venda foi recusada.", "Código 51."]
    assert split_segments("Yes. Getnet offers it.") == ["Yes.", "Getnet offers it."]  # a one-word answer to a yes/no question is a claim
    assert split_segments("**\n---\n  \n") == []  # no words, no segment


def test_an_answer_with_too_many_segments_is_not_split_so_the_node_falls_back():
    assert split_segments("\n".join(f"Fact number {i} is here." for i in range(MAX_SEGMENTS + 1))) is None


# ---------- what may skip the evidence: only a closing the code recognises ----------

@pytest.mark.parametrize("closing", ["Let me know if you need anything else!", "I can open a support ticket if you want.",
                                     "Hello! Happy to help.", "Se precisar, me avise.", "Olá! Posso ajudar com algo mais?",
                                     "Gracias, avísame si necesitas algo."])
def test_greetings_thanks_and_offers_may_skip_the_evidence(closing):
    assert may_skip_evidence(closing)


@pytest.mark.parametrize("claim", ["The terminal is offline because the signal is weak.",  # a fact, not a closing
                                   "Restart the terminal.", "Please restart the terminal if you need help.",  # an action verb
                                   "Let me know: your next payout is in 3 days.",  # a figure
                                   "Thanks, the fee is 2.5%.", "Obrigado, o valor é R$ 10.",
                                   "Reinicie o terminal e me avise.", "Feel free to " + "x " * 30])
def test_a_fact_a_step_a_figure_or_a_long_sentence_never_skips_the_evidence(claim):
    assert not may_skip_evidence(claim)


# ---------- the node ----------

def test_a_segment_the_model_does_not_answer_is_unsupported_so_nothing_can_be_omitted():
    audit = entries((1, True, "receive today the money of sales that would be paid later"))  # answers only #1 of 4
    model = SegModel(audit, replies=["Receivables advance lets you receive the money today."])
    update = make_verify_node(llm=model, mode="segments", keep_raw=True)(state("How does advance work?", ANSWER))
    raw = json.loads(update["verify_raw"])
    assert raw["omitted"] == [2, 3, 4] and raw["mode"] == "segments"
    assert update["verify_note"].startswith("revised:3 [A fee applies. | Open Menu > Antecipar and pick the sale. | Let me know")
    assert model.calls == ["SegmentedVerification", "write"]
    assert "A fee applies." in model.bodies[1] and "Open Menu" in model.bodies[1]  # the rewrite is told which segments to delete
    assert "[3] Open Menu > Antecipar and pick the sale." in model.bodies[0]  # the model sees the numbered segments


def test_valid_quotes_and_a_real_closing_leave_the_answer_alone():
    audit = entries((1, True, "receive today the money of sales that would be paid later"), (2, True, "A fee applies."),
                    (3, True, "Receivables advance lets you receive today"), (4, False, ""))
    model = SegModel(audit)
    update = make_verify_node(llm=model, mode="segments", keep_raw=True)(state("How does advance work?", ANSWER))
    assert update["verify_note"] == "" and "final_answer" not in update and model.calls == ["SegmentedVerification"]
    rows = json.loads(update["verify_raw"])["segments"]
    assert [r["quote_found"] for r in rows] == [True, True, True, False] and rows[3]["label_honored"] is True


def test_the_model_cannot_label_a_step_or_a_fact_as_not_a_claim_to_escape_the_check():
    audit = entries((1, True, "receive today the money of sales"), (2, False, ""), (3, False, ""), (4, False, ""))
    answer = "Receivables advance lets you receive today the money of sales.\nOpen Menu > Antecipar.\nYour fee is 2.5% per sale.\nThanks!"
    model = SegModel(audit, replies=["Receivables advance lets you receive today the money of sales. Thanks!"])
    update = make_verify_node(llm=model, mode="segments", keep_raw=True)(state("How does advance work?", answer))
    assert update["verify_note"].startswith("revised:2 [Open Menu > Antecipar. | Your fee is 2.5% per sale.]")
    rows = json.loads(update["verify_raw"])["segments"]
    assert [r["label_honored"] for r in rows] == [False, False, False, True]  # only "Thanks!" is believed


def test_a_quote_that_is_not_in_the_evidence_or_is_a_paraphrase_does_not_count():
    audit = entries((1, True, "the cash from future sales can be had immediately"), (2, True, "a fee of 5% applies"),
                    (3, True, "Open Menu > Antecipar"), (4, False, ""))
    model = SegModel(audit, replies=["Rewritten."])
    update = make_verify_node(llm=model, mode="segments")(state("How does advance work?", ANSWER))
    assert update["verify_note"].startswith("revised:3") and update["final_answer"] == "Rewritten."


def test_a_quote_of_what_the_customer_said_backs_a_segment_that_repeats_it():
    answer = "You said your terminal shows error E42.\nA fee applies."
    audit = entries((1, True, "my terminal shows error E42"), (2, True, "A fee applies."))
    update = make_verify_node(llm=SegModel(audit), mode="segments")(state("Hi, my terminal shows error E42, what now?", answer))
    assert update["verify_note"] == ""


def test_the_quote_is_checked_against_the_evidence_not_against_the_answer_itself():
    audit = entries((1, True, "Open Menu > Antecipar and pick the sale."), (2, True, "A fee applies."), (3, True, "x"), (4, False, ""))
    model = SegModel(audit, replies=["Rewritten."])
    update = make_verify_node(llm=model, mode="segments")(state("How does advance work?", ANSWER))
    assert "Open Menu > Antecipar" in update["verify_note"]  # quoting the answer back at us is not support


def test_a_provider_error_leaves_the_answer_as_it_was_and_says_so_in_the_raw_row():
    update = make_verify_node(llm=SegModel(fail=True), mode="segments", keep_raw=True)(state("How does advance work?", ANSWER))
    assert update["verify_note"] == "" and json.loads(update["verify_raw"]) == {"error": "RuntimeError"} and "final_answer" not in update


def test_with_no_evidence_nothing_is_audited_in_either_mode():
    model = SegModel()
    update = make_verify_node(llm=model, mode="segments")(state("Hi there!", "Hello! How can I help you today?", contexts=()))
    assert update == {"verify_note": ""} and model.calls == []


def test_an_answer_too_long_to_split_falls_back_to_the_statement_check():
    from app.agents.verifier import Statement, Verification

    class Both(SegModel):
        def with_structured_output(self, schema, method=None):
            self.calls.append(schema.__name__)
            return type("C", (), {"invoke": lambda _s, m, config=None: Verification(statements=[Statement(statement="x", evidence_quote="A fee applies.")])})()

    long_answer = "\n".join(f"Fact number {i} is here." for i in range(MAX_SEGMENTS + 1))
    model = Both()
    make_verify_node(llm=model, mode="segments")(state("q?", long_answer))
    assert model.calls == ["Verification"]


def test_the_mode_comes_from_the_setting_and_the_default_is_still_the_statement_check(monkeypatch):
    from app.config import get_settings

    assert get_settings().verifier_mode == "statements"
    model = SegModel(entries((1, True, "x")), replies=["Rewritten."])
    monkeypatch.setenv("VERIFIER_MODE", "segments")
    get_settings.cache_clear()
    try:
        make_verify_node(llm=model)(state("How does advance work?", ANSWER))
    finally:
        get_settings.cache_clear()
    assert model.calls[0] == "SegmentedVerification"


def test_audit_segments_reports_which_segments_the_code_rejected():
    unsupported, table = audit_segments(["A fee applies.", "Press 60 then 2."], entries((1, True, "A fee applies."), (2, True, "")), EVIDENCE, "")
    assert unsupported == ["Press 60 then 2."] and table["omitted"] == []
