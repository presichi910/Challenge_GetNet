"""The REPLAN_MAX 0-vs-2 harness: its pure parts. No network."""
from langchain_core.messages import AIMessage, HumanMessage

from eval.replan_ab import ESCALATED, apply_update, judge_jobs, render, run_arms, summarize, text_key
from eval.verifier_ab import snapshot

SNAP = snapshot({"final_answer": "Restart the terminal.", "language": "English", "plan": ["knowledge"],
                 "agent_outputs": {"knowledge": {"answer": "x", "contexts": ["[kb] old evidence"], "tool_calls": []}},
                 "messages": [HumanMessage(content="help?", id="h"), AIMessage(content="Restart the terminal.", id="a")]})


def test_a_text_is_identified_by_the_answer_and_by_the_evidence_the_judge_saw():
    assert text_key("a", ["e1"]) == text_key("a", ["e1"]) and text_key("a", ["e1"]) != text_key("a", ["e1", "e2"]) != text_key("b", ["e1"])


def test_arm_0_is_the_verify_node_alone_and_arm_2_runs_observe_first():
    def verify(state):
        return {"verify_note": "from verify", "final_answer": state["final_answer"] + " [v]"}

    def observe(state):
        return {"observe": {"claims": ["x"], "replans": ["retry_kb: 1 unsupported -> 2 passages -> 0 still unsupported"], "evidence": ["new passage"]},
                "final_answer": "Restart. [o]", "verify_note": "revised:1 [x]"}

    arms = run_arms(SNAP, observe, verify)
    assert arms["0"]["final"] == "Restart the terminal. [v]" and arms["0"]["evidence_extra"] == []
    assert arms["2"]["final"] == "Restart. [o] [v]" and arms["2"]["evidence_extra"] == ["new passage"]  # observe's text went through verify
    assert arms["2"]["replans"] == ["retry_kb: 1 unsupported -> 2 passages -> 0 still unsupported"] and not arms["2"]["escalated"]


def test_an_escalation_ends_arm_2_without_running_the_verify_node():
    def never(state):
        if state.get("observe"):  # arm 0 has no observe result: its verify node runs; arm 2's must not
            raise AssertionError("verify must not run after an escalation")
        return {"verify_note": ""}

    arms = run_arms(SNAP, lambda s: {"observe": {"claims": ["x"], "escalate": True, "replans": ["escalate: nothing"]}}, never)
    assert arms["2"]["escalated"] is True and arms["2"]["final"] == ESCALATED


def test_the_messages_of_a_node_update_are_merged_not_replaced():
    st = {"messages": [HumanMessage(content="q", id="h"), AIMessage(content="draft", id="a")], "final_answer": "draft"}
    merged = apply_update(st, {"final_answer": "new", "messages": [AIMessage(content="new", id="a")]})
    assert merged["final_answer"] == "new" and [m.content for m in merged["messages"]] == ["q", "new"]


def row(final0, final2, extra=(), escalated=False, replans=()):
    return {"id": "q05", "repeat": "new1", "draft": SNAP, "judged": {},
            "arms": {"0": {"final": final0, "note": "", "evidence_extra": [], "seconds": 4.0},
                     "2": {"final": final2, "note": "", "evidence_extra": list(extra), "escalated": escalated, "replans": list(replans), "seconds": 9.0}}}


def test_an_answer_the_observe_step_left_alone_is_judged_once_but_new_evidence_makes_it_a_new_text():
    assert len(judge_jobs(row("same", "same"))) == 1
    assert len(judge_jobs(row("same", "same", extra=["new passage"]))) == 2  # same words, now backed by more evidence: judged again
    assert len(judge_jobs(row("a", ESCALATED, escalated=True))) == 1  # an escalated case has no text to judge


def j(verdict, n, actionable, completeness=4):
    return {"verdict": verdict, "groundedness": 4, "completeness": completeness,
            "unsupported_claims": [{"claim": "c", "actionable": i < actionable} for i in range(n)]}


def test_the_three_figures_count_what_is_left_and_never_average_in_a_judge_error_or_an_escalation():
    r1, r2, r3 = row("a", "b", replans=["retry_kb: 2 unsupported -> 3 passages -> 0 still unsupported"]), row("c", "d"), row("e", "f", escalated=True, replans=["escalate: x"])
    for r in (r1, r2, r3):
        for arm, text in (("0", r["arms"]["0"]["final"]), ("2", r["arms"]["2"]["final"])):
            if r["arms"][arm].get("escalated"):
                continue
            ev = ["[kb] old evidence"] + r["arms"][arm]["evidence_extra"]
            r["judged"][text_key(text, ev)] = j("pass", 0, 0)
    r1["judged"][text_key("a", ["[kb] old evidence"])] = j("fail", 3, 2, completeness=3)
    r2["judged"][text_key("d", ["[kb] old evidence"])] = j("error", 0, 0)
    s = summarize([r1, r2, r3])
    assert (s["0"]["judged"], s["0"]["unsupported"], s["0"]["actionable"], s["0"]["pass"]) == (3, 3, 2, 2)
    assert (s["2"]["judged"], s["2"]["unsupported"], s["2"]["escalated"]) == (1, 0, 1)  # the judge error and the escalation are out
    assert s["observe"]["retried"] == 1 and s["observe"]["escalated"] == 1 and s["observe"]["text_changed"] == 3
    text = render([r1, r2, r3], s, "stamp")
    assert "escalated" in text and "judge error" in text and "REPLAN_MAX=2" in text


def test_a_row_whose_two_arms_ended_the_same_needs_no_score_when_only_the_differences_are_compared():
    from eval.replan_ab import differs

    same, changed, extra, escalated = row("t", "t"), row("a", "b"), row("t", "t", extra=["new passage"]), row("a", ESCALATED, escalated=True)
    assert [differs(r) for r in (same, changed, extra, escalated)] == [False, True, True, True]
    assert judge_jobs(same, differing_only=True) == {} and len(judge_jobs(same)) == 1
    assert len(judge_jobs(changed, differing_only=True)) == 2 and len(judge_jobs(escalated, differing_only=True)) == 1


def test_the_saved_measurement_has_twelve_texts_to_score_not_thirty_six(capsys):
    """The 30 saved drafts of 2026-11-15: 6 differ between the arms (12 texts); the other 24 are identical and cancel out."""
    import json
    from pathlib import Path

    f = Path("eval/reports/REPLAN_AB-20261115-151730.json")
    if not f.exists():
        import pytest

        pytest.skip("the saved measurement is not in this checkout")
    rows = json.loads(f.read_text(encoding="utf-8"))["rows"]
    assert sum(len(judge_jobs(r)) for r in rows) == 36 and sum(len(judge_jobs(r, True)) for r in rows) == 12
