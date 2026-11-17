"""Did the code that came AFTER the final run change what the agents do? The smoke test, with no judge.

    python -m eval.same_behavior --final R1.json R2.json R3.json R4.json R5.json --smoke S.json [-o SMOKE.md]

`--final` are the repetitions of the main run, `--smoke` one `run_eval --repeats 1 --no-judge` made later, on the commit that holds the work that was added
after it (webhook, SLA, observability, production). Both say which commit they came from (`commit` in their summary), and so does this report.

What it compares is the deterministic part, the checks that need no judge: routing, tool use, handoff (and memory after the restart). A single smoke run is
one sample of a model that varies from run to run, so "unchanged" is not "identical to run 3": it means the smoke's rate falls INSIDE the spread the final
run's own repetitions showed (min to max), and no case that passed a check in EVERY repetition of the final fails it in the smoke. Anything else is listed.

Rates cannot PROVE that nothing changed: a step that fires rarely hides inside the spread. So there is a second part, an identity check on the
candidate's rows, for a candidate that ran with REPLAN_MAX=0 (the observe step off, the graph as it was):
  (a) `replans` is empty or absent in EVERY row: the observe step took no action at all;
  (b) the agents that worked on each case are among the agent sets seen for that case in the final repetitions. The agents of a row are its `plan`
      (what the router chose) plus the `agent` of every tool call except `observe` (the retry search of the observe step is not an agent). This is
      how an agent added after the router shows up: the plan still says what the router chose, but the added agent's tool calls carry its name.
      "Among" is EXACT set equality with the set seen in at least one repetition of that case, not membership in the union of all the agents ever
      seen for it: with the union, a case seen once with `knowledge` and once with `support` would accept `knowledge + support`, which is exactly
      what an added agent produces. Rows with a provider error, and cases that no final repetition ran, are not compared (they are counted and listed).
      Limit: an agent that ran and made no tool call at all and is not in the plan would not be seen; the support agent always calls a tool.
If the candidate ran with another REPLAN_MAX, replans and added agents are expected and the identity check is reported as not applicable.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

CHECKS = {"routing": "routing_ok", "tools": "tools_ok", "handoff": "handoff_ok", "memory": "memory_ok"}
TOLERANCE = 1e-9


def load(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def rate(rows: list[dict], key: str) -> tuple[float | None, int]:
    vals = [r[key] for r in rows if r.get(key) is not None and not r.get("provider_error")]
    return (sum(bool(v) for v in vals) / len(vals), len(vals)) if vals else (None, 0)


def agents_used(row: dict) -> frozenset[str]:
    """The agents that worked on a case: the router's plan plus every agent that made a tool call (`observe` is a step, not an agent)."""
    return frozenset(row.get("plan") or []) | {tc["agent"] for tc in row.get("tool_calls") or [] if tc.get("agent") and tc["agent"] != "observe"}


def identity(final_runs: list[dict], smoke: dict) -> dict:
    """(a) no replans in any row; (b) every case's agents equal the agents of that case in at least one final repetition. See the module doc."""
    rows = [r for r in smoke["rows"] if not r.get("provider_error") and not r.get("error")]
    replan_max = smoke.get("summary", {}).get("replan_max")
    with_replans = [r["id"] for r in rows if r.get("replans")]
    seen: dict[str, set[frozenset[str]]] = {}
    for run in final_runs:
        for r in run["rows"]:
            if not r.get("provider_error") and not r.get("error"):
                seen.setdefault(r["id"], set()).add(agents_used(r))
    outside, not_compared = [], []
    for r in rows:
        if r["id"] not in seen:
            not_compared.append(r["id"])
        elif agents_used(r) not in seen[r["id"]]:
            outside.append((r["id"], sorted(agents_used(r)), sorted(sorted(s) for s in seen[r["id"]])))
    reference_replans = sum(1 for run in final_runs for r in run["rows"] if r.get("replans"))
    return {"applies": replan_max in (0, None), "replan_max": replan_max, "rows": len(rows),
            "replans": {"pass": not with_replans, "failing": with_replans, "reference_rows_with_replans": reference_replans},
            "agents": {"pass": not outside, "failing": outside, "compared": len(rows) - len(not_compared), "not_compared": not_compared},
            "skipped_errors": len(smoke["rows"]) - len(rows)}


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
    out["identity"] = identity(final_runs, smoke)
    ident = out["identity"]
    out["identical"] = (not ident["applies"]) or (ident["replans"]["pass"] and ident["agents"]["pass"])
    return out


def render_identity(i: dict) -> list[str]:
    if not i["applies"]:
        return [f"## Identity with REPLAN_MAX=0: NOT APPLICABLE (the candidate ran with REPLAN_MAX={i['replan_max']}: replans and added agents are expected)", ""]
    r, a = i["replans"], i["agents"]
    pf = lambda ok: "PASS" if ok else "**FAIL**"  # noqa: E731
    L = ["## Identity with REPLAN_MAX=0 (the observe step does nothing, and no agent is added)", "",
         f"- **(a) no replans in any row: {pf(r['pass'])}** ({i['rows']} rows; the final repetitions had {r['reference_rows_with_replans']} rows with replans)"
         + (f". Rows with replans: {', '.join(r['failing'])}" if r["failing"] else ""),
         f"- **(b) agents of each case among the sets seen in the final repetitions: {pf(a['pass'])}** ({a['compared']} cases compared)"
         + (". Outside: " + "; ".join(f"{cid} used {got}, seen {seen}" for cid, got, seen in a["failing"]) if a["failing"] else "")]
    if a["not_compared"]:
        L.append(f"- not compared (no final repetition ran them): {', '.join(a['not_compared'])}")
    if i["skipped_errors"]:
        L.append(f"- rows with a provider error, not compared: {i['skipped_errors']}")
    verdict = "IDENTICAL: the observe step took no action in any row and no case used an agent set the final run had not shown" if r["pass"] and a["pass"] else "NOT IDENTICAL, look at the rows above"
    return L + ["", f"**Identity verdict: {verdict}**", ""]


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
    L += render_identity(res["identity"])
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
    return 0 if res["unchanged"] and res["identical"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
