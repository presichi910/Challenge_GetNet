"""--repeats: spread of the metrics over N runs, pass count per case, unstable cases. No network."""
import pytest

from eval import repeats
from eval import run_eval
from eval.repeats import aggregate, case_passes, render, spread

OK = {"routing_ok": True, "tools_ok": True, "handoff_ok": True, "latency_ms": 10,
      "judge": {"verdict": "pass", "relevance": 5, "groundedness": 5, "completeness": 5, "tone": 5}}


def test_case_passes_needs_every_check():
    assert case_passes(OK) is True
    assert case_passes({**OK, "routing_ok": False}) is False
    assert case_passes({**OK, "handoff_ok": False}) is False
    assert case_passes({**OK, "handoff_ok": None}) is True  # no expectation
    assert case_passes({**OK, "judge": {"verdict": "fail"}}) is False
    assert case_passes({**OK, "judge": {"verdict": "error"}}) is None  # a judge outage says nothing: not a pass, not a fail
    assert case_passes({**OK, "routing_ok": False, "judge": {"verdict": "error"}}) is False  # a failed check still fails
    assert case_passes({"id": "x", "provider_error": "router: RateLimitError"}) is None
    assert case_passes({"id": "x", "error": "boom"}) is False


def test_spread_is_mean_min_max_per_metric():
    s = spread([{"cases": 3, "routing_accuracy": 1.0, "avg_tone": 5}, {"cases": 3, "routing_accuracy": 0.5, "avg_tone": 4},
                {"cases": 3, "routing_accuracy": 0.75, "avg_tone": 3, "tokens_by_model": []}])
    assert s["routing_accuracy"] == {"mean": pytest.approx(0.75), "min": 0.5, "max": 1.0, "n": 3}
    assert s["avg_tone"]["mean"] == pytest.approx(4) and "cases" not in s and "tokens_by_model" not in s


def test_aggregate_counts_passes_and_finds_unstable_and_always_failing_cases():
    run1 = [{**OK, "id": "a"}, {**OK, "id": "b"}, {**OK, "id": "c", "routing_ok": False}, {**OK, "id": "h", "holdout": True}]
    run2 = [{**OK, "id": "a"}, {**OK, "id": "b", "judge": {**OK["judge"], "verdict": "fail"}}, {**OK, "id": "c", "routing_ok": False},
            {"id": "h", "holdout": True, "provider_error": "x"}]
    run3 = [{**OK, "id": "a"}, {**OK, "id": "b"}, {**OK, "id": "c", "tools_ok": False}, {**OK, "id": "h", "holdout": True}]
    runs = [run1, run2, run3]
    agg = aggregate(runs, [run_eval.summarize_all(r) for r in runs])
    assert agg["cases"]["a"] == {"passed": 3, "ran": 3, "provider_errors": 0, "judge_errors": 0, "holdout": False, "pending": 0}
    assert agg["cases"]["b"]["passed"] == 2 and agg["unstable"] == ["b"]
    assert agg["always_fail"] == ["c"]
    assert agg["cases"]["h"] == {"passed": 2, "ran": 2, "provider_errors": 1, "judge_errors": 0, "holdout": True, "pending": 0}
    assert agg["provider_errors"] == 1
    assert agg["regular"]["routing_accuracy"]["min"] == pytest.approx(2 / 3)
    assert agg["holdout"]["routing_accuracy"]["n"] == 2  # the run where h hit a provider error has no holdout metrics
    text = render(agg, "stamp")
    assert "| b | 2/3 | 0 |" in text and "| h (holdout, original) | 2/2 | 1 |" in text
    assert "**Unstable (pass in some runs, not all):** b" in text


def test_repeats_spreads_the_two_holdouts_apart():
    ho_old = {**OK, "id": "ho02_a", "holdout": True}
    ho_new = {**OK, "id": "ho07_b", "holdout": True, "routing_ok": False}
    runs = [[{**OK, "id": "a"}, ho_old, ho_new] for _ in range(2)]
    agg = aggregate(runs, [run_eval.summarize_all(r) for r in runs])
    assert agg["holdout_original"]["routing_accuracy"]["mean"] == 1.0
    assert agg["holdout_blind"]["routing_accuracy"]["mean"] == 0.0
    text = render(agg, "stamp")
    assert text.index("## Holdout - original (ho01-ho05), seen during development") < text.index("## Holdout - new (ho06 and later; ho06, ho08 and ho12 are baseline seen)")
    assert "| ho02_a (holdout, original) | 2/2 | 0 |" in text and "| ho07_b (holdout, new) | 0/2 | 0 |" in text


