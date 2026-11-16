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
    assert "holdout, original" in text and "holdout, new, not targeted" in text and "holdout, baseline seen" in text
    assert "holdout, blind" not in text  # no column claims blindness, and there is no pooled ho06-ho13 total
    assert "Handoff accuracy | 100% (n=1) | - | - | - |" in text.replace("  ", " ")  # only one case had a handoff expectation
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


def test_the_generated_block_carries_the_calibration_sample_note_about_ho06_and_ho12(tmp_path):
    text = block([write_run(tmp_path, "a.judged.json", rows(["q01"], scored_ids={"q01"}))])
    assert "Calibration sample" in text and "ON PURPOSE as edge cases (2 of the 15)" in text


def test_the_block_cites_the_commit_the_runs_came_from_and_whether_the_tree_was_clean(tmp_path):
    summary = {"commit": "22423e2f10249eb38bcaa8e8583bdaac49b5b35a", "tree_clean": True, "replan_max": 0}
    text = block([write_run(tmp_path, "a.judged.json", rows(["q01"]), summary), write_run(tmp_path, "b.judged.json", rows(["q01"]), summary)])
    assert "commit=22423e2f1024, tree_clean=True" in text and "replan_max=0" in text
    mixed = block([write_run(tmp_path, "c.json", rows(["q01"]), summary), write_run(tmp_path, "d.json", rows(["q01"]), {**summary, "commit": "b" * 40})])
    assert "commit=22423e2f1024/bbbbbbbbbbbb" in mixed  # runs from two commits are said to be from two commits


def test_the_baseline_seen_holdout_cases_have_their_own_column_and_are_not_in_the_new_holdout(tmp_path):
    ids = ["q01", "ho02_x", "ho06_a", "ho07_b", "ho08_c", "ho09_d", "ho12_f"]
    rs = rows(ids, scored_ids=set(ids), holdout=set(ids) - {"q01"})
    text = block([write_run(tmp_path, "a.judged.json", rs)])
    lines = {line.split("|")[1].strip(): [c.strip() for c in line.split("|")[2:-1]] for line in text.splitlines() if line.startswith("| ") and "|---" not in line}
    header = next(line for line in text.splitlines() if line.startswith("| ") and "regular" in line)
    assert "holdout, original (1 cases)" in header and "holdout, new, not targeted (2 cases)" in header and "holdout, baseline seen (3 cases)" in header
    assert lines["Cases x runs"] == ["1", "1", "2", "3"]  # regular | original | new, not targeted (ho07, ho09) | seen (ho06, ho08, ho12)
    assert lines["**Judge pass rate**"] == ["100% (n=1)", "100% (n=1)", "100% (n=2)", "67% (n=3)"]  # ho12_f is a fail: 2 of the 3 seen
    assert "they are not blind" in text.replace("They are not blind", "they are not blind") and "ho06, ho08, ho12" in text
    assert "holdout, blind" not in text


def test_without_baseline_seen_rows_that_column_is_empty_and_there_is_no_note(tmp_path):
    ids = ["q01", "ho02_x", "ho07_y"]
    text = block([write_run(tmp_path, "a.judged.json", rows(ids, scored_ids=set(ids), holdout={"ho02_x", "ho07_y"}))])
    assert "holdout, baseline seen" in text and "Holdout, baseline seen (" not in text


def test_the_baseline_seen_set_is_written_by_hand_and_names_cases_that_are_holdout_in_the_dataset():
    import ast
    from pathlib import Path

    from eval import results_block

    assert results_block.SEEN_BASELINE == ("ho06", "ho08", "ho12")
    assert results_block.is_seen_baseline("ho06_kb_gap_pix_automatico") and results_block.is_seen_baseline("ho12")
    assert not results_block.is_seen_baseline("ho07_partner") and not results_block.is_seen_baseline("ho060_x") and not results_block.is_seen_baseline("q06")
    tree = ast.parse(Path(results_block.__file__).read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name) and n.targets[0].id == "SEEN_BASELINE")
    assert isinstance(node.value, ast.Tuple) and all(isinstance(e, ast.Constant) for e in node.value.elts)  # a literal: it cannot be computed from results
    dataset = {c["id"].split("_")[0]: c for c in map(json.loads, (Path(results_block.__file__).parent / "dataset.jsonl").read_text(encoding="utf-8").splitlines())}
    assert all(dataset[i].get("holdout") for i in results_block.SEEN_BASELINE)
