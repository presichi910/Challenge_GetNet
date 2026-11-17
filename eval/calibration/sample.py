"""Which 15 answers a human labels to measure the judge's agreement (kappa) on NEW responses.

    python -m eval.calibration.sample                        # print the sample
    python -m eval.calibration.sample -o eval/calibration/sample_v3.json
    python -m eval.calibration.sample --check eval/reports/<repetition 1>.json    # are all 15 in that run, with an answer?

The sample is chosen BEFORE any judge note exists, and from the DATASET ALONE (case ids and their kind): it cannot depend on how the judge scored
anything, so it cannot be tilted toward the cases where the judge is wrong (or right). It is STRATIFIED, with a fixed seed, so anyone reproduces it:

    4 new holdout     (ho06 and later; ho06, ho08 and ho12 are baseline seen) = ho06 and ho12 ALWAYS, plus 2 drawn from the other 6
    6 regular with tools (single-turn: the answers whose groundedness the judge can check against tool output)
    3 regular multi-turn (m01-m08: they exercise memory)
    2 regular other   (single-turn without tools: refusals, handoffs, injection)

Within a stratum the candidates are shuffled with `random.Random(f"{SEED}:{stratum}")`; the first `k` that exist are taken. The whole shuffled order
is stored too, so if a case has no usable row in the repetition (a provider error) the replacement is the NEXT in that order, decided in advance.
The sample comes from repetition 1 of the final run. It never reads a key or a judge note.
"""
from __future__ import annotations

import argparse
import json
import random
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEED = 20261117  # the delivery date: fixed, so the sample is reproducible and was not chosen after seeing anything
MANDATORY = ("ho06_", "ho12_")
QUOTAS = [("blind_holdout", 4), ("regular_with_tools", 6), ("regular_multi_turn", 3), ("regular_other", 2)]


NOTE = ("ho06 and ho12 were included ON PURPOSE as edge cases (2 of the 15), not drawn: ho06 is a gap in the knowledge base (the honest answer is nearly empty) "
        "and ho12 is an upset customer who does not ask for a person (an answer that can leave unsupported steps next to \"no documented steps\"). "
        "The other 13 rows are a seeded draw. Any figure over this sample (agreement, kappa) carries that: it is not a random sample of the 40 cases.")


def is_blind(case_id: str) -> bool:
    m = re.match(r"ho(\d+)", case_id)
    return bool(m) and int(m.group(1)) > 5


def strata(cases: list[dict]) -> dict[str, list[str]]:
    regular = [c for c in cases if not c.get("holdout")]
    return {
        "blind_holdout": sorted(c["id"] for c in cases if c.get("holdout") and is_blind(c["id"])),
        "regular_with_tools": sorted(c["id"] for c in regular if not c.get("history") and c.get("expected_tools")),
        "regular_multi_turn": sorted(c["id"] for c in regular if c.get("history")),
        "regular_other": sorted(c["id"] for c in regular if not c.get("history") and not c.get("expected_tools")),
    }


def order(stratum: str, ids: list[str], seed: int = SEED) -> list[str]:
    """The mandatory ids first (they are always in), then the rest in a seeded shuffle."""
    forced = [i for i in ids if i.startswith(MANDATORY)]
    rest = [i for i in ids if not i.startswith(MANDATORY)]
    random.Random(f"{seed}:{stratum}").shuffle(rest)
    return forced + rest


def choose(cases: list[dict], available: set[str] | None = None, seed: int = SEED) -> dict:
    out = {"seed": seed, "note": NOTE, "total": 0, "strata": {}}
    for name, k in QUOTAS:
        ordered = order(name, strata(cases)[name], seed)
        pool = [i for i in ordered if available is None or i in available]
        out["strata"][name] = {"quota": k, "chosen": pool[:k], "reserve_in_order": pool[k:], "candidates": len(ordered)}
        out["total"] += len(pool[:k])
    out["ids"] = [i for s in out["strata"].values() for i in s["chosen"]]
    return out


def check(sample: dict, run_path: str) -> dict:
    rows = json.loads(Path(run_path).read_text(encoding="utf-8"))["rows"]
    usable = {r["id"] for r in rows if r.get("answer") and not r.get("provider_error") and not r.get("error")}
    missing = [i for i in sample["ids"] if i not in usable]
    return {"usable": len(usable), "missing": missing, "replacement": choose(load_cases(), usable)["ids"] if missing else sample["ids"]}


def load_cases(path: Path = ROOT / "dataset.jsonl") -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("-o", "--out")
    ap.add_argument("--check", metavar="RUN_JSON", help="verify the sample against repetition 1 (every id has a usable answer)")
    args = ap.parse_args(argv)
    sample = choose(load_cases())
    if args.out:
        Path(args.out).write_text(json.dumps(sample, indent=2), encoding="utf-8")
    print(json.dumps(sample, indent=2))
    if args.check:
        res = check(sample, args.check)
        print("\nmissing from the run:", res["missing"] or "none")
        if res["missing"]:
            print("sample with the pre-decided replacements:", res["replacement"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
