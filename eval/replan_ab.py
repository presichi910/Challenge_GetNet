"""REPLAN_MAX 0 against 2, on the SAME drafts, with the judge as a separate, resumable step.

    python -m eval.replan_ab run --drafts eval/reports/VERIFIER_AB-<stamp>-drafts.json --capture 1
    python -m eval.replan_ab judge --file eval/reports/REPLAN_AB-<stamp>.json
    python -m eval.replan_ab report --file eval/reports/REPLAN_AB-<stamp>.json

Why this shape. (1) Two whole-system runs differ because the agents are not deterministic, so they cannot say what the
observe step changed. Here the agents run once per repetition (the drafts), and each draft goes through both arms:
  arm 0  the verify node alone (what runs today)           arm 2  the observe step with REPLAN_MAX=2, then the verify node
(2) The judge has a DAILY token limit (Groq, 200,000 tokens for the judge model) that the agents do not have. So `run`
spends no judge tokens, and `judge` scores only the distinct texts (an answer the observe step left alone is scored once),
saves after every call, and stops cleanly when the limit is reached: run it again later and it resumes.
Regular cases only: the holdout is never used here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import time
from datetime import datetime
from pathlib import Path

from langgraph.graph.message import add_messages

from eval.run_eval import ROOT, judge_question, load_cases
from eval.verifier_ab import JUDGE_GAP_SECONDS, capture, claims, has_evidence, rebuild

ESCALATED = "<escalated to a human>"


def text_key(answer: str, evidence: list[str]) -> str:
    """What was judged: the answer AND the evidence the judge saw (an answer can become backed by new evidence)."""
    return hashlib.sha256(json.dumps([answer, evidence], ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def apply_update(state: dict, update: dict) -> dict:
    """What LangGraph does with a node's return value (messages are merged, the rest replaced)."""
    merged = {**state, **{k: v for k, v in update.items() if k != "messages"}}
    if "messages" in update:
        merged["messages"] = add_messages(state["messages"], update["messages"])
    return merged


def run_arms(snap: dict, observe_node, verify_node) -> dict:
    """Both arms for one draft. Returns {"0": {...}, "2": {...}}."""
    out = {}
    state0 = rebuild(snap)
    t0 = time.perf_counter()
    u0 = verify_node(state0)
    out["0"] = {"final": u0.get("final_answer", snap["final_answer"]), "note": u0.get("verify_note", ""), "evidence_extra": [],
                "seconds": round(time.perf_counter() - t0, 1)}
    state2 = rebuild(snap)
    t0 = time.perf_counter()
    uo = observe_node(state2)
    observed = uo.get("observe") or {}
    if observed.get("escalate"):
        out["2"] = {"final": ESCALATED, "note": uo.get("verify_note", ""), "replans": observed.get("replans", []), "escalated": True,
                    "evidence_extra": observed.get("evidence", []), "seconds": round(time.perf_counter() - t0, 1)}
        return out
    merged = apply_update(state2, uo)
    u2 = verify_node(merged)
    final = u2.get("final_answer", merged["final_answer"])
    note = u2.get("verify_note", "")  # the verify node already carries the observe step's note (and adds a translation, if any)
    out["2"] = {"final": final, "note": note, "replans": observed.get("replans", []), "escalated": False,
                "evidence_extra": observed.get("evidence", []), "seconds": round(time.perf_counter() - t0, 1)}
    return out


def judge_jobs(row: dict) -> dict[str, tuple[str, list[str]]]:
    """key -> (answer, evidence) for every distinct thing the judge must score in this row."""
    base = [c for o in row["draft"]["agent_outputs"].values() for c in o.get("contexts", [])]
    jobs = {}
    for arm in ("0", "2"):
        a = row["arms"][arm]
        if a.get("escalated"):
            continue
        ev = base + list(a.get("evidence_extra", []))
        jobs[text_key(a["final"], ev)] = (a["final"], ev)
    return jobs


