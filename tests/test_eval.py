"""Tests for the evaluator."""
import pytest
import json

from eval.judge import Judge, parse_judgement
from eval.run_eval import load_cases, pct, routing_ok, summarize, tools_ok


ORIGINAL_17 = [f"q{i:02d}" for i in range(1, 11)] + [f"x0{i}" for i in range(1, 5)] + [f"h0{i}" for i in range(1, 4)]
HOLDOUT_IDS = [f"ho{i:02d}" for i in range(1, 14)]  # ho01-ho05: seen during development; ho06-ho13: blind, written before the prompt fixes


def test_dataset_covers_all_challenge_scenarios():
    """Invariants, not totals: regular cases are added in later phases and must not break this."""
    cases = load_cases(None)
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids))
    for prefix in ORIGINAL_17:  # the 17 cases of the original dataset (h01-h03 included) are still there
        assert sum(1 for i in ids if i.startswith(prefix + "_")) == 1, prefix
    assert all({"id", "user_id", "message", "expected_agents"} <= set(c) for c in cases)


def test_the_holdout_is_the_frozen_set_and_the_blind_part_says_what_correct_looks_like():
    cases = load_cases(None)
    held = {c["id"][:4]: c for c in cases if c.get("holdout")}
    assert sorted(held) == HOLDOUT_IDS  # no regular case is marked holdout and no holdout case is lost or added
    new = [held[i] for i in HOLDOUT_IDS[5:]]
    assert all(c["holdout"] is True and c.get("criteria") for c in new)
    assert {c["user_id"] for c in new} <= {"cliente1988", "cliente2024", "cliente3030"}  # real customers, no ghost user
    assert [i for i in HOLDOUT_IDS[5:] if held[i].get("history")] == ["ho09", "ho10"]  # the multi-turn ones
    assert [i for i in HOLDOUT_IDS[5:] if held[i].get("expect_handoff")] == ["ho11"]  # the one that asks for a person
    assert all(held[i]["expect_handoff"] is False for i in ("ho07", "ho08", "ho09", "ho10", "ho12", "ho13"))
    assert held["ho07"]["expected_tools"] == held["ho08"]["expected_tools"] == []  # refusals need no data tool


def test_routing_ok_allows_optional_but_not_missing_or_extra():
    case = {"expected_agents": ["support"], "optional_agents": ["knowledge"]}
    assert routing_ok(case, ["support"]) and routing_ok(case, ["support", "knowledge"])
    assert not routing_ok(case, ["knowledge"])
    assert not routing_ok({"expected_agents": [], "optional_agents": []}, ["knowledge"])


def test_tools_ok_is_subset_check():
    assert tools_ok({"expected_tools": ["a"]}, ["a", "b"]) and not tools_ok({"expected_tools": ["a"]}, ["b"])


def test_parse_judgement_handles_wrapped_json_and_clamps():
    j = parse_judgement('Sure! {"relevance": 9, "groundedness": 4, "completeness": 3, "tone": 5, "verdict": "pass", "reason": "ok"} done')
    assert j["relevance"] == 5 and j["verdict"] == "pass"
    with pytest.raises(ValueError):
        parse_judgement("no json")


def test_summarize_and_percentile():
    rows = [
        {"routing_ok": True, "tools_ok": True, "latency_ms": 100, "judge": {"relevance": 5, "groundedness": 4, "completeness": 4, "tone": 5, "verdict": "pass"}},
        {"routing_ok": False, "tools_ok": True, "latency_ms": 300, "degraded": True, "judge": {"relevance": 3, "groundedness": 2, "completeness": 3, "tone": 4, "verdict": "fail"}},
    ]
    s = summarize(rows)
    assert s["routing_accuracy"] == 0.5 and s["tool_use_accuracy"] == 1 and s["degraded_rate"] == 0.5
    assert s["avg_relevance"] == 4 and s["judge_pass_rate"] == 0.5
    assert pct([1, 2, 3, 4, 5], 0.5) == 3


