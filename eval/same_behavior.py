"""Did the code that came AFTER the final run change what the agents do? The smoke test, with no judge.

    python -m eval.same_behavior --final R1.json R2.json R3.json R4.json R5.json --smoke S.json [-o SMOKE.md]

`--final` are the repetitions of the main run, `--smoke` one `run_eval --repeats 1 --no-judge` made later, on the commit that holds the work that was added
after it (webhook, SLA, observability, production). Both say which commit they came from (`commit` in their summary), and so does this report.

What it compares is the deterministic part, the checks that need no judge: routing, tool use, handoff (and memory after the restart). A single smoke run is
one sample of a model that varies from run to run, so "unchanged" is not "identical to run 3": it means the smoke's rate falls INSIDE the spread the final
run's own repetitions showed (min to max), and no case that passed a check in EVERY repetition of the final fails it in the smoke. Anything else is listed.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from eval.run_eval import summarize_all

CHECKS = {"routing": "routing_ok", "tools": "tools_ok", "handoff": "handoff_ok", "memory": "memory_ok"}
TOLERANCE = 1e-9


def load(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def rate(rows: list[dict], key: str) -> tuple[float | None, int]:
    vals = [r[key] for r in rows if r.get(key) is not None and not r.get("provider_error")]
    return (sum(bool(v) for v in vals) / len(vals), len(vals)) if vals else (None, 0)


def compare(final_runs: list[dict], smoke: dict) -> dict:
    out = {"checks": {}, "suspects": [], "improved": [], "commits": {"final": sorted({r.get("summary", {}).get("commit", "unknown") for r in final_runs}),
                                                                      "smoke": smoke.get("summary", {}).get("commit", "unknown")},
           "smoke_tree_clean": smoke.get("summary", {}).get("tree_clean"), "final_tree_clean": [r.get("summary", {}).get("tree_clean") for r in final_runs]}
    for name, key in CHECKS.items():
        per_run = [rate(r["rows"], key) for r in final_runs]
        have = [v for v, _ in per_run if v is not None]
        s, n = rate(smoke["rows"], key)
        if not have or s is None:
            continue
        lo, hi = min(have), max(have)
        out["checks"][name] = {"final_min": lo, "final_max": hi, "final_mean": sum(have) / len(have), "smoke": s, "n_smoke": n,
                               "inside": lo - TOLERANCE <= s <= hi + TOLERANCE}
    by_id = {}
    for run in final_runs:
        for r in run["rows"]:
            by_id.setdefault(r["id"], []).append(r)
    for r in smoke["rows"]:
        past = by_id.get(r["id"], [])
        for name, key in CHECKS.items():
            if r.get(key) is None or not past or any(p.get(key) is None for p in past):
                continue
            if all(p[key] for p in past) and not r[key]:
                out["suspects"].append((r["id"], name))
            if not any(p[key] for p in past) and r[key]:
                out["improved"].append((r["id"], name))
    out["unchanged"] = all(c["inside"] for c in out["checks"].values()) and not out["suspects"]
    return out


def render(res: dict, final_files: list[str], smoke_file: str) -> str:
    c = res["commits"]
    L = ["# Did the later work change the behavior? (smoke test, no judge)", "",
         f"- **Final run** (`{', '.join(Path(f).name for f in final_files)}`): commit `{', '.join(x[:12] for x in c['final'])}`, working tree clean: {res['final_tree_clean']}",
         f"- **Smoke run** (`{Path(smoke_file).name}`): commit `{c['smoke'][:12]}`, working tree clean: {res['smoke_tree_clean']}",
         "", "_Unchanged = the smoke's rate is inside the spread of the final run's own repetitions, and no case that passed a check in every repetition of the final run fails it here._", "",
         "| check | final min | final max | final mean | smoke (n) | inside the spread |", "|---|---|---|---|---|---|"]
    for name, v in res["checks"].items():
        L.append(f"| {name} | {v['final_min'] * 100:.0f}% | {v['final_max'] * 100:.0f}% | {v['final_mean'] * 100:.0f}% | {v['smoke'] * 100:.0f}% ({v['n_smoke']}) | {'yes' if v['inside'] else '**NO**'} |")
    L += ["", f"**Suspects (passed in every final repetition, fail in the smoke):** {', '.join(f'{i} ({n})' for i, n in res['suspects']) or 'none'}",
          f"**Improved (failed in every final repetition, pass in the smoke):** {', '.join(f'{i} ({n})' for i, n in res['improved']) or 'none'}", "",
          f"## Verdict: {'NO CHANGE in routing, tools and handoff' if res['unchanged'] else 'CHANGED, look at the rows above'}", ""]
    if c["smoke"] in c["final"]:
        L += ["_The smoke ran on the SAME commit as the final run: it shows the run-to-run variation, not the effect of any later work._", ""]
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--final", nargs="+", required=True)
    ap.add_argument("--smoke", required=True)
    ap.add_argument("-o", "--out")
    args = ap.parse_args(argv)
    res = compare([load(f) for f in args.final], load(args.smoke))
    text = render(res, args.final, args.smoke)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    return 0 if res["unchanged"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
