"""The verifier ALONE: the same drafts, two checkers, one judge.

    python -m eval.verifier_ab                           # capture drafts of the regular cases, compare, judge
    python -m eval.verifier_ab --drafts eval/reports/VERIFIER_AB-<stamp>-drafts.json     # reuse drafts

Why drafts: a whole-system run changes everything at once (the agents are not deterministic), so two runs cannot
say anything about the verifier. Here the agents run ONCE with the verify step replaced by a recorder; each draft
(answer + evidence + conversation) is then given to the checker "statements" (the model lists what it finds) and to
the checker "segments" (the code splits the answer, the model must answer for every segment), several times each,
and one run of each is scored by the judge together with the unverified draft. The judge sees the evidence, so the
number that matters is how many unsupported claims are LEFT in what the customer would read.

Regular cases only: the holdout is never used (it only measures, and a verifier change is not what it measures).
The judge is paced to its tokens-per-minute limit; a judge error is retried after a wait and is never counted as a result.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage

from eval.run_eval import ROOT, judge_question, load_cases, run_case

MODES = ("statements", "segments")
JUDGE_GAP_SECONDS = 16  # the judge's limit is 8,000 tokens/min and a call is ~1.9K


def snapshot(state: dict) -> dict:
    """What the verify node would see, as plain data."""
    return {"final_answer": state.get("final_answer", ""), "language": state.get("language", "English"),
            "agent_outputs": state.get("agent_outputs") or {},
            "messages": [{"type": m.type, "content": str(m.content), "id": m.id} for m in state.get("messages") or []]}


def rebuild(snap: dict) -> dict:
    """The state for a verify node, from a snapshot."""
    kinds = {"human": HumanMessage, "ai": AIMessage}
    return {**snap, "messages": [kinds[m["type"]](content=m["content"], id=m["id"]) for m in snap["messages"] if m["type"] in kinds]}


def has_evidence(snap: dict) -> bool:
    return any(o.get("contexts") for o in snap["agent_outputs"].values())


def capture(cases: list[dict], pause: float) -> dict[str, dict]:
    """Run the cases through the real graph with the verify step replaced by a recorder."""
    from app.graph import build_default_graph
    from app.service import ChatService

    seen: dict[str, dict] = {}
    current = {"id": ""}

    def recorder(state: dict) -> dict:
        seen[current["id"]] = snapshot(state)
        return {"verify_note": ""}

    service = ChatService(build_default_graph(verify_node=recorder))
    stamp = datetime.now().strftime("%H%M%S")
    for c in cases:
        current["id"] = c["id"]
        state, _, exc = run_case(service, c, f"ab-{stamp}-{c['id']}", 2, time.sleep)
        print(f"   draft {c['id']}: {'ok' if c['id'] in seen else 'no draft (' + str(exc or 'handoff') + ')'}")
        time.sleep(pause)
    return seen


def parse_note(note: str) -> int:
    m = re.match(r"revised:(\d+)", note or "")
    return int(m.group(1)) if m else 0


def run_checker(snap: dict, mode: str) -> dict:
    from app.agents.verifier import make_verify_node

    update = make_verify_node(keep_raw=True, mode=mode)(rebuild(snap))
    raw = json.loads(update["verify_raw"]) if update.get("verify_raw") else {}
    listed = len(raw.get("segments") or raw.get("statements") or [])
    return {"mode": mode, "final": update.get("final_answer", snap["final_answer"]), "note": update.get("verify_note", ""),
            "flagged": parse_note(update.get("verify_note", "")), "listed": listed, "omitted": len(raw.get("omitted", [])),
            "error": raw.get("error")}


def judged(judge, case: dict, answer: str, snap: dict, sleep=time.sleep) -> dict:
    """The judge's verdict, paced, and retried after a wait if the judge itself failed (that is not a result)."""
    evidence = [c for o in snap["agent_outputs"].values() for c in o.get("contexts", [])]
    for attempt in range(3):
        sleep(JUDGE_GAP_SECONDS if attempt == 0 else 45)
        out = judge.score(judge_question(case), answer, evidence, case.get("criteria", ""))
        if out.get("verdict") != "error":
            return out
    return out


def claims(j: dict) -> tuple[int, int]:
    """(unsupported claims, of them actionable) in a judge result."""
    lst = j.get("unsupported_claims") or []
    return len(lst), sum(1 for c in lst if c.get("actionable"))


def summarize(rows: list[dict]) -> dict:
    """Totals over the cases that were judged on all three texts (draft, statements, segments)."""
    done = [r for r in rows if r.get("judge")]
    out = {"cases_with_evidence": len(rows), "cases_judged": len(done)}
    for name in ("draft", "statements", "segments"):
        js = [r["judge"][name] for r in done if r["judge"].get(name) and r["judge"][name].get("verdict") != "error"]
        c = [claims(j) for j in js]
        out[name] = {"judged": len(js), "unsupported": sum(a for a, _ in c), "actionable": sum(b for _, b in c),
                     "cases_clean": sum(1 for a, _ in c if a == 0), "pass": sum(1 for j in js if j.get("verdict") == "pass"),
                     "completeness_mean": round(statistics.mean(j["completeness"] for j in js), 2) if js else None,
                     "groundedness_mean": round(statistics.mean(j["groundedness"] for j in js), 2) if js else None}
    for mode in MODES:
        runs = [x for r in rows for x in r["runs"][mode] if not x["error"]]
        out[f"{mode}_checker"] = {"runs": len(runs), "listed_mean": round(statistics.mean(x["listed"] for x in runs), 2) if runs else None,
                                  "flagged_mean": round(statistics.mean(x["flagged"] for x in runs), 2) if runs else None,
                                  "omitted_total": sum(x["omitted"] for x in runs),
                                  "errors": sum(1 for r in rows for x in r["runs"][mode] if x["error"])}
    return out