def test_handoff_ok_and_summary_metrics():
    from eval.run_eval import handoff_ok

    assert handoff_ok({"expect_handoff": True}, "user_requested") is True
    assert handoff_ok({"expect_handoff": True}, None) is False     # missed
    assert handoff_ok({"expect_handoff": False}, "agent_unresolved") is False  # false alarm
    assert handoff_ok({}, "agent_unresolved") is None              # no expectation
    rows = [{"routing_ok": True, "tools_ok": True, "latency_ms": 1, "handoff": "user_requested", "handoff_ok": True},
            {"routing_ok": True, "tools_ok": True, "latency_ms": 1, "handoff": None, "handoff_ok": False},
            {"routing_ok": True, "tools_ok": True, "latency_ms": 1, "handoff": "x", "handoff_ok": None}]
    s = summarize(rows)
    assert s["handoff_accuracy"] == 0.5 and round(s["handoff_rate"], 2) == 0.67


# ---------------- provider errors are not verdicts ----------------

class RateLimitError(Exception):  # same class name as the Groq SDK one, which is what we match on
    pass


QUOTA = "RateLimitError: Error code: 429 - Rate limit reached ... tokens per day (TPD): Limit 200000, Used 199483"
CASE = {"id": "c", "user_id": "u", "message": "hi", "expected_agents": ["support"]}


def good_state(**extra):
    out = {"plan": ["support"], "final_answer": "a", "router_error": "", "synth_error": "",
           "agent_outputs": {"support": {"answer": "a", "error": None, "tool_calls": [], "contexts": []}}}
    out.update(extra)
    return out


class FakeService:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)

    def run_raw(self, message, user_id, thread_id=None):
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome, 5


class CountingJudge:
    def __init__(self):
        self.calls = 0

    def score(self, *a, **k):
        self.calls += 1
        return {"relevance": 5, "groundedness": 5, "completeness": 5, "tone": 5, "verdict": "pass", "reason": ""}


@pytest.mark.parametrize("text, expected", [
    (QUOTA, True),
    ("APIConnectionError: Connection error.", True),
    ("InternalServerError: Error code: 503 - overloaded", True),
    ("APITimeoutError: Request timed out.", True),
    ("BadRequestError: Error code: 400 - tool_use_failed", False),  # the model's fault, counts
    ("empty_answer", False),
    ("ValueError: no JSON in judge output", False),
    ("", False),
    (None, False),
])
def test_is_provider_error(text, expected):
    from eval.run_eval import is_provider_error

    assert is_provider_error(text) is expected


@pytest.mark.parametrize("state, where", [
    (good_state(router_error=QUOTA, plan=["knowledge"]), "router"),  # quota -> router fell back to knowledge
    (good_state(agent_outputs={"support": {"answer": "", "error": QUOTA, "tool_calls": [], "contexts": []}}), "support"),
    (good_state(synth_error=QUOTA), "synthesizer"),
])
def test_a_provider_error_in_any_node_marks_the_case_as_error_and_skips_the_judge(state, where):
    from eval.run_eval import evaluate

    judge = CountingJudge()
    rows = evaluate([CASE], FakeService(state), judge, pause=0)
    assert rows[0]["provider_error"].startswith(where) and "routing_ok" not in rows[0]
    assert judge.calls == 0  # no quota spent judging something that did not really run


def test_an_exception_from_the_provider_while_running_is_an_error_but_a_bug_is_a_failure():
    from eval.run_eval import evaluate

    rows = evaluate([CASE, CASE], FakeService(RateLimitError("Error code: 429"), ValueError("bug")), None, pause=0)
    assert rows[0].get("provider_error") and rows[0]["provider_error"].startswith("run: RateLimitError")
    assert not rows[1].get("provider_error") and rows[1]["routing_ok"] is False  # real failure, still counted


def test_provider_errors_do_not_count_in_any_metric():
    from eval.run_eval import evaluate

    judge = CountingJudge()
    states = [good_state(), good_state(router_error=QUOTA), good_state()]
    rows = evaluate([CASE] * 3, FakeService(*states), judge, pause=0)
    s = summarize(rows)
    assert s["cases"] == 2 and s["provider_errors"] == 1
    assert s["routing_accuracy"] == 1.0 and s["tool_use_accuracy"] == 1.0 and s["judge_pass_rate"] == 1.0
    assert s["degraded_rate"] == 0 and judge.calls == 2


def test_a_run_made_only_of_provider_errors_reports_no_metrics():
    s = summarize([{"id": "a", "provider_error": "router: x", "latency_ms": 0},
                   {"id": "b", "provider_error": "run: y", "latency_ms": 0}])
    assert s == {"cases": 0, "provider_errors": 2}  # not "0% routing accuracy"