def summarize(rows: list[dict]) -> dict:
    """Per arm over the rows where the arm's text was judged (and was not escalated)."""
    out = {"drafts": len(rows)}
    for arm in ("0", "2"):
        scored = []
        for r in rows:
            a = r["arms"][arm]
            if a.get("escalated"):
                continue
            j = (r.get("judged") or {}).get(text_key(a["final"], [c for o in r["draft"]["agent_outputs"].values() for c in o.get("contexts", [])] + list(a.get("evidence_extra", []))))
            if j and j.get("verdict") != "error":
                scored.append(j)
        c = [claims(j) for j in scored]
        out[arm] = {"judged": len(scored), "unsupported": sum(a for a, _ in c), "actionable": sum(b for _, b in c),
                    "pass": sum(1 for j in scored if j.get("verdict") == "pass"),
                    "completeness": round(statistics.mean(j["completeness"] for j in scored), 2) if scored else None,
                    "groundedness": round(statistics.mean(j["groundedness"] for j in scored), 2) if scored else None,
                    "escalated": sum(1 for r in rows if r["arms"][arm].get("escalated"))}
    two = [r["arms"]["2"] for r in rows]
    out["observe"] = {"retried": sum(1 for a in two if any(x.startswith("retry_kb:") for x in a.get("replans", []))),
                      "retry_failed": sum(1 for a in two if any(x.startswith("retry_kb_failed") for x in a.get("replans", []))),
                      "offered_ticket": sum(1 for a in two if any(x.startswith("offer_ticket") for x in a.get("replans", []))),
                      "escalated": sum(1 for a in two if a.get("escalated")),
                      "text_changed": sum(1 for r in rows if r["arms"]["2"]["final"] != r["arms"]["0"]["final"]),
                      "seconds_arm0": round(statistics.mean(r["arms"]["0"]["seconds"] for r in rows), 1) if rows else None,
                      "seconds_arm2": round(statistics.mean(a["seconds"] for a in two), 1) if rows else None}
    return out


def render(rows: list[dict], summary: dict, stamp: str) -> str:
    L = [f"# REPLAN_MAX 0 vs 2 ({stamp})", "",
         "_The same drafts through the verify node alone (0) and through the observe step then the verify node (2). "
         "Regular cases with evidence only; the judge scores each distinct text once._", "",
         "| draft | arm 0: unsupported (actionable) | arm 2: unsupported (actionable) | what observe did |", "|---|---|---|---|"]

    def cell(r, arm):
        a = r["arms"][arm]
        if a.get("escalated"):
            return "escalated"
        ev = [c for o in r["draft"]["agent_outputs"].values() for c in o.get("contexts", [])] + list(a.get("evidence_extra", []))
        j = (r.get("judged") or {}).get(text_key(a["final"], ev))
        return "not judged yet" if not j else ("judge error" if j.get("verdict") == "error" else "%d (%d) %s" % (*claims(j), j["verdict"]))

    for r in rows:
        L.append(f"| {r['id'][:26]} #{r['repeat']} | {cell(r, '0')} | {cell(r, '2')} | {'; '.join(r['arms']['2'].get('replans', [])) or '-'} |")
    L += ["", "## The three figures", "", "| arm | judged | unsupported claims | **actionable** | **completeness** | **pass** | groundedness | escalated |",
          "|---|---|---|---|---|---|---|---|"]
    for arm in ("0", "2"):
        s = summary[arm]
        L.append(f"| REPLAN_MAX={arm} | {s['judged']} | {s['unsupported']} | **{s['actionable']}** | **{s['completeness']}** | **{s['pass']}/{s['judged']}** | {s['groundedness']} | {s['escalated']} |")
    o = summary["observe"]
    L += ["", f"observe: retried the knowledge base in {o['retried']} of {summary['drafts']} drafts ({o['retry_failed']} failed), offered a ticket {o['offered_ticket']}, "
          f"escalated {o['escalated']}, changed the text of {o['text_changed']}; seconds per draft: arm 0 {o['seconds_arm0']}, arm 2 {o['seconds_arm2']}.", ""]
    return "\n".join(L)


