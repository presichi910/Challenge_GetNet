"""Export an eval run to a CSV a human fills in, to calibrate the LLM judge.

    python -m eval.calibration.export                         # newest eval/reports/<stamp>.json
    python -m eval.calibration.export eval/reports/20261112-150000.json -o eval/calibration/labels_template.csv

One row per judged case: the question, the answer, a summary of the evidence (tools called with their
arguments, the handoff), the judge's scores and reason, and empty columns for the human:
human_grounded (1-5), human_complete (1-5), human_pass (yes/no) and notes. Then run
`python -m eval.calibration.agreement <filled csv>`.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
DEFAULT_OUT = Path(__file__).resolve().parent / "labels_template.csv"

COLUMNS = ["id", "holdout", "question", "criteria", "answer", "evidence_summary", "evidence", "judge_relevance", "judge_grounded",
           "judge_complete", "judge_tone", "judge_verdict", "judge_reason",
           "human_grounded", "human_complete", "human_pass", "notes"]
HUMAN_COLUMNS = ("human_grounded", "human_complete", "human_pass", "notes")
# the sheet the human fills in: no judge score or reason, so the human label is not anchored on the judge
BLIND_COLUMNS = ["id", "holdout", "question", "criteria", "answer", "evidence", *HUMAN_COLUMNS]
NO_EVIDENCE = "(not stored in this report: re-run the eval with the current run_eval.py)"


def newest_report(reports: Path = REPORTS) -> Path:
    found = sorted(p for p in reports.glob("[0-9]*-[0-9]*.json"))
    if not found:
        raise FileNotFoundError(f"no eval report JSON in {reports} (run python -m eval.run_eval first)")
    return found[-1]


def evidence_summary(row: dict) -> str:
    parts = [f"{tc['agent']}.{tc['name']}({json.dumps(tc.get('args', {}), ensure_ascii=False)})"
             for tc in row.get("tool_calls", [])]
    if not parts:
        parts = ["no tools"]
    if row.get("handoff"):
        parts.append(f"handoff={row['handoff']}")
    return "; ".join(parts)


def question_of(row: dict) -> str:
    past = row.get("history") or []
    if not past:
        return row.get("message", "")
    turns = " | ".join(f"{t['role']}: {t['content']}" for t in past)
    return f"[earlier: {turns}] {row.get('message', '')}"

def load_criteria(path: Path = ROOT / "dataset.jsonl") -> dict[str, str]:
    """id -> the case-specific expected behavior the judge is given (it takes precedence in JUDGE_PROMPT).
    The human must read it too, or the two are not scoring the same thing."""
    if not path.exists():
        return {}
    cases = (json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip())
    return {c["id"]: c["criteria"] for c in cases if c.get("criteria")}


def to_label_rows(report: dict, criteria: dict[str, str] | None = None) -> list[dict]:
    """Judged cases only: a provider error or a judge error has nothing to calibrate."""
    criteria = criteria or {}
    out = []
    for r in report.get("rows", []):
        j = r.get("judge") or {}
        if r.get("provider_error") or not j or j.get("verdict") == "error":
            continue
        out.append({"id": r["id"], "holdout": "yes" if r.get("holdout") else "", "question": question_of(r),
                    "criteria": criteria.get(r["id"]), "answer": r.get("answer", ""), "evidence_summary": evidence_summary(r),
                    "evidence": ("\n\n".join(r["evidence"]) or "(sin evidencia: no se llamó ninguna tool)") if "evidence" in r else NO_EVIDENCE,
                    "judge_relevance": j.get("relevance"), "judge_grounded": j.get("groundedness"),
                    "judge_complete": j.get("completeness"), "judge_tone": j.get("tone"),
                    "judge_verdict": j.get("verdict"), "judge_reason": j.get("reason", ""),
                    **{c: "" for c in HUMAN_COLUMNS}})
    return out


def write_csv(rows: list[dict], path: Path, columns: list[str] = COLUMNS) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("report", nargs="?", help="eval report JSON (default: the newest in eval/reports)")
    ap.add_argument("-o", "--out", default=str(DEFAULT_OUT))
    ap.add_argument("--blind", metavar="PATH", help="also write the sheet to label: no judge columns "
                    "(agreement.py --judge-key <the -o file> joins them back by id)")
    args = ap.parse_args()
    src = Path(args.report) if args.report else newest_report()
    rows = to_label_rows(json.loads(src.read_text(encoding="utf-8")), load_criteria())
    write_csv(rows, Path(args.out))
    if args.blind:
        write_csv(rows, Path(args.blind), BLIND_COLUMNS)
    print(f"{len(rows)} cases from {src} -> {args.out}. Fill human_grounded, human_complete (1-5), "
          "human_pass (yes/no), notes; then: python -m eval.calibration.agreement <file>")


if __name__ == "__main__":
    main()