def test_judge_errors_are_counted_apart_and_not_averaged():
    bad = {"relevance": 0, "groundedness": 0, "completeness": 0, "tone": 0, "verdict": "error", "reason": "x"}
    ok = {"relevance": 5, "groundedness": 5, "completeness": 5, "tone": 5, "verdict": "pass", "reason": ""}
    s = summarize([{"routing_ok": True, "tools_ok": True, "latency_ms": 1, "judge": bad},
                   {"routing_ok": True, "tools_ok": True, "latency_ms": 1, "judge": ok}])
    assert s["judge_errors"] == 1 and s["avg_relevance"] == 5 and s["judge_pass_rate"] == 1.0


def test_report_lists_errors_and_an_incomplete_run_keeps_latest_md(tmp_path, monkeypatch):
    import eval.run_eval as run_eval

    monkeypatch.setattr(run_eval, "ROOT", tmp_path)
    (tmp_path / "reports").mkdir()
    latest = tmp_path / "reports" / "latest.md"
    latest.write_text("PREVIOUS FULL REPORT", encoding="utf-8")

    ok_row = {"id": "a", "message": "m", "routing_ok": True, "tools_ok": True, "latency_ms": 5, "plan": []}
    err_row = {"id": "b", "message": "m", "provider_error": "router: RateLimitError: 429", "latency_ms": 0}

    summary = summarize([ok_row, err_row])
    text = run_eval.render_report([ok_row, err_row], summary, "stamp")
    assert "| b | - |" in text and "| error |" in text and "Provider errors" in text
    assert "excluded from every metric" in text

    run_eval.write_report([ok_row, err_row], summary)
    assert latest.read_text(encoding="utf-8") == "PREVIOUS FULL REPORT"  # not overwritten
    assert len(list((tmp_path / "reports").glob("*.md"))) == 2  # but the dated report was written

    run_eval.write_report([ok_row], summarize([ok_row]))
    assert "PREVIOUS" not in latest.read_text(encoding="utf-8")  # a clean run does update it


# ---------------- per-minute limits are waited out, daily quotas are not ----------------

PER_MINUTE = ("RateLimitError: Error code: 429 - Rate limit reached for model `m` on tokens per minute (TPM): "
              "Limit 8000, Used 6651, Requested 1592. Please try again in 1.8225s.")


@pytest.mark.parametrize("hint, expected", [
    ("Please try again in 1.8225s.", 5.0),        # short hint -> floor
    ("Please try again in 30s", 32.0),            # hint + margin
    ("Please try again in 1m5.2s", 67.2),
    ("Please try again in 450ms", 5.0),           # milliseconds are not minutes
    ("Please try again in 7m12.5s", 70.0),        # capped
    ("no hint at all", 5.0),
])
def test_retry_wait(hint, expected):
    from eval.run_eval import retry_wait

    assert retry_wait(hint) == pytest.approx(expected)


def test_daily_limit_detection():
    from eval.run_eval import is_daily_limit

    assert is_daily_limit(QUOTA) and not is_daily_limit(PER_MINUTE)


def test_a_per_minute_limit_is_waited_out_and_the_case_counts_normally():
    from eval.run_eval import evaluate

    waits = []
    svc = FakeService(good_state(router_error=PER_MINUTE), good_state())
    rows = evaluate([CASE], svc, CountingJudge(), pause=0, retries=2, sleep=waits.append)
    assert waits == [5.0]  # waited once, then it worked
    assert not rows[0].get("provider_error") and rows[0]["routing_ok"] is True and svc.outcomes == []


def test_a_per_minute_limit_that_never_clears_ends_as_an_error_not_a_failure():
    from eval.run_eval import evaluate

    waits = []
    svc = FakeService(*[good_state(router_error=PER_MINUTE) for _ in range(3)])
    rows = evaluate([CASE], svc, None, pause=0, retries=2, sleep=waits.append)
    assert len(waits) == 2 and rows[0]["provider_error"].startswith("router:")
    assert summarize(rows) == {"cases": 0, "provider_errors": 1}


def test_a_daily_quota_is_not_retried():
    from eval.run_eval import evaluate

    waits = []
    svc = FakeService(good_state(router_error=QUOTA), good_state())
    rows = evaluate([CASE], svc, None, pause=0, retries=3, sleep=waits.append)
    assert waits == [] and rows[0].get("provider_error") and len(svc.outcomes) == 1  # second outcome untouched


