"""Re-score 19 of the 27 V2 rows with the CURRENT judge, and put three verdicts side by side on the SAME rows: the old judge, the current judge, the human.

    python -m eval.calibration.v2_rescore prepare           # writes eval/reports/V2RESCORE19-20261114-192208.json (the input of eval.rejudge) and prints the 19 ids
    python -m eval.rejudge eval/reports/V2RESCORE19-20261114-192208.json ...   # the judge (see CLAUDE.md for the command of the day)
    python -m eval.calibration.v2_rescore report -o eval/calibration/V2_RESCORE_REPORT.md

WHICH 19: `random.Random(SEED).sample(sorted ids, 19)`. The seed is written in CLAUDE.md BEFORE anything is launched, and the choice reads nothing but the
27 case ids: not a verdict, not a human label, not a score. (Which rows the old judge or the human liked could not have steered it.)

WHAT IT IS NOT: V2 is the sheet the judge was ADJUSTED on (its desagreements q04, q09, q10, x01, x04, m02, m03 led to the rules now in the code). Agreement
of the current judge with the V2 human labels is therefore NOT an independent measure; the independent one is V3 (new answers, a sample fixed before any judge
note existed). This report only says what the change of judge did to rows we already know, and it says it with the n of every number.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # eval/
SEED = 20261116  # Monday: the day of the re-score; fixed and written down before anything runs
K = 19
SOURCE_RUN = ROOT / "reports" / "20261114-192208.json"
SHEET = ROOT / "calibration" / "labels_v2_blind.csv"
INPUT = ROOT / "reports" / "V2RESCORE19-20261114-192208.json"
TOKENS_PER_CHAR = 0.286  # measured on the 40 rows of the final run (judge tokens per character of prompt + question + answer + evidence + criteria)
NOTE = ("V2 is the sheet the judge was ADJUSTED on: its disagreements led to the rules now in the code. The agreement of the current judge with the V2 human "
        "labels is therefore NOT independent of the judge's design. The independent measure is V3 (new answers; the sample was fixed before any judge note existed).")


def choose(ids: list[str], k: int = K, seed: int = SEED) -> list[str]:
    """The k rows, from the ids alone. Returned in the order of the source file, not the order they were drawn."""
    ordered = sorted(ids)
    drawn = set(random.Random(seed).sample(ordered, k))
    return [i for i in ids if i in drawn]


def source_rows(path: Path = SOURCE_RUN) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["rows"]


def prepare(chosen: list[str], src: Path = SOURCE_RUN, out: Path = INPUT) -> Path:
    """The input of `eval.rejudge`: the chosen rows with their answer and evidence, WITHOUT the old judge's verdict (or rejudge would skip them as scored)."""
    rows = [{k: v for k, v in r.items() if k not in ("judge", "judge_usage")} for r in source_rows(src) if r["id"] in set(chosen)]
    out.write_text(json.dumps({"summary": {"label": "V2 answers (2026-11-14) scored by the CURRENT judge", "source_run": src.name, "seed": SEED,
                                           "commit": "n/a (answers of the 2026-11-14 run)"}, "rows": rows}, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


def estimate_tokens(rows: list[dict], cases: dict[str, dict]) -> int:
    from app.prompts import JUDGE_PROMPT
    from eval.run_eval import judge_question

    chars = sum(len(JUDGE_PROMPT) + len(judge_question(cases[r["id"]])) + len(r["answer"]) + sum(len(e) for e in r.get("evidence", [])) + len(cases[r["id"]].get("criteria", ""))
                for r in rows)
    return int(chars * TOKENS_PER_CHAR)


def kappa(pairs: list[tuple[bool, bool]]) -> float | None:
    n = len(pairs)
    if not n:
        return None
    po = sum(a == b for a, b in pairs) / n
    pa, pb = sum(a for a, _ in pairs) / n, sum(b for _, b in pairs) / n
    pe = pa * pb + (1 - pa) * (1 - pb)
    return None if pe == 1 else (po - pe) / (1 - pe)


def pct(k: int, n: int) -> str:
    return f"{k}/{n} = {k / n * 100:.0f}%" if n else "n=0"


def build(old_rows: dict[str, dict], new_rows: dict[str, dict], human: dict[str, bool], ids: list[str], all_ids: list[str] | None = None) -> dict:
    """The numbers. `new_rows`: the rescored rows (may be partial). A row the current judge has not scored (or scored as an error) is left out of the
    SAME-ROWS comparison, and says so."""
    old = {i: old_rows[i]["judge"]["verdict"] == "pass" for i in ids}
    new = {i: new_rows[i]["judge"]["verdict"] == "pass" for i in ids if (new_rows.get(i, {}).get("judge") or {}).get("verdict") in ("pass", "fail")}
    errors = [i for i in ids if (new_rows.get(i, {}).get("judge") or {}).get("verdict") == "error"]
    same = [i for i in ids if i in new]
    left_out = [i for i in (all_ids or ids) if i not in ids]
    oldv = {i: old_rows[i]["judge"]["verdict"] == "pass" for i in (all_ids or ids)}
    return {"ids": ids, "n_left_out": len(left_out),
            "old_agree_selected": sum(oldv[i] == human[i] for i in ids), "old_agree_left_out": sum(oldv[i] == human[i] for i in left_out), "n_selected": len(ids), "n_rescored": len(same), "judge_errors": errors, "pending": [i for i in ids if i not in new and i not in errors],
            "old_all": sum(old[i] for i in ids), "human_all": sum(human[i] for i in ids),
            "old_same": sum(old[i] for i in same), "new_same": sum(new[i] for i in same), "human_same": sum(human[i] for i in same),
            "agree_old": sum(old[i] == human[i] for i in same), "agree_new": sum(new[i] == human[i] for i in same),
            "kappa_old": kappa([(old[i], human[i]) for i in same]), "kappa_new": kappa([(new[i], human[i]) for i in same]),
            "changed": [(i, "pass" if old[i] else "fail", "pass" if new[i] else "fail") for i in same if old[i] != new[i]]}


def render(r: dict) -> str:
    n, s = r["n_selected"], r["n_rescored"]
    k = lambda v: "n/a" if v is None else f"{v:.2f}"  # noqa: E731
    L = ["# V2 rows re-scored with the current judge", "", f"_{NOTE}_", "",
         f"- Rows: **{n} of the 27 V2 rows**, chosen with seed {SEED} from the ids alone (`random.Random({SEED}).sample`). Scored by the current judge so far: **{s} of {n}**"
         f"{'' if not r['pending'] else f' ({len(r['pending'])} still pending)'}{'' if not r['judge_errors'] else f', {len(r['judge_errors'])} judge error(s) left out'}.", "",
         "## Pass rate on the same rows", "", "| who | rows | pass |", "|---|---|---|",
         f"| old judge (the one V2 was measured with) | {s} | {pct(r['old_same'], s)} |",
         f"| **current judge** | {s} | **{pct(r['new_same'], s)}** |",
         f"| human (V2 labels) | {s} | {pct(r['human_same'], s)} |", "",
         f"_For the record, on all {n} selected rows (not only the {s} re-scored): old judge {pct(r['old_all'], n)}, human {pct(r['human_all'], n)}._", "",
         "## Agreement with the human on those rows (pass/fail)", "", "| judge | agrees with the human | Cohen's kappa |", "|---|---|---|",
         f"| old | {pct(r['agree_old'], s)} | {k(r['kappa_old'])} |", f"| current | {pct(r['agree_new'], s)} | {k(r['kappa_new'])} |", "",
         f"_n = {s}: a difference of one row is {100 / s:.0f} points. Read the direction, not the decimals._" if s else "_Nothing has been re-scored yet._", ""]
    L += ["## About the draw", "",
          f"The 19 were drawn from the ids alone, with a seed fixed beforehand. By chance (about 6 draws in 100) the draw contains EVERY row on which the old judge and "
          f"the human disagreed in V2: the old judge agreed with the human on **{pct(r['old_agree_selected'], n)}** of the selected rows and on "
          f"**{pct(r['old_agree_left_out'], r['n_left_out'])}** of the {r['n_left_out']} left out. So the old judge starts from a lower agreement on these {n} rows than on all 27, "
          "and the comparison below favors a change of judge more than a random draw would. The seed was not changed after seeing this: that would be choosing the rows by their results.", ""]
    L += ["## Rows where the verdict changed with the judge", "",
          ", ".join(f"{i} ({a} -> {b})" for i, a, b in r["changed"]) or "none", ""]
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("prepare")
    rep = sub.add_parser("report")
    rep.add_argument("-o", "--out")
    rep.add_argument("--rescored", default=str(INPUT.with_name(INPUT.stem + ".judged.json")))
    args = ap.parse_args(argv)

    old_rows = {r["id"]: r for r in source_rows()}
    chosen = choose(list(old_rows))
    if args.cmd == "prepare":
        from eval.run_eval import load_cases

        out = prepare(chosen)
        rows = [r for r in source_rows() if r["id"] in set(chosen)]
        est = estimate_tokens(rows, {c["id"]: c for c in load_cases(None)})
        print(f"seed {SEED}: {len(chosen)} rows -> {out}\n  " + ", ".join(chosen) + f"\nestimated judge cost: ~{est} tokens (+-25%: {int(est * .75)}-{int(est * 1.25)})")
        return 0
    human = {r["id"]: r["human_pass"].strip().lower() == "yes" for r in csv.DictReader(SHEET.open(encoding="utf-8"))}
    path = Path(args.rescored)
    new_rows = {r["id"]: r for r in json.loads(path.read_text(encoding="utf-8"))["rows"]} if path.exists() else {}
    text = render(build(old_rows, new_rows, human, chosen, list(old_rows)))
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
