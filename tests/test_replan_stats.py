"""eval/replan_stats.py: what the observe step did in real runs, no judge."""
import json

from eval.replan_stats import main, render, rows_of, stats


def row(i, latency, cost, replans=(), handoff=None, tokens=1000, **kw):
    return {"id": i, "latency_ms": latency, "usage": {"cost_usd": cost, "total_tokens": tokens}, "replans": list(replans), "handoff": handoff, **kw}


def run_file(tmp_path, name, rows):
    p = tmp_path / name
    p.write_text(json.dumps({"summary": {}, "rows": rows}), encoding="utf-8")
    return str(p)


def test_it_counts_each_kind_of_action_the_extra_voyage_calls_and_the_escalations():
    rows = [row("q04", 5000, 0.002, ["retry_kb: 1 unsupported -> 5 passages -> 0 still unsupported"]),
            row("q08", 9000, 0.004, ["retry_kb: 3 unsupported -> 5 passages -> 4 still unsupported", "offer_ticket: no step is left"]),
            row("q05", 6000, 0.003, ["retry_kb: 5 unsupported -> 5 passages -> 5 still unsupported", "escalate: nothing in the answer is backed by the evidence"],
                handoff="agent_unresolved"),
            row("q09", 4000, 0.001, ["retry_kb_failed: RateLimitError"]), row("q01", 3000, 0.001)]
    s = stats(rows)
    assert s["acted"] == 4 and s["acted_ids"] == ["q04", "q05", "q08", "q09"]
    assert s["kinds"] == {"retry_kb:": 3, "retry_kb_failed": 1, "offer_ticket": 1, "escalate": 1}
    assert s["escalated_by_observe"] == 1 and s["handoffs"] == 1 and s["voyage_extra_calls"] == 4
    assert s["latency_p50"] == 5000 and s["cost_total"] == 0.011 and round(s["tokens_mean"]) == 1000


def test_provider_errors_and_crashed_rows_are_left_out(tmp_path):
    f = run_file(tmp_path, "a.json", [row("q01", 1000, 0.001), {"id": "q02", "provider_error": "429"}, {"id": "q03", "error": "boom"}])
    assert [r["id"] for r in rows_of([f])] == ["q01"]


def test_the_report_compares_the_runs_says_the_off_branch_must_not_act_and_does_not_claim_better_answers(tmp_path):
    off = [run_file(tmp_path, "off1.json", [row("q01", 4000, 0.002), row("q05", 6000, 0.003)])]
    on = [run_file(tmp_path, "on1.json", [row("q01", 4000, 0.002), row("q05", 11000, 0.005, ["retry_kb: 5 unsupported -> 5 passages -> 0 still unsupported"])])]
    out = tmp_path / "r.md"
    assert main(["--off", *off, "--on", *on, "-o", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "Acted in **1 of 2** case-runs (50%): q05" in text and "+2.50 s" in text and "+5.0 s" in text and "+0.0010" in text
    assert "it must be 0: the step is off" in text and "does not say whether the answers are better" in text
    assert "Escalated to a human instead of answering: 0" in text


def test_it_renders_when_there_is_no_cost_recorded():
    s = stats([{"id": "q01", "latency_ms": 1000, "replans": []}])
    assert s["cost_mean"] is None and "n/a" in render(s, s, ["a"], ["b"])


def test_handoffs_are_compared_as_a_rate_because_the_branches_ran_different_numbers_of_case_runs():
    off = stats([row(f"c{i}", 1000, 0.001, handoff="agent_unresolved" if i < 6 else None) for i in range(200)])   # 5 repetitions: 30 of 200
    on = stats([row(f"c{i}", 1000, 0.001, handoff="agent_unresolved" if i < 6 else None) for i in range(80)])     # 2 repetitions: 12 of 80... here 6 of 80
    text = render(off, on, ["a"], ["b"])
    assert "handoff rate | 6/200 = 3.0% | 6/80 = 7.5% | +4.5 points" in text and "| handoffs |" not in text