def test_a_non_provider_exception_is_not_retried():
    from eval.run_eval import evaluate

    waits = []
    svc = FakeService(ValueError("bug"), good_state())
    rows = evaluate([CASE], svc, None, pause=0, retries=3, sleep=waits.append)
    assert waits == [] and rows[0]["routing_ok"] is False


def test_the_report_says_which_models_ran_and_a_partial_run_keeps_latest_md(tmp_path, monkeypatch):
    import eval.run_eval as run_eval

    monkeypatch.setattr(run_eval, "ROOT", tmp_path)
    (tmp_path / "reports").mkdir()
    latest = tmp_path / "reports" / "latest.md"
    latest.write_text("FULL REPORT", encoding="utf-8")
    row = {"id": "a", "message": "m", "routing_ok": True, "tools_ok": True, "latency_ms": 5, "plan": []}
    models = {"knowledge": "k-model", "router / support / synthesizer": "l-model", "judge": "j-model"}

    path = run_eval.write_report([row], summarize([row]), models, update_latest=False)  # e.g. --only
    assert latest.read_text(encoding="utf-8") == "FULL REPORT"
    text = path.read_text(encoding="utf-8")
    assert "knowledge = `k-model`" in text and "router / support / synthesizer = `l-model`" in text and "judge = `j-model`" in text

    run_eval.write_report([row], summarize([row]), models)  # a full clean run updates it
    assert "k-model" in latest.read_text(encoding="utf-8")


# the real Groq message is long: "tokens per day" sits after the 160 characters the report shows
REAL_DAILY = ("RateLimitError: Error code: 429 - {'error': {'message': 'Rate limit reached for model "
              "`openai/gpt-oss-20b` in organization `org_01abcdefghijklmnopqrstuvwx` service tier `on_demand` on "
              "tokens per day (TPD): Limit 200000, Used 199976, Requested 1232. Please try again in 8m41.856s. "
              "Need more tokens? Upgrade to Dev Tier today at https://console.groq.com/settings/billing', "
              "'type': 'tokens', 'code': 'rate_limit_exceeded'}}")


def test_a_real_daily_quota_message_is_not_retried_even_though_the_report_cuts_it():
    from eval.run_eval import evaluate, provider_error_in

    state = good_state(router_error=REAL_DAILY, plan=["knowledge"])
    assert "tokens per day" not in provider_error_in(state)  # the display text is cut before it
    waits = []
    svc = FakeService(state, good_state())
    rows = evaluate([CASE], svc, None, pause=0, retries=3, sleep=waits.append)
    assert waits == [] and rows[0].get("provider_error") and len(svc.outcomes) == 1


def test_a_real_per_minute_message_is_still_retried():
    from eval.run_eval import evaluate

    minute = REAL_DAILY.replace("tokens per day (TPD)", "tokens per minute (TPM)").replace("8m41.856s", "9.5s")
    waits = []
    rows = evaluate([CASE], FakeService(good_state(router_error=minute), good_state()), None, pause=0,
                    retries=2, sleep=waits.append)
    assert waits == [pytest.approx(11.5)] and not rows[0].get("provider_error")


def test_rows_keep_the_exact_tool_calls_with_their_arguments():
    from eval.run_eval import evaluate

    state = good_state(agent_outputs={
        "support": {"answer": "a", "error": None, "contexts": [], "tool_calls": [
            {"name": "get_terminal_status", "args": {}},
            {"name": "get_recent_transactions", "args": {"limit": 5, "status": "declined"}}]},
        "knowledge": {"answer": "b", "error": None, "contexts": [], "tool_calls": [
            {"name": "search_getnet_kb", "args": {"query": "decline error"}}]},
    })
    row = evaluate([CASE], FakeService(state), None, pause=0)[0]
    assert row["tool_calls"] == [
        {"agent": "support", "name": "get_terminal_status", "args": {}},
        {"agent": "support", "name": "get_recent_transactions", "args": {"limit": 5, "status": "declined"}},
        {"agent": "knowledge", "name": "search_getnet_kb", "args": {"query": "decline error"}},
    ]
    assert row["tools_called"] == ["get_terminal_status", "get_recent_transactions", "search_getnet_kb"]  # unchanged


