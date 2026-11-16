"""eval/same_behavior.py: the post-final smoke test says whether routing, tools and handoff are unchanged, citing both commits."""
import json

from eval.same_behavior import compare, main, render

FINAL_COMMIT, SMOKE_COMMIT = "a" * 40, "b" * 40


def row(i, routing=True, tools=True, handoff=None, memory=None):
    return {"id": i, "routing_ok": routing, "tools_ok": tools, "handoff_ok": handoff, "memory_ok": memory}


def run(rows, commit, clean=True):
    return {"summary": {"commit": commit, "tree_clean": clean}, "rows": rows}


def final_runs():
    base = [row("q01"), row("q02"), row("h01", handoff=True), row("m01", memory=True)]
    flaky = [row("q01"), row("q02", routing=False), row("h01", handoff=True), row("m01", memory=True)]  # q02 varies between repetitions
    return [run(base, FINAL_COMMIT), run(flaky, FINAL_COMMIT), run(base, FINAL_COMMIT)]


def test_a_smoke_inside_the_spread_with_no_new_failure_is_unchanged_and_cites_both_commits():
    smoke = run([row("q01"), row("q02"), row("h01", handoff=True), row("m01", memory=True)], SMOKE_COMMIT)
    res = compare(final_runs(), smoke)
    assert res["unchanged"] and res["suspects"] == [] and res["checks"]["routing"]["inside"] is True
    text = render(res, ["f1.json", "f2.json", "f3.json"], "smoke.json")
    assert FINAL_COMMIT[:12] in text and SMOKE_COMMIT[:12] in text and "NO CHANGE in routing, tools and handoff" in text


def test_a_case_that_passed_every_repetition_and_fails_in_the_smoke_is_a_suspect_and_the_verdict_is_changed():
    smoke = run([row("q01", routing=False), row("q02"), row("h01", handoff=True), row("m01", memory=True)], SMOKE_COMMIT)
    res = compare(final_runs(), smoke)
    assert res["suspects"] == [("q01", "routing")] and res["unchanged"] is False
    assert "CHANGED" in render(res, ["f.json"], "s.json") and "q01 (routing)" in render(res, ["f.json"], "s.json")


def test_a_rate_outside_the_spread_the_final_itself_showed_is_not_unchanged():
    steady = [run([row("q01"), row("q02")], FINAL_COMMIT) for _ in range(3)]  # routing 100% in every repetition: no spread at all
    smoke = run([row("q01"), row("q02", routing=False)], SMOKE_COMMIT)
    res = compare(steady, smoke)
    assert res["checks"]["routing"]["inside"] is False and res["unchanged"] is False


def test_a_case_that_failed_every_repetition_and_passes_now_is_listed_as_improved_not_as_a_problem():
    always_bad = [run([row("ho02", routing=False)], FINAL_COMMIT) for _ in range(3)]
    res = compare(always_bad, run([row("ho02")], SMOKE_COMMIT))
    assert res["improved"] == [("ho02", "routing")] and res["suspects"] == []


def test_a_smoke_on_the_same_commit_says_it_only_shows_run_to_run_variation(tmp_path):
    f, s = tmp_path / "f.json", tmp_path / "s.json"
    f.write_text(json.dumps(run([row("q01")], FINAL_COMMIT)), encoding="utf-8")
    s.write_text(json.dumps(run([row("q01")], FINAL_COMMIT)), encoding="utf-8")
    out = tmp_path / "SMOKE.md"
    assert main(["--final", str(f), "--smoke", str(s), "-o", str(out)]) == 0
    assert "SAME commit" in out.read_text(encoding="utf-8")


def test_the_exit_code_is_non_zero_when_something_changed(tmp_path):
    f, s = tmp_path / "f.json", tmp_path / "s.json"
    f.write_text(json.dumps(run([row("q01")], FINAL_COMMIT)), encoding="utf-8")
    s.write_text(json.dumps(run([row("q01", tools=False)], SMOKE_COMMIT)), encoding="utf-8")
    assert main(["--final", str(f), "--smoke", str(s)]) == 1
