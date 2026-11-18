"""A deterministic, scripted comparison of one or more judged runs (the candidates, e.g. the replan branch) with one or more judged baseline runs.

    python -m eval.compare_runs --candidate eval/reports/20261116-234030.judged.json [more candidates ...] \\
        --baseline eval/reports/20261115-202155.judged.json eval/reports/20261115-202732.judged.json ... \\
        --out eval/reports/REPLAN-AB-20261116-234030.md

It reads saved files only: no judge, no model, no network. It uses the REGULAR cases (ids that do not start with "ho"), checks that every
file holds exactly the same set of regular ids, and writes a Markdown report that has no timestamp and a stable order, so the same inputs give
the same bytes. Every number in the report is computed here; it says which rows differ, never which side is better.

    python -m eval.compare_runs --paired --groups eval/probe/replan_probe_groups.json --candidate ON1.judged.json ON2.judged.json \\
        --baseline OFF1.judged.json OFF2.judged.json --out eval/reports/REPLAN-PROBE-<stamp>.md

The paired mode is the one exception to "never which side is better": it applies the rule of docs/REPLAN_PROBE_PREREG.md (and its amendment 0) to the pairs and names
the outcome that rule gives, with the numbers it used. It is the only place that rule is computed.
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

# ---------------------------------------------------------------------------------------------------------------------------------------------
# The paired section (the replan probe, docs/REPLAN_PROBE_PREREG.md and its amendment 0). The ON runs are the candidates and the OFF runs the baselines,
# given in the same order: the i-th ON run is paired with the i-th OFF run, case by case. Everything here is computed from the saved files.
# ---------------------------------------------------------------------------------------------------------------------------------------------

N_MIN = 20  # valid pairs needed for the rule to say anything but "inconclusive"
NET_MIN = 3  # the net of passes (and the net of the pairs where the step acted) that the rule asks for
GROUP_ORDER = ("B1", "B2", "B3", "control")
BRANCH_KINDS = {"B1": ("retry_kb",), "B2": ("add_support",), "B3": ("offer_ticket", "escalate")}  # the replan entry that shows each branch acted
PAIRED_NOTICE = ("A pair is one case at one repetition number: the ON row and the OFF row. It counts only if BOTH rows have a pass or fail verdict. A pair in which the ON row has no replan "
                 "entry is the same system in both rows, so what differs there is noise, not the step. This report applies the rule of the preregistration mechanically and says "
                 "which of its outcomes the numbers give; it makes no other inference.")


def load_groups(path: str | Path) -> dict[str, list[str]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if set(data) != set(GROUP_ORDER) or any(not isinstance(v, list) or not v for v in data.values()):
        raise SystemExit(f"compare_runs: {path} must name exactly the groups {list(GROUP_ORDER)}, each with at least one case id")
    return {g: list(data[g]) for g in GROUP_ORDER}


def sign_test_p(b: int, c: int) -> float | None:
    """Exact two-sided sign test on b + c discordant pairs, each side equally likely under the null. None when there are no discordant pairs."""
    from math import comb

    n = b + c
    if n == 0:
        return None
    tail = sum(comb(n, i) for i in range(min(b, c) + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def percentile(values: list[float], q: float) -> float | None:
    got = sorted(v for v in values if isinstance(v, (int, float)))
    if not got:
        return None
    return got[min(len(got) - 1, max(0, round(q * (len(got) - 1))))]


def paired_stats(pairs: list[dict]) -> dict:
    """pairs: dicts with `on`, `off` (verdicts) and `acted` (the ON row has a replan entry). -> the counts of the rule, over the VALID pairs."""
    valid = [p for p in pairs if p["on"] in ("pass", "fail") and p["off"] in ("pass", "fail")]
    b = sum(p["off"] == "fail" and p["on"] == "pass" for p in valid)
    c = sum(p["off"] == "pass" and p["on"] == "fail" for p in valid)
    acted = [p for p in valid if p["acted"]]
    b_act = sum(p["off"] == "fail" and p["on"] == "pass" for p in acted)
    c_act = sum(p["off"] == "pass" and p["on"] == "fail" for p in acted)
    return {"pairs": len(pairs), "valid": len(valid), "on_pass": sum(p["on"] == "pass" for p in valid), "off_pass": sum(p["off"] == "pass" for p in valid),
            "net": sum(p["on"] == "pass" for p in valid) - sum(p["off"] == "pass" for p in valid), "b": b, "c": c, "acted": len(acted), "b_act": b_act, "c_act": c_act,
            "acted_net": b_act - c_act}


def rule_outcome(s: dict, false_triggers: int, n_min: int = N_MIN, net_min: int = NET_MIN) -> tuple[str, str]:
    """(outcome, why), the rule of the preregistration and its amendment 0, applied to the counts of all the valid pairs."""
    if s["valid"] < n_min:
        return "inconclusive", f"{s['valid']} valid pairs, fewer than {n_min}"
    if s["net"] >= net_min:
        if s["acted_net"] < net_min:
            return "noise", f"Net {s['net']:+d} but acted-net {s['acted_net']:+d} (< +{net_min}): the global difference comes mostly from pairs where the step did not act"
        if false_triggers:
            return "null", f"Net {s['net']:+d} and acted-net {s['acted_net']:+d}, but {false_triggers} control row(s) of the ON arm triggered a replan"
        return "benefit suggested", f"Net {s['net']:+d}, acted-net {s['acted_net']:+d}, {s['valid']} valid pairs, no control triggered"
    if s["net"] <= -net_min:
        where = "from pairs where the step acted" if s["acted_net"] <= -net_min else f"mostly NOT from pairs where the step acted (acted-net {s['acted_net']:+d})"
        return "harm suggested", f"Net {s['net']:+d}, {where}"
    return "null", f"Net {s['net']:+d}, inside +/-{net_min - 1}"


def build_paired(on_runs: list[dict], off_runs: list[dict], groups: dict[str, list[str]], n_min: int = N_MIN, net_min: int = NET_MIN) -> str:
    if not on_runs or len(on_runs) != len(off_runs):
        raise SystemExit(f"compare_runs --paired: it needs the same number of ON and OFF runs, in the same order (got {len(on_runs)} and {len(off_runs)})")
    check_ids(on_runs[0], [*on_runs[1:], *off_runs])
    ids = sorted(on_runs[0]["rows"])
    named = sorted(i for g in groups.values() for i in g)
    if named != ids:
        raise SystemExit(f"compare_runs --paired: the groups file and the runs do not name the same cases (groups only: {sorted(set(named) - set(ids)) or 'none'}; "
                         f"runs only: {sorted(set(ids) - set(named)) or 'none'})")
    for run in on_runs:
        if not any("replans" in r for r in run["rows"].values()):
            raise SystemExit(f"compare_runs --paired: no row of {run['name']} has a 'replans' field, so it cannot tell where the step acted")
    group_of = {i: g for g, members in groups.items() for i in members}
    pairs = []
    for k, (on, off) in enumerate(zip(on_runs, off_runs), 1):
        for cid in ids:
            orow, frow = on["rows"][cid], off["rows"][cid]
            pairs.append({"rep": k, "id": cid, "group": group_of[cid], "on": verdict_of(orow), "off": verdict_of(frow), "actions": replan_actions(orow),
                          "acted": bool(replan_actions(orow)), "orow": orow, "frow": frow})
    overall = paired_stats(pairs)
    false_rows = [p for p in pairs if p["group"] == "control" and p["acted"]]
    outcome, why = rule_outcome(overall, len(false_rows), n_min, net_min)
    p_act = sign_test_p(overall["b_act"], overall["c_act"])

    out: list[str] = ["# Replan probe: ON runs against OFF runs, paired (scripted)", ""]
    for k, (on, off) in enumerate(zip(on_runs, off_runs), 1):
        out.append(f"- **Repetition {k}:** ON `{on['name']}` (replan_max={on['summary'].get('replan_max', 'not recorded')}, commit {short_commit(on)}) with OFF `{off['name']}` "
                   f"(replan_max={off['summary'].get('replan_max', 'not recorded')}, commit {short_commit(off)}), {len(ids)} cases")
    out += ["", f"**{PAIRED_NOTICE}**", "", f"**Outcome of the preregistered rule: {outcome}.** {why}.", ""]

    dropped = [p for p in pairs if not (p["on"] in ("pass", "fail") and p["off"] in ("pass", "fail"))]
    out += ["## The pairs", "", f"- pairs: {overall['pairs']}; valid: {overall['valid']} (the rule needs at least {n_min}); dropped: {len(dropped)}"]
    out += [f"  - dropped: `{p['id']}` repetition {p['rep']}: ON {p['on']}, OFF {p['off']}" for p in dropped]
    out += [f"- ON passes {overall['on_pass']}, OFF passes {overall['off_pass']}, **Net {overall['net']:+d}** over the valid pairs",
            f"- discordant pairs: OFF fail -> ON pass {overall['b']}, OFF pass -> ON fail {overall['c']}",
            f"- pairs where the ON row acted: {overall['acted']}; among them OFF fail -> ON pass {overall['b_act']}, OFF pass -> ON fail {overall['c_act']}, **acted-net {overall['acted_net']:+d}**",
            f"- exact two-sided sign test on those discordant acted pairs ({overall['b_act'] + overall['c_act']}): "
            + ("not applicable (none)" if p_act is None else f"p = {p_act:.4f}") + " (descriptive, no threshold)",
            f"- false triggers (a control row of the ON arm with a replan entry): {len(false_rows)}"]
    out += [f"  - `{p['id']}` repetition {p['rep']}: {', '.join(p['actions'])}" for p in false_rows]

    out += ["", "## By group", "", "| group | pairs | valid | ON pass | OFF pass | Net | OFF fail -> ON pass | OFF pass -> ON fail | pairs where ON acted |", "|---|---|---|---|---|---|---|---|---|"]
    blocks = [(g, [p for p in pairs if p["group"] == g]) for g in GROUP_ORDER]
    blocks.insert(2, ("B1 + B2 (without the bias of B3)", [p for p in pairs if p["group"] in ("B1", "B2")]))
    for name, rows in blocks:
        s = paired_stats(rows)
        out.append(f"| {name} | {s['pairs']} | {s['valid']} | {s['on_pass']} | {s['off_pass']} | {s['net']:+d} | {s['b']} | {s['c']} | {s['acted']} |")
    out += ["", "B3 favors the ON arm by construction (its criterion rewards a concrete way forward and the ticket offer of the step supplies one); the line B1 + B2 is the reading "
            "without that bias. The rule above uses all the valid pairs.", ""]

    kinds_of = lambda rows: ", ".join(f"{k} {n}" for k, n in sorted(Counter(a for p in rows for a in p["actions"]).items())) or "none"  # noqa: E731
    noncontrol = [p for p in pairs if p["group"] != "control"]
    by_on = [("ON acted", [p for p in noncontrol if p["acted"]]), ("ON did not act", [p for p in noncontrol if not p["acted"]]), ("controls", [p for p in pairs if p["group"] == "control"])]
    out += ["## By what the ON row did (amendment 1 of the preregistration)", "",
            "The non-control pairs split by whether the ON row has at least one replan entry in THIS run; the controls apart. **The main reading is the `ON acted` line**: it is the only "
            "place the step can make a difference, and a pair in which it did not act is the same system in both rows. The groups by design branch above stay as preregistered.", "",
            "| what the ON row did | pairs | valid | ON pass | OFF pass | Net | OFF fail -> ON pass | OFF pass -> ON fail | replan entries of the ON rows |", "|---|---|---|---|---|---|---|---|---|"]
    for name, rows in by_on:
        s = paired_stats(rows)
        out.append(f"| {name} | {s['pairs']} | {s['valid']} | {s['on_pass']} | {s['off_pass']} | {s['net']:+d} | {s['b']} | {s['c']} | {kinds_of(rows)} |")
    out.append("")

    out += ["## Where the step acted (the ON rows)", "", "Each cell is the number of cases of the group whose ON row has an entry of the branch (B1 `retry_kb`, B2 `add_support`, "
            "B3 `offer_ticket` or `escalate`); the rows `any entry` count any replan entry of the ON row, whatever its kind.", "",
            "| group | " + " | ".join(f"repetition {k}" for k in range(1, len(on_runs) + 1)) + " |", "|---|" + "---|" * len(on_runs)]
    for g in ("B1", "B2", "B3"):
        cells = []
        for k in range(1, len(on_runs) + 1):
            hit = [p for p in pairs if p["rep"] == k and p["group"] == g and any(a in BRANCH_KINDS[g] for a in p["actions"])]
            cells.append(f"{len(hit)} of {len(groups[g])}")
        out.append(f"| {g} ({', '.join(BRANCH_KINDS[g])}) | " + " | ".join(cells) + " |")
    for g in GROUP_ORDER:
        cells = [f"{sum(p['acted'] for p in pairs if p['rep'] == k and p['group'] == g)} of {len(groups[g])}" for k in range(1, len(on_runs) + 1)]
        out.append(f"| any entry, {g} | " + " | ".join(cells) + " |")
    out += ["", "| case | group | repetition | replan entries of the ON row | ON verdict | OFF verdict |", "|---|---|---|---|---|---|"]
    for p in pairs:
        if p["acted"]:
            out.append(f"| {p['id']} | {p['group']} | {p['rep']} | {', '.join(p['actions'])} | {p['on']} | {p['off']} |")
    if not any(p["acted"] for p in pairs):
        out.append("| none | | | | | |")

    out += ["", "## The rows where the ON row acted, one by one (descriptive)", "",
            "Added to this report after the numbers of the run were seen, as a display only: it changes no count and not the rule. For each pair in which the ON row has a replan entry: "
            "the entries as saved, the verdict and the judge's scores in both arms, how many claims the JUDGE listed as unsupported in each, the note of the verifier in each, and the agent cost "
            "and latency of the ON row against the mean of the same case in the OFF runs. `reason` is the judge's own sentence, cut at 220 characters.", ""]
    off_by_id = {cid: [off["rows"][cid] for off in off_runs] for cid in ids}

    def jf(row: dict, key: str):
        return (row.get("judge") or {}).get(key)

    def n_claims(row: dict) -> int | str:
        c = (row.get("judge") or {}).get("unsupported_claims")
        return "-" if c is None else len(c)

    def cut(text, n=220):
        text = " ".join(str(text or "").split())
        return text if len(text) <= n else text[:n - 3] + "..."

    deltas_c, deltas_l = [], []
    for p in pairs:
        if not p["acted"]:
            continue
        o, f = p["orow"], p["frow"]
        offs = off_by_id[p["id"]]
        mc = mean([(r.get("usage") or {}).get("cost_usd") for r in offs])
        ml = mean([r.get("latency_ms") for r in offs])
        oc, ol = (o.get("usage") or {}).get("cost_usd"), o.get("latency_ms")
        if isinstance(oc, (int, float)) and mc is not None:
            deltas_c.append(oc - mc)
        if isinstance(ol, (int, float)) and ml is not None:
            deltas_l.append(ol - ml)
        out += [f"- **`{p['id']}`, repetition {p['rep']}** ({p['group']}): ON {p['on']}, OFF {p['off']}",
                f"  - replan entries of the ON row: {'; '.join(str(r) for r in (o.get('replans') or []))}",
                f"  - judge scores, groundedness / completeness: ON {jf(o, 'groundedness')} / {jf(o, 'completeness')}, OFF {jf(f, 'groundedness')} / {jf(f, 'completeness')}; "
                f"claims the judge listed as unsupported: ON {n_claims(o)}, OFF {n_claims(f)}",
                f"  - note of the verifier: ON `{cut(o.get('verify_note'), 120)}`, OFF `{cut(f.get('verify_note'), 120)}`",
                f"  - agent cost: ON {fmt_usd(oc)} against the OFF mean {fmt_usd(mc)}; latency (ms): ON {fmt_ms(ol)} against the OFF mean {fmt_ms(ml)}",
                f"  - reason, ON: {cut(jf(o, 'reason'))}", f"  - reason, OFF: {cut(jf(f, 'reason'))}"]
    if not deltas_c:
        out.append("- no pair in which the ON row acted")
    else:
        med = lambda v: sorted(v)[len(v) // 2] if len(v) % 2 else (sorted(v)[len(v) // 2 - 1] + sorted(v)[len(v) // 2]) / 2  # noqa: E731
        out += ["", f"Over the {len(deltas_c)} ON rows that acted, against the mean of the same case in the OFF runs: extra agent cost {sum(deltas_c) / len(deltas_c):+.5f} USD per row on average "
                f"({sum(deltas_c):+.5f} USD in all); extra latency {sum(deltas_l) / len(deltas_l):+,.0f} ms per row on average and {med(deltas_l):+,.0f} ms at the median "
                "(a large difference between the two means a few slow rows of the OFF runs weigh on the average)."]
    out += ["", "## Noise baseline: OFF 1 against OFF 2", ""]
    if len(off_runs) < 2:
        out.append("There is only one OFF run: no baseline.")
    else:
        o1, o2 = off_runs[0], off_runs[1]
        both = [cid for cid in ids if verdict_of(o1["rows"][cid]) in ("pass", "fail") and verdict_of(o2["rows"][cid]) in ("pass", "fail")]
        diff = [cid for cid in both if verdict_of(o1["rows"][cid]) != verdict_of(o2["rows"][cid])]
        out += [f"The same cases with the same configuration (`{o1['name']}` and `{o2['name']}`): {len(both)} cases have a verdict in both, **{len(diff)} get a different one**."]
        out += [f"- `{cid}`: OFF 1 {verdict_of(o1['rows'][cid])}, OFF 2 {verdict_of(o2['rows'][cid])}" for cid in diff]

    def usd(rows: list[dict]) -> list[float]:
        return [(r.get("usage") or {}).get("cost_usd") for r in rows]

    on_rows, off_rows = [p["orow"] for p in pairs], [p["frow"] for p in pairs]
    out += ["", "## Agent cost, latency and what was written", "", "| arm | rows | mean agent cost | total agent cost | latency p50 (ms) | latency p95 (ms) | rows with a handoff |", "|---|---|---|---|---|---|---|"]
    for name, rows in (("ON", on_rows), ("OFF", off_rows)):
        costs = [c for c in usd(rows) if isinstance(c, (int, float))]
        lat = [r.get("latency_ms") for r in rows]
        out.append(f"| {name} | {len(rows)} | {fmt_usd(mean(costs))} | {fmt_usd(sum(costs) if costs else None)} | {fmt_ms(percentile(lat, 0.5))} | {fmt_ms(percentile(lat, 0.95))} | "
                   f"{sum(bool(r.get('handoff')) for r in rows)} |")
    out += ["", "The tickets and handoffs each run wrote are counted from the files of its own folders by the person who ran it, not from the rows.", "", "## How to read this", "", ATTRIBUTION, ""]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--candidate", required=True, nargs="+", action="extend", help="the judged run(s) to compare (a .judged.json); repeat the flag or list several")
    ap.add_argument("--baseline", required=True, nargs="+", help="one or more judged baseline runs")
    ap.add_argument("--out", required=True, help="the Markdown file to write")
    ap.add_argument("--paired", action="store_true", help="the replan probe: the i-th candidate (ON) is paired with the i-th baseline (OFF) case by case; needs --groups")
    ap.add_argument("--groups", help="with --paired: the JSON file that names the cases of each group (B1, B2, B3, control), eval/probe/replan_probe_groups.json")
    args = ap.parse_args(argv)
    candidates, baselines = [load_run(p) for p in args.candidate], [load_run(p) for p in args.baseline]
    if args.paired:
        if not args.groups:
            raise SystemExit("compare_runs --paired needs --groups")
        text = build_paired(candidates, baselines, load_groups(args.groups))
    else:
        text = build(candidates[0], baselines) if len(candidates) == 1 else build_many(candidates, baselines)
    Path(args.out).write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
