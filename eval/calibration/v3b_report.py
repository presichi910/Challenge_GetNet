"""The result of V3b (docs/V3B_PREREG.md): the agreement of the judge with a human on the 15 new rows alone and on the 30 rows of V3 and V3b together.

    python -m eval.calibration.v3b_report -o eval/calibration/CALIBRATION_V3b.md

It reads the filled blind sheets and the keys (the judge's columns), refuses to run if the V3b key is not the one that was sealed (its SHA-256 is in `sample_v3b.json`), and
writes the Markdown report. For each of the two sets: the number of labeled rows, the pass/fail agreement with its Wilson 95% interval, Cohen's kappa (a point value: with 15
or 30 rows an interval would suggest more than the sample can say), the 2 x 2 table, the mean absolute error of groundedness and completeness, and the disagreements.
Everything is computed from the files by `eval/calibration/agreement.py` and the Wilson formula below; nothing is typed by hand. Pure Python, no network.
"""
from __future__ import annotations

import argparse
import csv
import json
from math import sqrt
from pathlib import Path

from eval.calibration.agreement import agreement, merge_judge
from eval.calibration.sample_v3b import sha256

CAL = Path(__file__).resolve().parent
Z95 = 1.959963984540054


def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float] | None:
    """The Wilson score interval of a proportion k / n. None when n is 0."""
    if n <= 0:
        return None
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def read(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def joined(blind: Path, key: Path, label: str) -> list[dict]:
    """The filled sheet plus the judge's columns of its key, joined by id; `label` is kept so a disagreement can be named."""
    keyed = read(key)
    case_of = {r["id"]: r.get("case_id") or r["id"] for r in keyed}
    return [{**r, "case": case_of.get(r["id"], r["id"]), "set": label} for r in merge_judge(read(blind), keyed)]


def summary(rows: list[dict]) -> dict:
    res = agreement(rows)
    k = res["confusion"]["tp"] + res["confusion"]["tn"]
    res["agree_k"], res["agree_n"] = k, res["labeled"]
    res["wilson"] = wilson(k, res["labeled"])
    res["disagreement_cases"] = [r["case"] for r in rows if r["id"] in set(res["disagreements"])]
    return res


def section(title: str, rows: list[dict]) -> list[str]:
    s = summary(rows)
    c = s["confusion"]

    def f(v, pct=False):
        return "n/a" if v is None else (f"{v * 100:.0f}%" if pct else f"{v:.2f}")

    ci = s["wilson"]
    return [f"## {title}", "",
            f"- labeled rows: {s['labeled']} of {s['rows']}",
            f"- pass/fail agreement: {s['agree_k']} of {s['agree_n']} = {f(s['pass_agreement'], pct=True)}; Wilson 95% interval "
            + ("n/a" if ci is None else f"{ci[0] * 100:.0f}% to {ci[1] * 100:.0f}%"),
            f"- Cohen's kappa: {f(s['cohen_kappa'])} (a point value, no interval)",
            f"- MAE groundedness (1-5): {f(s['mae_grounded'])} over {s['n_grounded']} rows",
            f"- MAE completeness (1-5): {f(s['mae_complete'])} over {s['n_complete']} rows", "",
            "| judge \\ human | pass | fail |", "|---|---|---|", f"| pass | {c['tp']} | {c['fp']} |", f"| fail | {c['fn']} | {c['tn']} |", "",
            "Disagreements: " + (", ".join(f"`{x}`" for x in s["disagreement_cases"]) or "none"), ""]


def report(v3: list[dict], v3b: list[dict]) -> str:
    out = ["# Judge calibration V3b: agreement with a human", "",
           "_The 15 rows of V3b (`labels_v3b_blind.csv`, key `labels_v3b_key.csv`) alone, and the 30 rows of V3 and V3b together. "
           "The rule that drew them and what the human did and did not see are in `docs/V3B_PREREG.md`._", ""]
    out += section("V3b alone (15 new cases)", v3b)
    out += section("V3 and V3b together (30 rows of 30 cases)", [*v3, *v3b])
    out += ["## What this is and is not", "",
            "- **All the rows already had a verdict of the judge** when they were drawn, and the human did not see it. V3b measures the agreement of the human with a verdict that existed; "
            "it is not a measurement made before the judge ran.",
            "- The 15 rows are a seeded draw of one repetition of one run over 25 cases, and V3 is a draw of 15 over the other cases (with `ho06` and `ho12` forced in): the 30 are not "
            "a random sample of the 40 cases or of the answers the system gives.",
            "- With 15 or 30 rows the interval is wide whatever the agreement is; the kappa is given without one on purpose.", ""]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--v3-blind", default=str(CAL / "labels_v3_blind.csv"))
    ap.add_argument("--v3-key", default=str(CAL / "labels_v3_key.csv"))
    ap.add_argument("--v3b-blind", default=str(CAL / "labels_v3b_blind.csv"))
    ap.add_argument("--v3b-key", default=str(CAL / "labels_v3b_key.csv"))
    ap.add_argument("--manifest", default=str(CAL / "sample_v3b.json"))
    args = ap.parse_args(argv)
    sealed = json.loads(Path(args.manifest).read_text(encoding="utf-8"))["key_sha256"]
    if sha256(Path(args.v3b_key)) != sealed:
        raise SystemExit("v3b_report: the V3b key is not the one that was sealed (its SHA-256 differs from the manifest): the key was edited after the draw")
    text = report(joined(Path(args.v3_blind), Path(args.v3_key), "V3"), joined(Path(args.v3b_blind), Path(args.v3b_key), "V3b"))
    Path(args.out).write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
