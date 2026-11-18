"""Before any judge token is spent on the replan probe (docs/REPLAN_PROBE_PREREG.md, amendment 0): are these the 48 rows, and does the judge's batch tool find each one's case?

    python -m eval.probe_preflight OFF1.json ON1.json OFF2.json ON2.json            # the four runs, in the order they will be judged
    python -m eval.probe_preflight ... --expect 48 --dataset eval/probe/replan_probe.jsonl

`eval/rejudge.py --dry-run` counts the rows that need a verdict, but it does not look at the dataset: a row whose id it does not find is skipped, silently, when it
scores for real. So a dry-run that says 48 does not show that EVAL_DATASET was set. This script checks both, and it only reads files (no model, no judge, no network):

  * every file holds exactly the ids of the cases file (no case missing, none twice, none extra);
  * the number of rows that still need a verdict, summed over the files, is the expected one (48 by default);
  * every one of those rows finds its case, and a criterion, in the cases file that `load_cases` reads with that dataset.

Exit code 0 = go; 1 = STOP (and it says why). It does not call `rejudge`: it reads the same files the same way (`load_rows`, `needs_judging`).
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

DEFAULT_DATASET = Path(__file__).resolve().parent / "probe" / "replan_probe.jsonl"


def check(paths: list[str], expect: int, cases: dict[str, dict]) -> tuple[list[str], list[str]]:
    """-> (report lines, problems). No problem = go."""
    from eval.rejudge import load_rows, needs_judging

    lines, problems = [], []
    total = 0
    want = set(cases)
    for p in paths:
        _, rows = load_rows(p)
        ids = [r["id"] for r in rows]
        todo = [r["id"] for r in rows if needs_judging(r)]
        total += len(todo)
        lines.append(f"{Path(p).name}: {len(rows)} rows, {len(todo)} to judge")
        if len(ids) != len(set(ids)):
            problems.append(f"{Path(p).name}: an id appears more than once: {sorted({i for i in ids if ids.count(i) > 1})}")
        if set(ids) != want:
            problems.append(f"{Path(p).name}: its ids are not those of the cases file (missing: {sorted(want - set(ids)) or 'none'}; extra: {sorted(set(ids) - want) or 'none'})")
        lost = [i for i in todo if i not in cases]
        if lost:
            problems.append(f"{Path(p).name}: {len(lost)} row(s) to judge have no case in the cases file, so rejudge would skip them silently: {sorted(lost)}")
        bare = [i for i in todo if i in cases and not cases[i].get("criteria")]
        if bare:
            problems.append(f"{Path(p).name}: row(s) whose case has no criterion: {sorted(bare)}")
    lines.append(f"total rows to judge: {total} (expected {expect})")
    if total != expect:
        problems.append(f"{total} rows to judge, not {expect}")
    return lines, problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("runs", nargs="+", help="the run JSON files (the .judged.json of a previous pass is used if it exists, as rejudge does)")
    ap.add_argument("--expect", type=int, default=48)
    ap.add_argument("--dataset", default=str(DEFAULT_DATASET), help="the cases file; it is also what EVAL_DATASET must name when rejudge runs")
    args = ap.parse_args(argv)
    os.environ["EVAL_DATASET"] = str(Path(args.dataset).resolve())
    from eval.run_eval import load_cases

    cases = {c["id"]: c for c in load_cases(None)}
    lines, problems = check(args.runs, args.expect, cases)
    print(f"cases file: {os.environ['EVAL_DATASET']} ({len(cases)} cases)")
    print("\n".join(lines))
    if problems:
        print("\nSTOP. Nothing may be judged until this is fixed:")
        print("\n".join(f"  - {p}" for p in problems))
        return 1
    print(f"\nGO: {args.expect} rows, every one with its case and criterion. Run rejudge with EVAL_DATASET={os.environ['EVAL_DATASET']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
