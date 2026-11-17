"""A deterministic, scripted comparison of one or more judged runs (the candidates, e.g. the replan branch) with one or more judged baseline runs.

    python -m eval.compare_runs --candidate eval/reports/20261116-234030.judged.json [more candidates ...] \\
        --baseline eval/reports/20261115-202155.judged.json eval/reports/20261115-202732.judged.json ... \\
        --out eval/reports/REPLAN-AB-20261116-234030.md

It reads saved files only: no judge, no model, no network. It uses the REGULAR cases (ids that do not start with "ho"), checks that every
file holds exactly the same set of regular ids, and writes a Markdown report that has no timestamp and a stable order, so the same inputs give
the same bytes. Every number in the report is computed here; it says which rows differ, never which side is better.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

VERDICTS = ("pass", "fail", "error", "unscored")

NOTICE = ("This compares ONE judged repetition (the candidate) with the baseline repetitions listed above. The judge is not deterministic: "
          "the same text can get another verdict on another pass, so a single row that differs is not a finding.")

ATTRIBUTION = ("A difference in a row where no replan action fired is not evidence about the replan: no replan action ran in that row. This report does not establish its cause. "
               "Candidates and baselines also differ in run date, commit and judge session, and some rows depend on tool data or web search results that change with the date.")


def is_regular(case_id: str) -> bool:
    return not case_id.startswith("ho")


def load_run(path: str | Path) -> dict:
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"compare_runs: {p} does not exist") from None
    except json.JSONDecodeError as e:
        raise SystemExit(f"compare_runs: {p} is not valid JSON ({e})") from e
    if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
        raise SystemExit(f"compare_runs: {p} has no 'rows' list: it is not a run file")
    rows = {r["id"]: r for r in data["rows"] if isinstance(r, dict) and "id" in r and is_regular(r["id"])}
    if not rows:
        raise SystemExit(f"compare_runs: {p} has no regular case (every id starts with 'ho')")
    return {"name": p.name, "summary": data.get("summary") or {}, "rows": rows}


def verdict_of(row: dict) -> str:
    judge = row.get("judge")
    if not judge:
        return "unscored"
    v = judge.get("verdict")
    return v if v in ("pass", "fail", "error") else "unscored"


def replan_actions(row: dict) -> list[str]:
    """The kind of each replan the observe step took in this row ("retry_kb", "add_support", "offer_ticket", ...)."""
    return [str(r).split(":", 1)[0].strip() for r in (row.get("replans") or [])]


def check_ids(candidate: dict, baselines: list[dict]) -> None:
    want = set(candidate["rows"])
    for b in baselines:
        have = set(b["rows"])
        if have != want:
            only_c, only_b = sorted(want - have), sorted(have - want)
            raise SystemExit(f"compare_runs: the regular case ids of {b['name']} do not match those of the candidate {candidate['name']}: "
                             f"only in the candidate: {only_c or 'none'}; only in the baseline: {only_b or 'none'}")


def counts(run: dict) -> Counter:
    return Counter(verdict_of(r) for r in run["rows"].values())


def short_commit(run: dict) -> str:
    commit = run["summary"].get("commit")
    if not commit:
        return "not recorded"
    clean = run["summary"].get("tree_clean")
    return f"{str(commit)[:12]} (tree_clean={clean})"


def fmt_counts(c: Counter) -> str:
    return ", ".join(f"{v} {c.get(v, 0)}" for v in VERDICTS)


def unanimous(verdicts: list[str]) -> str | None:
    """The one verdict ("pass" or "fail") that every baseline that has a verdict gave; None if they differ or none has one."""
    given = {v for v in verdicts if v in ("pass", "fail")}
    return given.pop() if len(given) == 1 else None


def mean(values: list) -> float | None:
    got = [v for v in values if isinstance(v, (int, float))]
    return sum(got) / len(got) if got else None


def fmt_ms(v: float | None) -> str:
    return "-" if v is None else f"{v:,.0f}"


def fmt_usd(v: float | None) -> str:
    return "-" if v is None else f"${v:.5f}"


def score(row: dict, key: str) -> str:
    v = (row.get("judge") or {}).get(key)
    return "-" if v is None else str(v)


def headline(candidate: dict, baselines: list[dict]) -> str:
    """One generated line: the candidate's pass count against the range of the baselines' pass counts, and how many rows fired a replan.
    Inside means min <= candidate <= max, ends included. A row with no pass verdict (fail, error, unscored) is not counted as a pass."""
    total = len(candidate["rows"])
    passed = counts(candidate).get("pass", 0)
    base = [counts(b).get("pass", 0) for b in baselines]
    low, high = min(base), max(base)
    where = "inside" if low <= passed <= high else "outside"
    fired = sum(bool(replan_actions(r)) for r in candidate["rows"].values())
    return (f"**Headline:** candidate {passed} pass of {total} regular rows; baseline repetitions {low}-{high} pass of {total}; "
            f"the candidate count is {where} that range; replan fired in {fired} of {total} rows.")


def build(candidate: dict, baselines: list[dict]) -> str:
    check_ids(candidate, baselines)
    ids = sorted(candidate["rows"])
    if not any("replans" in r for r in candidate["rows"].values()):
        raise SystemExit(f"compare_runs: no row of {candidate['name']} has a 'replans' field, so it cannot tell where the replan fired")
    n = len(baselines)
    out: list[str] = ["# Candidate run against baseline runs (regular cases, scripted)", ""]
    out += [f"- **Candidate:** `{candidate['name']}`, commit {short_commit(candidate)}, replan_max={candidate['summary'].get('replan_max', 'not recorded')}, "
            f"label={candidate['summary'].get('label') or 'none'}, {len(ids)} regular rows"]
    for i, b in enumerate(baselines, 1):
        out.append(f"- **Baseline {i}:** `{b['name']}`, commit {short_commit(b)}, replan_max={b['summary'].get('replan_max', 'not recorded')}, {len(b['rows'])} regular rows")
    commits = {str(r["summary"].get("commit")) for r in [candidate, *baselines]}
    if len(commits) > 1:
        out.append("- The runs come from different commits (shown above); the code between them is not compared by this script.")
    out += ["", f"**{NOTICE}**", "", "Only the regular cases are compared (ids that start with `ho` are left out). A verdict is the judge's `verdict` field as saved; "
            "`error` is a row the judge could not score and `unscored` a row with no judge field.", "", headline(candidate, baselines), "",
            "## Verdict counts", "", "| run | " + " | ".join(VERDICTS) + " |", "|---|" + "---|" * len(VERDICTS)]
    for run, label in [(candidate, "candidate")] + [(b, f"baseline {i}") for i, b in enumerate(baselines, 1)]:
        c = counts(run)
        out.append(f"| {label} | " + " | ".join(str(c.get(v, 0)) for v in VERDICTS) + " |")

    rows = []
    for cid in ids:
        crow = candidate["rows"][cid]
        bverdicts = [verdict_of(b["rows"][cid]) for b in baselines]
        actions = replan_actions(crow)
        rows.append({"id": cid, "cv": verdict_of(crow), "bv": bverdicts, "k": sum(v == "pass" for v in bverdicts), "actions": actions,
                     "unan": unanimous(bverdicts), "g": score(crow, "groundedness"), "c": score(crow, "completeness")})

    out += ["", "## Per case", "", f"`baselines that passed` is k of N (N = {n}); a baseline row with no pass/fail verdict counts as not passed and is listed as such.", "",
            "| case | candidate verdict | baselines that passed | replan fired | groundedness | completeness |", "|---|---|---|---|---|---|"]
    for r in rows:
        missing = sum(v not in ("pass", "fail") for v in r["bv"])
        k = f"{r['k']} of {n}" + (f" ({missing} without verdict)" if missing else "")
        fired = ("yes: " + ", ".join(r["actions"])) if r["actions"] else "no"
        out.append(f"| {r['id']} | {r['cv']} | {k} | {fired} | {r['g']} | {r['c']} |")

    differing = [r for r in rows if r["cv"] in ("pass", "fail") and r["unan"] is not None and r["cv"] != r["unan"]]
    out += ["", "## Rows where the candidate's verdict differs from the unanimous verdict of the baselines", "",
            "Unanimous means every baseline that has a pass or fail verdict on the row gave the same one. Rows where the baselines disagree among themselves are not listed.", ""]
    for title, fired in (("Replan fired", True), ("Replan did not fire", False)):
        group = [r for r in differing if bool(r["actions"]) == fired]
        out += [f"### {title} ({len(group)})", ""]
        out += [f"- `{r['id']}`: candidate {r['cv']}, baselines unanimous {r['unan']} ({r['k']} of {n} passed); candidate groundedness {r['g']}, completeness {r['c']}"
                for r in group] or ["- none"]
        out.append("")
    out += ["## Latency and agent cost of the rows where the replan fired", "",
            "The candidate's value for the row, against the mean of the same case over the baselines (the baselines never fire a replan). Latency in ms, agent cost in USD from the row's `usage`; "
            "`-` where a file has no value.", "", "| case | replan fired | candidate latency | baseline mean latency | candidate cost | baseline mean cost |", "|---|---|---|---|---|---|"]
    fired_rows = [r for r in rows if r["actions"]]
    for r in fired_rows:
        crow = candidate["rows"][r["id"]]
        lat = [b["rows"][r["id"]].get("latency_ms") for b in baselines]
        cost = [(b["rows"][r["id"]].get("usage") or {}).get("cost_usd") for b in baselines]
        out.append(f"| {r['id']} | {', '.join(r['actions'])} | {fmt_ms(crow.get('latency_ms'))} | {fmt_ms(mean(lat))} | "
                   f"{fmt_usd((crow.get('usage') or {}).get('cost_usd'))} | {fmt_usd(mean(cost))} |")
    if not fired_rows:
        out.append("| none | | | | | |")
    out.append("")
    out += ["## How to read the differences", "", ATTRIBUTION, ""]
    return "\n".join(out)


INFERENCE = ("With {k} repetitions with the replan on against {n} base repetitions and a judge that is not deterministic, this report makes no statistical inference: "
             "it lists, row by row, what each repetition did.")


def build_many(candidates: list[dict], baselines: list[dict]) -> str:
    """The same comparison for several candidate repetitions (e.g. the three replan-on repetitions) against the baselines, in one report."""
    for c in candidates:
        check_ids(c, baselines)
        if not any("replans" in r for r in c["rows"].values()):
            raise SystemExit(f"compare_runs: no row of {c['name']} has a 'replans' field, so it cannot tell where the replan fired")
    k, n = len(candidates), len(baselines)
    ids = sorted(candidates[0]["rows"])
    out: list[str] = [f"# {k} candidate repetitions against {n} baseline repetitions (regular cases, scripted)", ""]
    for j, c in enumerate(candidates, 1):
        out.append(f"- **Candidate {j}:** `{c['name']}`, commit {short_commit(c)}, replan_max={c['summary'].get('replan_max', 'not recorded')}, "
                   f"label={c['summary'].get('label') or 'none'}, {len(c['rows'])} regular rows")
    for i, b in enumerate(baselines, 1):
        out.append(f"- **Baseline {i}:** `{b['name']}`, commit {short_commit(b)}, replan_max={b['summary'].get('replan_max', 'not recorded')}, {len(b['rows'])} regular rows")
    commits = {str(r["summary"].get("commit")) for r in [*candidates, *baselines]}
    if len(commits) > 1:
        out.append("- The runs come from different commits (shown above); the code between them is not compared by this script.")
    notice = (f"This compares {k} judged repetitions (the candidates) with the {n} baseline repetitions listed above. The judge is not deterministic: "
              "the same text can get another verdict on another pass, so a single row that differs is not a finding.")
    out += ["", f"**{notice}**", "", "Only the regular cases are compared (ids that start with `ho` are left out). A verdict is the judge's `verdict` field as saved; "
            "`error` is a row the judge could not score and `unscored` a row with no judge field.", ""]
    for j, c in enumerate(candidates, 1):
        out += [f"Candidate {j} (`{c['name']}`):", "", headline(c, baselines), ""]

    out += ["## Verdict counts", "", "| run | " + " | ".join(VERDICTS) + " |", "|---|" + "---|" * len(VERDICTS)]
    for run, label in [(c, f"candidate {j}") for j, c in enumerate(candidates, 1)] + [(b, f"baseline {i}") for i, b in enumerate(baselines, 1)]:
        cnt = counts(run)
        out.append(f"| {label} | " + " | ".join(str(cnt.get(v, 0)) for v in VERDICTS) + " |")

    rows = []
    for cid in ids:
        bverdicts = [verdict_of(b["rows"][cid]) for b in baselines]
        per = []
        for c in candidates:
            crow = c["rows"][cid]
            per.append({"v": verdict_of(crow), "actions": replan_actions(crow), "g": score(crow, "groundedness"), "c": score(crow, "completeness")})
        rows.append({"id": cid, "bv": bverdicts, "k": sum(v == "pass" for v in bverdicts), "unan": unanimous(bverdicts), "per": per})

    def kof(r: dict) -> str:
        missing = sum(v not in ("pass", "fail") for v in r["bv"])
        return f"{r['k']} of {n}" + (f" ({missing} without verdict)" if missing else "")

    def fired(p: dict) -> str:
        return ("yes: " + ", ".join(p["actions"])) if p["actions"] else "no"

    cols = range(1, k + 1)
    out += ["", "## Per case", "", f"`baselines that passed` is k of N (N = {n}); a baseline row with no pass/fail verdict counts as not passed and is listed as such. "
            "Scores are the judge's groundedness / completeness for that candidate.", "",
            "| case | " + " | ".join(f"verdict {j}" for j in cols) + " | baselines that passed | " + " | ".join(f"replan fired {j}" for j in cols) + " | "
            + " | ".join(f"scores {j}" for j in cols) + " |", "|---|" + "---|" * (3 * k + 1)]
    for r in rows:
        out.append(f"| {r['id']} | " + " | ".join(p["v"] for p in r["per"]) + f" | {kof(r)} | " + " | ".join(fired(p) for p in r["per"]) + " | "
                   + " | ".join(f"{p['g']} / {p['c']}" for p in r["per"]) + " |")

    for j in cols:
        out += ["", f"## Candidate {j}: rows where its verdict differs from the unanimous verdict of the baselines", "",
                "Unanimous means every baseline that has a pass or fail verdict on the row gave the same one. Rows where the baselines disagree among themselves are not listed.", ""]
        differing = [(r, r["per"][j - 1]) for r in rows if r["per"][j - 1]["v"] in ("pass", "fail") and r["unan"] is not None and r["per"][j - 1]["v"] != r["unan"]]
        for title, was_fired in (("replan fired", True), ("replan did not fire", False)):
            group = [(r, p) for r, p in differing if bool(p["actions"]) == was_fired]
            out += [f"### Candidate {j}: {title} ({len(group)})", ""]
            out += [f"- `{r['id']}`: candidate {p['v']}, baselines unanimous {r['unan']} ({r['k']} of {n} passed); candidate groundedness {p['g']}, completeness {p['c']}"
                    for r, p in group] or ["- none"]
            out.append("")

    hit = [r for r in rows if any(p["actions"] for p in r["per"])]
    out += ["## Rows where the replan fired in at least one candidate", "",
            "The verdict of every candidate on the row (whether or not the replan fired in that candidate), and the baselines that passed.", "",
            "| case | " + " | ".join(f"replan fired {j}" for j in cols) + " | " + " | ".join(f"verdict {j}" for j in cols) + " | baselines that passed |",
            "|---|" + "---|" * (2 * k + 1)]
    for r in hit:
        out.append(f"| {r['id']} | " + " | ".join(fired(p) for p in r["per"]) + " | " + " | ".join(p["v"] for p in r["per"]) + f" | {kof(r)} |")
    if not hit:
        out.append("| none |" + " |" * (2 * k + 1))
    out += ["", "Latency in ms and agent cost in USD (from each row's `usage`) of those cases: each candidate's value, and the mean of the same case over the baselines (the baselines never fire a replan). "
            "`-` where a file has no value.", "",
            "| case | " + " | ".join(f"latency {j}" for j in cols) + " | baseline mean latency | " + " | ".join(f"cost {j}" for j in cols) + " | baseline mean cost |",
            "|---|" + "---|" * (2 * k + 2)]
    for r in hit:
        cid = r["id"]
        lat = [b["rows"][cid].get("latency_ms") for b in baselines]
        cost = [(b["rows"][cid].get("usage") or {}).get("cost_usd") for b in baselines]
        out.append(f"| {cid} | " + " | ".join(fmt_ms(c["rows"][cid].get("latency_ms")) for c in candidates) + f" | {fmt_ms(mean(lat))} | "
                   + " | ".join(fmt_usd((c["rows"][cid].get("usage") or {}).get("cost_usd")) for c in candidates) + f" | {fmt_usd(mean(cost))} |")
    if not hit:
        out.append("| none |" + " |" * (2 * k + 2))
    out += ["", "## How to read the differences", "", ATTRIBUTION, "", INFERENCE.format(k=k, n=n), ""]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--candidate", required=True, nargs="+", action="extend", help="the judged run(s) to compare (a .judged.json); repeat the flag or list several")
    ap.add_argument("--baseline", required=True, nargs="+", help="one or more judged baseline runs")
    ap.add_argument("--out", required=True, help="the Markdown file to write")
    args = ap.parse_args(argv)
    candidates, baselines = [load_run(p) for p in args.candidate], [load_run(p) for p in args.baseline]
    text = build(candidates[0], baselines) if len(candidates) == 1 else build_many(candidates, baselines)
    Path(args.out).write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