def load(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save(path: str, data: dict) -> None:
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def cmd_run(args) -> None:
    from app.agents.observe import make_observe_node
    from app.agents.verifier import make_verify_node

    regular = {c["id"]: c for c in load_cases(None, regular_only=True)}
    sets = []  # (repeat label, drafts)
    for i, path in enumerate(args.drafts or [], 1):
        sets.append((f"saved{i}", json.loads(Path(path).read_text(encoding="utf-8"))))
    for path in args.reuse or []:  # the drafts inside an earlier REPLAN_AB file, grouped by their repeat
        grouped: dict[str, dict] = {}
        for r in load(path)["rows"]:
            grouped.setdefault(r["repeat"], {})[r["id"]] = r["draft"]
        sets.extend(grouped.items())
    for i in range(args.capture):
        print(f"capturing a fresh set of drafts ({i + 1}/{args.capture})...", flush=True)
        sets.append((f"new{i + 1}", capture(list(regular.values()), args.pause)))
    observe_node, verify_node = make_observe_node(replan_max=2), make_verify_node()
    rows = []
    for label, drafts in sets:
        for cid, snap in drafts.items():
            if cid in regular and has_evidence(snap) and snap["final_answer"].strip():
                print(f"   {label} {cid}", flush=True)
                rows.append({"id": cid, "repeat": label, "draft": snap, "arms": run_arms(snap, observe_node, verify_node)})
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = ROOT / "reports" / f"REPLAN_AB-{stamp}.json"
    path.parent.mkdir(exist_ok=True)
    save(str(path), {"rows": rows, "judged_at": None})
    print(f"written: {path}")


def cmd_judge(args) -> None:
    from eval.judge import Judge

    data = load(args.file)
    cases = {c["id"]: c for c in load_cases(None, regular_only=True)}
    judge = Judge()
    done = spent = 0
    for r in data["rows"]:
        r.setdefault("judged", {})
        for key, (answer, evidence) in judge_jobs(r).items():
            if key in r["judged"] and r["judged"][key].get("verdict") != "error":
                continue
            if args.max_calls and spent >= args.max_calls:
                print(f"stopped after {spent} calls (--max-calls)")
                save(args.file, data)
                return
            time.sleep(JUDGE_GAP_SECONDS)
            case = cases[r["id"]]
            out = judge.score(judge_question(case), answer, evidence, case.get("criteria", ""))
            spent += 1
            if out.get("verdict") == "error" and "tokens per day" in out.get("reason", ""):
                print("the judge's DAILY token limit is reached: stopping. Run `judge` again later; what is scored is saved.")
                save(args.file, data)
                return
            r["judged"][key] = out
            done += 1
            save(args.file, data)  # after every call: a stop loses nothing
            print(f"   judged {r['id']} #{r['repeat']}: {out.get('verdict')}", flush=True)
    save(args.file, data)
    print(f"all texts scored ({done} calls this time)")


def cmd_report(args) -> None:
    data = load(args.file)
    summary = summarize(data["rows"])
    text = render(data["rows"], summary, Path(args.file).stem.split("-", 1)[-1])
    out = Path(args.file).with_suffix(".md")
    out.write_text(text, encoding="utf-8")
    print(text)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--drafts", nargs="*", help="saved drafts (from eval.verifier_ab)")
    r.add_argument("--reuse", nargs="*", help="reuse the drafts of an earlier REPLAN_AB file (only the arms run again)")
    r.add_argument("--capture", type=int, default=0, help="how many fresh sets of drafts to capture (agents only, no judge)")
    r.add_argument("--pause", type=float, default=3.0)
    j = sub.add_parser("judge")
    j.add_argument("--file", required=True)
    j.add_argument("--max-calls", type=int, default=0)
    p = sub.add_parser("report")
    p.add_argument("--file", required=True)
    args = ap.parse_args()
    {"run": cmd_run, "judge": cmd_judge, "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    main()