def render(rows: list[dict], summary: dict, stamp: str, repeats: int) -> str:
    L = [f"# Verifier A/B ({stamp})", "",
         f"_Same drafts, two checkers, {repeats} runs each; the judge scores ONE run of each next to the unverified draft. "
         "Regular cases with evidence only (the others are not touched by a verifier)._", "",
         "## Unsupported claims the judge finds in what the customer would read", "",
         "| case | segments | draft | statements | segments | statements pass | segments pass |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        j = r.get("judge") or {}
        cell = lambda n: (lambda c: f"{c[0]} ({c[1]} actionable)")(claims(j[n])) if j.get(n) and j[n].get("verdict") != "error" else "judge error"
        ok = lambda n: j[n]["verdict"] if j.get(n) else "-"
        L.append(f"| {r['id'][:28]} | {r['segments']} | {cell('draft')} | {cell('statements')} | {cell('segments')} | {ok('statements')} | {ok('segments')} |")
    L += ["", "## Totals", "", "| text | judged | unsupported claims | actionable | answers with none | judge pass | groundedness | completeness |",
          "|---|---|---|---|---|---|---|---|"]
    for name in ("draft", "statements", "segments"):
        s = summary[name]
        L.append(f"| {name} | {s['judged']} | {s['unsupported']} | {s['actionable']} | {s['cases_clean']} | {s['pass']} | {s['groundedness_mean']} | {s['completeness_mean']} |")
    L += ["", "## What each checker did (all runs)", "", "| checker | runs | entries it returned per answer | segments/statements it flagged per answer | omitted segments | errors |", "|---|---|---|---|---|---|"]
    for mode in MODES:
        s = summary[f"{mode}_checker"]
        L.append(f"| {mode} | {s['runs']} | {s['listed_mean']} | {s['flagged_mean']} | {s['omitted_total']} | {s['errors']} |")
    L += ["", "## Spread across the repeated runs (flagged per answer: min / max)", "", "| case | statements | segments |", "|---|---|---|"]
    for r in rows:
        f = {m: [x["flagged"] for x in r["runs"][m]] for m in MODES}
        L.append(f"| {r['id'][:28]} | {min(f['statements'])} / {max(f['statements'])} | {min(f['segments'])} / {max(f['segments'])} |")
    return "\n".join(L) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--drafts", help="reuse the drafts of an earlier run")
    ap.add_argument("--repeats", type=int, default=3, help="runs of each checker per draft")
    ap.add_argument("--pause", type=float, default=3.0, help="seconds between cases while capturing")
    ap.add_argument("--no-judge", action="store_true")
    args = ap.parse_args()

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = ROOT / "reports"
    out_dir.mkdir(exist_ok=True)
    regular = [c for c in load_cases(None) if not c.get("holdout")]
    if args.drafts:
        drafts = json.loads(Path(args.drafts).read_text(encoding="utf-8"))
    else:
        print(f"capturing drafts of {len(regular)} regular cases...")
        drafts = capture(regular, args.pause)
        (out_dir / f"VERIFIER_AB-{stamp}-drafts.json").write_text(json.dumps(drafts, ensure_ascii=False, indent=1), encoding="utf-8")
    cases = {c["id"]: c for c in regular}
    todo = [(cid, snap) for cid, snap in drafts.items() if cid in cases and has_evidence(snap) and snap["final_answer"].strip()]
    print(f"{len(todo)} drafts with evidence; running both checkers x{args.repeats}...")

    from app.segments import split_segments

    def work(item):
        cid, snap = item
        return {"id": cid, "segments": len(split_segments(snap["final_answer"]) or []),
                "runs": {m: [run_checker(snap, m) for _ in range(args.repeats)] for m in MODES}}

    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(work, todo))

    if not args.no_judge:
        from eval.judge import Judge

        judge = Judge()
        for r in rows:
            snap, case = drafts[r["id"]], cases[r["id"]]
            print(f"   judging {r['id']}...")
            r["judge"] = {"draft": judged(judge, case, snap["final_answer"], snap),
                          "statements": judged(judge, case, r["runs"]["statements"][0]["final"], snap),
                          "segments": judged(judge, case, r["runs"]["segments"][0]["final"], snap)}
    summary = summarize(rows) if not args.no_judge else {}
    (out_dir / f"VERIFIER_AB-{stamp}.json").write_text(json.dumps({"rows": rows, "summary": summary}, ensure_ascii=False, indent=1), encoding="utf-8")
    if not args.no_judge:
        text = render(rows, summary, stamp, args.repeats)
        (out_dir / f"VERIFIER_AB-{stamp}.md").write_text(text, encoding="utf-8")
        print("\n" + text)
    print(f"written: VERIFIER_AB-{stamp}.json / .md")


if __name__ == "__main__":
    main()
