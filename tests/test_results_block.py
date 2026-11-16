"""The README's results are generated from saved runs, with the n each rate is over. A partial scoring never passes for a full one."""
import json

import pytest

from eval.rejudge import judged_path
from eval.results_block import END, START, block, main, replace_in

BASE = {"routing_ok": True, "tools_ok": True, "handoff_ok": None, "latency_ms": 4000, "degraded": False, "plan": [], "tools_called": []}


def judged(verdict="pass", g=5, c=5):
    return {"verdict": verdict, "relevance": 5, "groundedness": g, "completeness": c, "tone": 5, "reason": "x"}


def write_run(tmp_path, name, rows, summary=None):
    path = tmp_path / name
    path.write_text(json.dumps({"summary": summary or {}, "rows": rows}), encoding="utf-8")
    return str(path)


def rows(ids, scored_ids=(), holdout=()):
    out = []
    for i in ids:
        r = {**BASE, "id": i, "answer": "a"}
        if i in holdout:
            r["holdout"] = True
        if i in scored_ids:
            r["judge"] = judged("fail" if i.endswith("f") else "pass")
        out.append(r)
    return out


def test_a_judge_rate_states_how_many_rows_it_is_over_and_pending_rows_are_not_in_it(tmp_path):
    a = write_run(tmp_path, "a.judged.json", rows(["q01", "q02f", "q03", "q04"], scored_ids={"q01", "q02f", "q03", "q04"}))
    b = write_run(tmp_path, "b.judged.json", rows(["q01", "q02f", "q03", "q04"], scored_ids={"q01"}))  # the judge stopped after one row
    text = block([a, b])
    assert "Judge: scored 5 of 8 rows" in text and "per run: 4 of 4, 1 of 4" in text and "3 still pending" in text
    assert "**Judge pass rate** | 80% (n=5)" in text  # 4 passes of the 5 scored rows, NOT of 8
    assert "Routing | 100% (n=8)" in text and "pass in some runs" in text


def test_a_run_nobody_has_scored_shows_only_the_checks_and_says_so(tmp_path):
    text = block([write_run(tmp_path, "a.json", rows(["q01", "q02"]))])
    assert "has not scored these runs yet" in text and "Judge pass rate" not in text and "Routing | 100% (n=2)" in text


def test_holdout_is_shown_apart_by_group_and_a_rate_over_fewer_cases_has_its_own_n(tmp_path):
    rs = rows(["q01", "ho02_x", "ho07_y"], scored_ids={"q01", "ho07_y"}, holdout={"ho02_x", "ho07_y"})
    rs[0]["handoff_ok"] = True
    text = block([write_run(tmp_path, "a.judged.json", rs)])
    assert "holdout, original" in text and "holdout, blind" in text
    assert "Handoff accuracy | 100% (n=1) | - | - |" in text.replace("  ", " ")  # only one case had a handoff expectation
    assert "(n=1)" in text.split("Judge pass rate")[1]


def test_the_partial_scoring_state_survives_the_real_rejudge_file_naming(tmp_path):
    src = write_run(tmp_path, "run.json", rows(["q01", "q02"]))
    judged_path(src).write_text(json.dumps({"summary": {"label": "replan on", "replan_max": 2}, "rows": rows(["q01", "q02"], scored_ids={"q01"})}),
                                encoding="utf-8")
    text = block([str(judged_path(src))])
    assert "label=replan on, replan_max=2" in text and "scored 1 of 2 rows" in text


def test_the_block_replaces_only_what_is_between_the_markers(tmp_path):
    readme = f"before\n{START}\nold numbers\n{END}\nafter\n"
    run = write_run(tmp_path, "a.json", rows(["q01"]))
    path = tmp_path / "README.md"
    path.write_text(readme, encoding="utf-8")
    assert main([run, "--readme", str(path)]) == 0
    out = path.read_text(encoding="utf-8")
    assert out.startswith("before\n" + START) and out.endswith(END + "\nafter\n") and "old numbers" not in out and "Routing" in out
    with pytest.raises(ValueError, match="no <!-- results:start -->"):
        replace_in("no markers here", "x")


def test_the_real_readme_has_the_markers():
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
    assert text.count(START) == 1 and text.count(END) == 1 and text.index(START) < text.index(END)