def test_run_repeats_writes_one_report_per_run_and_only_the_first_may_become_latest(tmp_path, monkeypatch):
    calls = []

    def fake_write_report(rows, summary, models=None, update_latest=True):
        calls.append(update_latest)
        return tmp_path / f"run{len(calls)}.md"

    monkeypatch.setattr(run_eval, "write_report", fake_write_report)
    monkeypatch.setattr(repeats, "ROOT", tmp_path)

    class Service:
        def run_raw(self, message, user_id, thread_id=None):
            return {"plan": ["support"], "final_answer": "ok",
                    "agent_outputs": {"support": {"answer": "ok", "error": None, "tool_calls": [], "contexts": []}}}, 5

    case = {"id": "q1", "user_id": "u", "message": "hi", "expected_agents": ["support"]}
    done, path = run_eval.run_repeats([case], Service(), None, 3, pause=0)
    assert len(done) == 3 and calls == [True, False, False]
    assert path is not None and path.name.startswith("REPEATS-") and "| q1 | 3/3 | 0 |" in path.read_text(encoding="utf-8")
    calls.clear()
    done, path = run_eval.run_repeats([case], Service(), None, 1, pause=0, update_latest=False)
    assert calls == [False] and path is None


def test_a_judge_error_is_left_out_of_the_pass_count_and_counted_apart():
    bad_judge = {**OK, "judge": {"verdict": "error", "reason": "judge_error: bad json"}}
    runs = [[{**OK, "id": "a"}, {**OK, "id": "b"}], [bad_judge | {"id": "a"}, {**OK, "id": "b"}],
            [{**OK, "id": "a"}, {**bad_judge, "id": "b", "routing_ok": False}]]
    agg = aggregate(runs, [run_eval.summarize_all(r) for r in runs])
    assert agg["cases"]["a"] == {"passed": 2, "ran": 2, "provider_errors": 0, "judge_errors": 1, "holdout": False, "pending": 0}  # the middle run is out
    assert agg["cases"]["b"] == {"passed": 2, "ran": 3, "provider_errors": 0, "judge_errors": 1, "holdout": False, "pending": 0}  # a failed check still fails
    assert agg["judge_errors"] == 2
    text = render(agg, "stamp")
    assert "Judge errors over all runs: 2" in text and "| a | 2/2 | 0 | 0 |" in text
    assert "a" not in agg["unstable"]  # a judge outage must not make a case look unstable either


# ---- a partial scoring (eval/rejudge.py stopped by the daily limit): the n of every judge-based rate, and pending is not a pass ----
NOJ = {k: v for k, v in OK.items() if k != "judge"}  # a row the judge has not scored


def scored(row, verdict="pass"):
    return {**NOJ, **row, "judge": {"verdict": verdict, "relevance": 5, "groundedness": 5, "completeness": 5, "tone": 5, "reason": "x"}}


def test_a_row_the_judge_has_not_scored_yet_is_pending_not_a_pass_in_a_run_that_is_being_scored():
    from eval.repeats import case_passes

    pending_row = {**NOJ, "id": "a"}
    assert case_passes(pending_row) is True  # a --no-judge run: the checks alone decide, as before
    assert case_passes(pending_row, judging=True) is None  # a run being scored: not scored yet says nothing
    assert case_passes({**NOJ, "id": "a", "routing_ok": False}, judging=True) is False  # a failed check is a fail with or without the judge


def test_with_a_partial_scoring_every_judge_rate_declares_how_many_rows_it_is_over():
    full = [scored({"id": "a"}), scored({"id": "b"}, "fail")]
    partial = [scored({"id": "a"}), {**NOJ, "id": "b"}]  # the judge stopped before row b
    none_yet = [{**NOJ, "id": "a"}, {**NOJ, "id": "b"}]  # a --no-judge run, not scored at all
    runs = [full, partial, none_yet]
    agg = aggregate(runs, [run_eval.summarize_all(r) for r in runs])
    rate = agg["regular"]["judge_pass_rate"]
    assert rate["judged"] == [2, 1] and rate["n"] == 2  # the run with no scores has no rate at all, it is not averaged in as 0
    assert rate["pooled"] == pytest.approx(2 / 3)  # 2 passes over the 3 rows that were scored, not over 6
    assert agg["judged_per_run"] == [2, 1, 0] and agg["rows_per_run"] == [2, 2, 2] and agg["pending"] == 1
    assert agg["cases"]["b"] == {"passed": 2, "ran": 2, "provider_errors": 0, "judge_errors": 0, "holdout": False, "pending": 1} or \
        agg["cases"]["b"]["pending"] == 1  # run 3 has no scores at all: its checks decide there; run 2's row is the pending one
    text = render(agg, "stamp")
    assert "Scored by the judge, per run: 2 of 2, 1 of 2, 0 of 2" in text and "judged per run (n)" in text and "pooled 67% of 3" in text
