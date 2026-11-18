"""V3b: 15 MORE judged answers for a human to label, to double the sample the judge's agreement is measured on (V3 has 15).

    python -m eval.calibration.sample_v3b                                        # print the sample
    python -m eval.calibration.sample_v3b -o eval/calibration/sample_v3b.json    # and save the manifest
    python -m eval.calibration.sample_v3b --export --blind eval/calibration/labels_v3b_blind.csv --key eval/calibration/labels_v3b_key.csv

THE RULE (written and committed BEFORE the sample was drawn; see docs/V3B_PREREG.md):

  * Source: repetition 2 of the final run, `eval/reports/20261115-202732.judged.json` (commit 22423e2, REPLAN_MAX=0). V3 used repetition 1.
    Every row of it already has a verdict of the judge, so the judge saw these answers; the human will not see the verdict.
  * Excluded: the 15 CASES of V3 (`sample_v3.json`), in any repetition, so the 30 rows of V3 and V3b are 30 different cases.
  * Strata: the same four of V3 (`eval.calibration.sample.strata`): blind holdout (ho06 and later), regular with tools, regular multi-turn, regular
    other. The five holdout cases ho01-ho05 were seen during development and are in no stratum, as in V3.
  * Quotas: 3 + 4 + 4 + 4 = 15. They are the 15/20 share of what is left in each stratum (4, 6, 5 and 5 cases), rounded by the largest remainder.
    No case is forced in: V3 forced ho06 and ho12 on purpose; V3b is a plain seeded draw.
  * Seed: 20261118 (the date of the decision). Within a stratum, the candidates (sorted, minus the V3 cases) are shuffled with
    `random.Random(f"{SEED}:{stratum}")`; the first `k` that have a usable row are taken, and the rest of the shuffled order is the reserve: if a chosen
    case has no judged row in the source, the replacement is the NEXT in that order.
  * "Usable" means the row exists with a verdict of `pass` or `fail`. Which of the two it is never enters the choice.

The sample is a function of the dataset's structure (kind of case), of V3's ids and of WHICH rows exist. It never reads a verdict, a score, a note or a key.
The sheet a human fills in has an opaque id per row (the order is a seeded shuffle), the question, the case's expected behavior, the evidence and the answer:
no verdict, no score, no case id, no repetition, no replan setting, no holdout flag. The key (the judge columns plus the real case id) is written by this
script and never printed; its SHA-256 goes in the manifest, so anybody can see that it was not edited while the labels were being made.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
from pathlib import Path

from eval.calibration.export import BLIND_COLUMNS, COLUMNS, load_criteria, to_label_rows, write_csv
from eval.calibration.sample import load_cases, strata

ROOT = Path(__file__).resolve().parent.parent
SEED = 20261118
SOURCE = "eval/reports/20261115-202732.judged.json"
V3_MANIFEST = Path(__file__).resolve().parent / "sample_v3.json"
QUOTAS = [("blind_holdout", 3), ("regular_with_tools", 4), ("regular_multi_turn", 4), ("regular_other", 4)]
KEY_COLUMNS = ["id", "case_id", "source", *COLUMNS[1:]]
NOTE = ("V3b is a plain seeded draw (no case forced in) over the 25 cases V3 did not use, from repetition 2 of the final run. Its 15 rows already had a verdict "
        "of the judge, and the human who labels them does not see it. Any figure over V3b or over V3 + V3b is over 15 or 30 rows of one run, not over the 40 cases.")


def v3_ids(path: Path = V3_MANIFEST) -> list[str]:
    return json.loads(path.read_text(encoding="utf-8"))["ids"]


def order(stratum: str, ids: list[str], excluded: set[str], seed: int = SEED) -> list[str]:
    """The stratum's candidates without the V3 cases, in a seeded shuffle."""
    rest = sorted(i for i in ids if i not in excluded)
    random.Random(f"{seed}:{stratum}").shuffle(rest)
    return rest


def choose(cases: list[dict], excluded: set[str], available: set[str] | None = None, seed: int = SEED) -> dict:
    out = {"seed": seed, "source": SOURCE, "excluded_v3": sorted(excluded), "note": NOTE, "total": 0, "strata": {}}
    pools = strata(cases)
    for name, k in QUOTAS:
        ordered = order(name, pools[name], excluded, seed)
        pool = [i for i in ordered if available is None or i in available]
        out["strata"][name] = {"quota": k, "chosen": pool[:k], "reserve_in_order": pool[k:], "candidates": len(ordered)}
        out["total"] += len(pool[:k])
    out["ids"] = [i for s in out["strata"].values() for i in s["chosen"]]
    return out


def available_rows(run_path: str | Path, criteria: dict[str, str] | None = None) -> dict[str, dict]:
    """id -> label row, for the rows of the run that have a verdict of pass or fail (the same rows `export` writes)."""
    report = json.loads(Path(run_path).read_text(encoding="utf-8"))
    return {r["id"]: r for r in to_label_rows(report, criteria or {}) if r["judge_verdict"] in ("pass", "fail")}


def sheet_order(ids: list[str], seed: int = SEED) -> list[str]:
    shuffled = sorted(ids)
    random.Random(f"{seed}:sheet-order").shuffle(shuffled)
    return shuffled


def build_sheets(ids: list[str], rows: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    """(blind rows, key rows). The blind row has an opaque id and nothing of the judge; the key row has the same id, the real case id and the judge."""
    blind, key = [], []
    for n, case_id in enumerate(sheet_order(ids), start=1):
        r, opaque = rows[case_id], f"v3b-{n:02d}"
        blind.append({"id": opaque, "question": r["question"], "criteria": r["criteria"] or "", "answer": r["answer"], "evidence": r["evidence"],
                      **{c: "" for c in BLIND_COLUMNS if c.startswith("human_") or c == "notes"}})
        key.append({**r, "id": opaque, "case_id": case_id, "source": SOURCE})
    return blind, key


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("-o", "--out", help="save the manifest here")
    ap.add_argument("--export", action="store_true", help="write the blind sheet and the sealed key (needs --blind and --key)")
    ap.add_argument("--blind")
    ap.add_argument("--key")
    args = ap.parse_args(argv)
    cases = load_cases()
    rows = available_rows(ROOT.parent / SOURCE, load_criteria())
    sample = choose(cases, set(v3_ids()), set(rows))
    if args.export:
        if not (args.blind and args.key):
            ap.error("--export needs --blind and --key")
        blind, key = build_sheets(sample["ids"], rows)
        write_csv(blind, Path(args.blind), ["id", "question", "criteria", "answer", "evidence", "human_grounded", "human_complete", "human_pass", "notes"])
        write_csv(key, Path(args.key), KEY_COLUMNS)
        sample["blind_sheet"] = args.blind
        sample["key"] = args.key
        sample["key_sha256"] = sha256(Path(args.key))
        sample["blind_sha256"] = sha256(Path(args.blind))
        sample["sheet_rows"] = len(blind)
    if args.out:
        Path(args.out).write_text(json.dumps(sample, indent=2), encoding="utf-8")
    shown = {k: v for k, v in sample.items() if k != "note"}
    print(json.dumps(shown, indent=2))
    return 0


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


if __name__ == "__main__":
    raise SystemExit(main())
