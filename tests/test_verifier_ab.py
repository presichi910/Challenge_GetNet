"""The verifier A/B harness: its pure parts. No network."""
from langchain_core.messages import AIMessage, HumanMessage

from eval.verifier_ab import claims, has_evidence, judged, parse_note, rebuild, render, snapshot, summarize

STATE = {"final_answer": "A fee applies.", "language": "English", "plan": ["knowledge"],
         "agent_outputs": {"knowledge": {"answer": "x", "contexts": ["[kb] A fee applies."], "tool_calls": []}},
         "messages": [HumanMessage(content="How much?", id="h1"), AIMessage(content="A fee applies.", id="a1")]}


def test_a_snapshot_rebuilds_the_state_the_verify_node_would_have_seen():
    snap = snapshot(STATE)
    assert has_evidence(snap) and snap["messages"][1] == {"type": "ai", "content": "A fee applies.", "id": "a1"}
    state = rebuild(snap)
    assert [(m.type, m.content, m.id) for m in state["messages"]] == [("human", "How much?", "h1"), ("ai", "A fee applies.", "a1")]
    assert state["final_answer"] == "A fee applies." and state["agent_outputs"]["knowledge"]["contexts"] == ["[kb] A fee applies."]
    assert not has_evidence(snapshot({**STATE, "agent_outputs": {}}))


def test_notes_and_judge_claims_are_counted():
    assert parse_note("revised:3 [a | b | c]") == 3 and parse_note("") == 0 and parse_note("translated:pt->en") == 0
    assert claims({"unsupported_claims": [{"claim": "a", "actionable": True}, {"claim": "b", "actionable": False}]}) == (2, 1)
    assert claims({}) == (0, 0)


def test_a_judge_error_is_retried_after_a_wait_and_is_never_returned_as_a_result():
    class Flaky:
        def __init__(self):
            self.calls = 0

        def score(self, *a, **k):
            self.calls += 1
            return {"verdict": "error"} if self.calls < 3 else {"verdict": "pass", "unsupported_claims": []}

    waits = []
    out = judged(Flaky(), {"message": "q", "criteria": ""}, "answer", snapshot(STATE), sleep=waits.append)
    assert out["verdict"] == "pass" and waits == [16, 45, 45]  # paced first, then a longer wait after each judge error


def run(mode, flagged, listed, omitted=0, error=None):
    return {"mode": mode, "final": "x", "note": "", "flagged": flagged, "listed": listed, "omitted": omitted, "error": error}


def j(verdict, unsupported, completeness=4):
    return {"verdict": verdict, "groundedness": 5 - min(unsupported, 3), "completeness": completeness,
            "unsupported_claims": [{"claim": "c", "actionable": i == 0} for i in range(unsupported)]}


def test_the_summary_counts_what_is_left_for_the_customer_and_never_averages_in_a_judge_error():
    rows = [{"id": "q05", "segments": 6, "runs": {"statements": [run("statements", 0, 1)], "segments": [run("segments", 5, 6, omitted=1)]},
             "judge": {"draft": j("fail", 5), "statements": j("fail", 5), "segments": j("pass", 0)}},
            {"id": "q08", "segments": 4, "runs": {"statements": [run("statements", 2, 2)], "segments": [run("segments", 2, 4)]},
             "judge": {"draft": j("fail", 2), "statements": j("pass", 0), "segments": j("error", 0)}}]
    s = summarize(rows)
    assert (s["draft"]["unsupported"], s["statements"]["unsupported"], s["segments"]["unsupported"]) == (7, 5, 0)
    assert s["segments"]["judged"] == 1 and s["draft"]["judged"] == 2  # the judge error is out of the segments column
    assert s["statements_checker"]["listed_mean"] == 1.5 and s["segments_checker"]["omitted_total"] == 1
    text = render(rows, s, "stamp", 1)
    assert "| q05 | 6 | 5 (1 actionable) | 5 (1 actionable) | 0 (0 actionable) | fail | pass |" in text and "judge error" in text