# ---------------- multi-turn cases ----------------

def multi_turn_cases():
    return [c for c in load_cases(None) if c.get("history")]


def test_multi_turn_cases_are_well_formed():
    cases = multi_turn_cases()
    assert len(cases) >= 3
    for c in cases:
        roles = [t["role"] for t in c["history"]]
        assert roles == ["user", "assistant"] * (len(roles) // 2) and roles, c["id"]  # alternate, start with the customer, end with the assistant
        assert all(t["content"].strip() for t in c["history"]), c["id"]


def test_the_history_is_given_to_the_graph_as_context_and_single_turn_cases_are_called_as_before():
    from langchain_core.messages import AIMessage, HumanMessage

    from eval.run_eval import evaluate, history_messages, judge_question

    case = {**CASE, "history": [{"role": "user", "content": "I like green"}, {"role": "assistant", "content": "Noted"}]}
    assert [type(m) for m in history_messages(case)] == [HumanMessage, AIMessage]

    seen = {}

    class Recording(FakeService):
        def run_raw(self, message, user_id, thread_id=None, **kw):
            seen["kw"] = kw
            return super().run_raw(message, user_id, thread_id)

    rows = evaluate([case], Recording(good_state()), None, pause=0)
    assert [m.content for m in seen["kw"]["context"]] == ["I like green", "Noted"]
    assert rows[0]["history"] == case["history"]

    # a case without history never passes `context` (so a service with the old signature still works)
    evaluate([CASE], FakeService(good_state()), None, pause=0)
    assert judge_question(CASE) == "hi"
    q = judge_question(case)
    assert "Customer: I like green" in q and "Assistant: Noted" in q and q.endswith("hi")


def _j(**kw):
    base = {"unsupported_claims": [], "language_match": True, "relevance": 5, "groundedness": 5,
            "completeness": 5, "tone": 5, "verdict": "pass", "reason": "r"}
    return json.dumps({**base, **kw})


def test_an_actionable_unsupported_claim_caps_groundedness_at_3_and_fails():
    out = parse_judgement(_j(unsupported_claims=[{"claim": "open Menu > X", "actionable": True}]))
    assert out["groundedness"] == 3 and out["verdict"] == "fail"


def test_a_minor_unsupported_claim_caps_at_4_and_still_passes():
    out = parse_judgement(_j(unsupported_claims=[{"claim": "usually quick", "actionable": False}]))
    assert out["groundedness"] == 4 and out["verdict"] == "pass"


def test_answering_in_another_language_fails_even_with_top_scores():
    assert parse_judgement(_j(language_match=False))["verdict"] == "fail"


def test_old_format_without_the_new_fields_is_unchanged():
    out = parse_judgement('{"relevance":5,"groundedness":5,"completeness":5,"tone":5,"verdict":"pass"}')
    assert out["verdict"] == "pass" and out["groundedness"] == 5 and out["unsupported_claims"] == []

class _LLM:
    def __init__(self, text):
        self.text = text

    def invoke(self, messages, config=None):
        return type("M", (), {"content": self.text})()


def test_the_code_overrides_a_wrong_language_call_of_the_model():
    llm = _LLM(_j(language_match=False, verdict="fail"))
    out = Judge(llm=llm).score("How many installments can I use?", "You can split a sale into up to 48 installments.", ["e"])
    assert out["language_match"] is True and out["verdict"] == "pass"


def test_the_code_catches_a_wrong_language_the_model_missed():
    out = Judge(llm=_LLM(_j())).score("Which of the two days was bigger?", "Você teve mais vendas hoje: 450 reais.", [])
    assert out["language_match"] is False and out["verdict"] == "fail"


def test_the_eval_rows_carry_verify_raw_and_the_eval_asks_the_graph_for_it():
    from eval.run_eval import evaluate

    rows = evaluate([CASE], FakeService(good_state(verify_note="revised:1 [x]", verify_raw='{"statements": []}')), None, pause=0)
    assert rows[0]["verify_note"] == "revised:1 [x]" and rows[0]["verify_raw"] == '{"statements": []}'
    assert evaluate([CASE], FakeService(good_state()), None, pause=0)[0]["verify_raw"] == ""  # a state without it
    import inspect

    from eval import run_eval

    assert "keep_verify_raw=True" in inspect.getsource(run_eval.main)  # production never sets it
