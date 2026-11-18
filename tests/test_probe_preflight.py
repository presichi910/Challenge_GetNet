"""eval/probe_preflight.py: 48 rows to judge, and every one with its case, or STOP. It would have caught what a plain rejudge dry-run does not: ids the dataset
does not know are counted by the dry-run and skipped, silently, by the real run."""
import json
from pathlib import Path

import pytest

from eval.probe_preflight import DEFAULT_DATASET, check, main

IDS = [json.loads(line)["id"] for line in DEFAULT_DATASET.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture(autouse=True)
def restore_env(monkeypatch):
    monkeypatch.setenv("EVAL_DATASET", "")  # blank = unset; main() sets the variable itself, and this makes the test put it back as it was


def run(tmp_path: Path, name: str, ids=IDS, judged=(), extra=()) -> str:
    rows = [{"id": i, "answer": f"answer {i}", "evidence": [], **({"judge": {"verdict": "pass"}} if i in judged else {})} for i in ids]
    rows += [{"id": i, "answer": "x", "evidence": []} for i in extra]
    path = tmp_path / name
    path.write_text(json.dumps({"summary": {}, "rows": rows}), encoding="utf-8")
    return str(path)


def four(tmp_path, **kw) -> list[str]:
    return [run(tmp_path, f"r{n}.json", **kw) for n in range(4)]


def test_four_runs_of_the_twelve_cases_are_48_rows_and_go(tmp_path, capsys):
    assert main(four(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "total rows to judge: 48 (expected 48)" in out and "GO: 48 rows" in out and len(IDS) == 12


def test_a_count_other_than_48_stops_it(tmp_path, capsys):
    paths = four(tmp_path)
    paths[3] = run(tmp_path, "short.json", ids=IDS[:11])  # one case missing in the last run
    assert main(paths) == 1
    out = capsys.readouterr().out
    assert "STOP" in out and "47 rows to judge, not 48" in out and f"missing: {[IDS[11]]}" in out


def test_rows_already_judged_lower_the_count_and_stop_it_too(tmp_path, capsys):
    paths = four(tmp_path)
    paths[0] = run(tmp_path, "done.json", judged={IDS[0]})
    assert main(paths) == 1 and "47 rows to judge, not 48" in capsys.readouterr().out


def test_an_id_the_cases_file_does_not_know_is_named_because_rejudge_would_skip_it_silently(tmp_path, capsys):
    paths = four(tmp_path)
    paths[1] = run(tmp_path, "odd.json", ids=[*IDS[:11], "q03_payout_timing"])  # a regular case of the dataset, not of the probe
    assert main(paths) == 1
    out = capsys.readouterr().out
    assert "q03_payout_timing" in out and "skip them silently" in out


def test_without_the_probe_file_as_the_dataset_every_probe_row_is_lost(tmp_path, capsys):
    """What the plain dry-run does not show: with the variable unset, rejudge counts 48 and skips all of them."""
    from eval.rejudge import load_rows, needs_judging

    paths = four(tmp_path)
    assert sum(needs_judging(r) for p in paths for r in load_rows(p)[1]) == 48  # the dry-run's number
    from eval.run_eval import load_cases

    cases = {c["id"]: c for c in load_cases(None)}  # the default dataset: the variable is unset
    _, problems = check(paths, 48, cases)
    assert any("no case in the cases file" in p for p in problems)


def test_a_duplicated_id_and_a_case_without_criterion_are_reported(tmp_path):
    p = run(tmp_path, "dup.json", ids=[*IDS, IDS[0]])
    cases = {i: {"criteria": "c"} for i in IDS}
    _, problems = check([p], 13, cases)
    assert any("more than once" in x for x in problems)
    cases[IDS[1]] = {"criteria": ""}
    _, problems = check([run(tmp_path, "ok.json")], 12, cases)
    assert any("no criterion" in x for x in problems)
