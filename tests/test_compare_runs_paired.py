"""eval/compare_runs.py --paired: the replan probe's pairs, the rule of docs/REPLAN_PROBE_PREREG.md (amendment 0) applied mechanically, from synthetic files.
It covers a pair dropped for a judge error, a pair in which ON acted and a pair with no action, the noise guard, the sign test and the noise baseline."""
import json
from pathlib import Path

import pytest

from eval.compare_runs import (BRANCH_KINDS, build_paired, load_groups, load_run, main, paired_stats, percentile, rule_outcome, sign_test_p)

REPO = Path(__file__).resolve().parent.parent
GROUPS = load_groups(REPO / "eval" / "probe" / "replan_probe_groups.json")
IDS = [i for g in GROUPS.values() for i in g]


def row(verdict: str, actions=(), cost=0.002, latency=5000, handoff=None) -> dict:
    r = {"id": "x", "answer": "a", "replans": [f"{a}: detail" for a in actions], "usage": {"cost_usd": cost}, "latency_ms": latency, "handoff": handoff}
    r["judge"] = {"verdict": verdict, "groundedness": 4, "completeness": 4} if verdict != "unscored" else None
    return r


def write(tmp_path: Path, name: str, rows: dict[str, dict], replan_max: int = 0) -> dict:
    data = {"summary": {"commit": "a" * 40, "tree_clean": True, "replan_max": replan_max}, "rows": [{**r, "id": i} for i, r in rows.items()]}
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return load_run(path)


def pair_runs(tmp_path, on_spec: dict[str, list], off_spec: dict[str, list]):
    """on_spec / off_spec: case id -> [verdict or (verdict, actions) per repetition]; every case not named is `pass` / `pass`, no action."""
    def runs(spec, label, replan_max):
        out = []
        for rep in range(2):
            rows = {}
            for cid in IDS:
                v = spec.get(cid, ["pass", "pass"])[rep]
                rows[cid] = row(*v) if isinstance(v, tuple) else row(v)
            out.append(write(tmp_path, f"{label}{rep + 1}.judged.json", rows, replan_max))
        return out
    return runs(on_spec, "on", 2), runs(off_spec, "off", 0)


def text_of(tmp_path, on_spec, off_spec) -> str:
    on, off = pair_runs(tmp_path, on_spec, off_spec)
    return build_paired(on, off, GROUPS)


# ---- the pure counts ----

def test_the_groups_file_names_the_twelve_cases_of_the_probe_and_nothing_else():
    probe = [json.loads(line)["id"] for line in (REPO / "eval" / "probe" / "replan_probe.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    assert sorted(IDS) == sorted(probe) and len(IDS) == 12 and {g: len(v) for g, v in GROUPS.items()} == {"B1": 3, "B2": 3, "B3": 3, "control": 3}
    assert all(i.startswith("c") for i in GROUPS["control"]) and all(i.startswith("p") for g in ("B1", "B2", "B3") for i in GROUPS[g])
    assert set(BRANCH_KINDS) == {"B1", "B2", "B3"}


def test_the_exact_sign_test():
    assert sign_test_p(0, 0) is None
    assert sign_test_p(3, 0) == pytest.approx(0.25) and sign_test_p(0, 3) == pytest.approx(0.25)
    assert sign_test_p(5, 1) == pytest.approx(0.21875) and sign_test_p(2, 2) == 1.0
    assert sign_test_p(10, 0) == pytest.approx(2 / 1024)


def test_percentiles():
    assert percentile([], 0.5) is None and percentile([5], 0.95) == 5 and percentile([1, 2, 3, 4, 5], 0.5) == 3 and percentile([None, 4, 2], 0.5) in (2, 4)


def test_a_pair_with_a_judge_error_is_dropped_from_every_count_and_listed():
    pairs = [{"on": "pass", "off": "fail", "acted": True}, {"on": "error", "off": "fail", "acted": True}, {"on": "pass", "off": "unscored", "acted": False},
             {"on": "fail", "off": "pass", "acted": False}]
    s = paired_stats(pairs)
    assert s["pairs"] == 4 and s["valid"] == 2 and s["net"] == 0 and (s["b"], s["c"]) == (1, 1) and s["acted"] == 1 and s["acted_net"] == 1


def test_a_pair_where_on_acted_counts_in_the_acted_net_and_a_pair_without_action_does_not():
    acted_up = {"on": "pass", "off": "fail", "acted": True}
    quiet_up = {"on": "pass", "off": "fail", "acted": False}
    acted_down = {"on": "fail", "off": "pass", "acted": True}
    s = paired_stats([acted_up, acted_up, quiet_up, quiet_up, quiet_up, acted_down])
    assert s["net"] == 4 and (s["b"], s["c"]) == (5, 1) and s["acted"] == 3 and (s["b_act"], s["c_act"], s["acted_net"]) == (2, 1, 1)


# ---- the rule ----

def stats(valid=24, net=0, acted_net=0):
    return {"valid": valid, "net": net, "acted_net": acted_net}


def test_the_four_outcomes_and_the_noise_case():
    assert rule_outcome(stats(net=3, acted_net=3), 0)[0] == "benefit suggested"
    assert rule_outcome(stats(net=5, acted_net=2), 0)[0] == "noise"  # the global difference is not from the pairs where the step acted
    assert rule_outcome(stats(net=3, acted_net=3), 1)[0] == "null"  # a control triggered
    assert rule_outcome(stats(net=2, acted_net=2), 0)[0] == "null"
    assert rule_outcome(stats(net=-3, acted_net=-3), 0)[0] == "harm suggested"
    assert "NOT from pairs" in rule_outcome(stats(net=-4, acted_net=0), 0)[1]  # the harm that does not come from where the step acted says so
    assert rule_outcome(stats(valid=19, net=6, acted_net=6), 0)[0] == "inconclusive"


# ---- the report, from files ----

def acted(verdict: str, kind: str = "retry_kb") -> tuple:
    return (verdict, [kind])


def test_three_acted_fail_to_pass_pairs_and_no_control_trigger_suggest_a_benefit(tmp_path):
    on = {"p01_reprint_receipt": [acted("pass"), "pass"], "p02_close_day_sales": [acted("pass"), "pass"], "p04_pix_fee": [acted("pass", "add_support"), "pass"]}
    off = {"p01_reprint_receipt": ["fail", "pass"], "p02_close_day_sales": ["fail", "pass"], "p04_pix_fee": ["fail", "pass"]}
    text = text_of(tmp_path, on, off)
    assert "Outcome of the preregistered rule: benefit suggested." in text
    assert "valid: 24" in text and "**Net +3**" in text and "**acted-net +3**" in text and "false triggers (a control row of the ON arm with a replan entry): 0" in text
    assert "exact two-sided sign test on those discordant acted pairs (3): p = 0.2500" in text


def test_the_same_net_from_pairs_without_action_is_reported_as_noise(tmp_path):
    on = {"p01_reprint_receipt": ["pass", "pass"], "p02_close_day_sales": ["pass", "pass"], "p04_pix_fee": ["pass", "pass"], "p05_pix_savings_account": [acted("pass"), "pass"]}
    off = {"p01_reprint_receipt": ["fail", "pass"], "p02_close_day_sales": ["fail", "pass"], "p04_pix_fee": ["fail", "pass"], "p05_pix_savings_account": ["fail", "pass"]}
    text = text_of(tmp_path, on, off)
    assert "Outcome of the preregistered rule: noise." in text and "**Net +4**" in text and "**acted-net +1**" in text
    assert "mostly from pairs where the step did not act" in text


def test_a_control_that_triggers_blocks_the_benefit_and_is_named(tmp_path):
    on = {"p01_reprint_receipt": [acted("pass"), "pass"], "p02_close_day_sales": [acted("pass"), "pass"], "p04_pix_fee": [acted("pass"), "pass"],
          "c02_biggest_sale_yesterday": ["pass", acted("pass", "retry_kb")]}
    off = {"p01_reprint_receipt": ["fail", "pass"], "p02_close_day_sales": ["fail", "pass"], "p04_pix_fee": ["fail", "pass"]}
    text = text_of(tmp_path, on, off)
    assert "Outcome of the preregistered rule: null." in text and "1 control row(s)" in text
    assert "`c02_biggest_sale_yesterday` repetition 2: retry_kb" in text


def test_judge_errors_drop_pairs_and_below_twenty_valid_the_rule_says_inconclusive(tmp_path):
    on = {c: [("error", []), "pass"] for c in IDS[:6]}
    off = {}
    text = text_of(tmp_path, on, off)
    assert "valid: 18" in text and "dropped: 6" in text and "`p01_reprint_receipt` repetition 1: ON error, OFF pass" in text
    assert "Outcome of the preregistered rule: inconclusive." in text


def test_harm_the_by_group_table_the_b1_plus_b2_subtotal_and_the_noise_baseline(tmp_path):
    on = {"p07_printer_not_printing": [acted("fail", "escalate"), "fail"], "p08_software_update": [acted("fail", "offer_ticket"), "fail"], "p09_reinicia_sozinha": ["fail", "pass"],
          "p03_antecipar_na_maquininha": ["pass", "pass"]}
    off = {"p07_printer_not_printing": ["pass", "pass"], "p08_software_update": ["pass", "pass"], "p09_reinicia_sozinha": ["pass", "pass"]}
    text = text_of(tmp_path, on, off)
    assert "Outcome of the preregistered rule: harm suggested." in text and "**Net -5**" in text
    assert "| B3 | 6 | 6 | 1 | 6 | -5 | 0 | 5 | 2 |" in text  # 6 B3 pairs: ON 1 pass, OFF 6 pass, 5 pass->fail, 2 where ON acted
    assert "| B1 + B2 (without the bias of B3) | 12 | 12 | 12 | 12 | +0 | 0 | 0 | 0 |" in text
    assert "B3 favors the ON arm by construction" in text and "| B3 (offer_ticket, escalate) | 1 of 3 | 0 of 3 |" not in text  # the firing table counts the target entries only
    assert "| B3 (offer_ticket, escalate) | 2 of 3 | 0 of 3 |" in text
    # OFF 1 against OFF 2: p09 is pass/pass in both here, so no case differs
    assert "**0 get a different one**" in text


def test_the_noise_baseline_counts_the_cases_that_change_between_the_two_off_runs(tmp_path):
    off = {"p01_reprint_receipt": ["pass", "fail"], "p04_pix_fee": ["fail", "pass"], "c01_payout_status": ["pass", "pass"]}
    text = text_of(tmp_path, {}, off)
    assert "12 cases have a verdict in both, **2 get a different one**" in text
    assert "- `p01_reprint_receipt`: OFF 1 pass, OFF 2 fail" in text and "- `p04_pix_fee`: OFF 1 fail, OFF 2 pass" in text


def test_cost_latency_and_handoffs_per_arm(tmp_path):
    on, off = pair_runs(tmp_path, {}, {})
    for run in on:
        for r in run["rows"].values():
            r["usage"]["cost_usd"], r["latency_ms"] = 0.004, 9000
        run["rows"]["p07_printer_not_printing"]["handoff"] = {"id": "HND-1"}
    text = build_paired(on, off, GROUPS)
    assert "| ON | 24 | $0.00400 | $0.09600 | 9,000 | 9,000 | 2 |" in text and "| OFF | 24 | $0.00200 | $0.04800 | 5,000 | 5,000 | 0 |" in text


def test_the_same_inputs_give_the_same_bytes_and_the_cli_writes_the_file(tmp_path):
    on, off = pair_runs(tmp_path, {"p01_reprint_receipt": [acted("pass"), "pass"]}, {"p01_reprint_receipt": ["fail", "pass"]})
    assert build_paired(on, off, GROUPS) == build_paired(on, off, GROUPS)
    out = tmp_path / "REPLAN-PROBE-test.md"
    args = ["--paired", "--groups", str(REPO / "eval" / "probe" / "replan_probe_groups.json"), "--candidate", *[str(tmp_path / f"on{k}.judged.json") for k in (1, 2)],
            "--baseline", *[str(tmp_path / f"off{k}.judged.json") for k in (1, 2)], "--out", str(out)]
    assert main(args) == 0 and out.read_text(encoding="utf-8").startswith("# Replan probe: ON runs against OFF runs, paired (scripted)")
    assert "\r" not in out.read_text(encoding="utf-8") and "202" not in out.read_text(encoding="utf-8").split("\n")[0]  # no timestamp in the title


def test_it_refuses_what_it_cannot_pair(tmp_path):
    on, off = pair_runs(tmp_path, {}, {})
    with pytest.raises(SystemExit, match="same number"):
        build_paired(on, off[:1], GROUPS)
    with pytest.raises(SystemExit, match="do not name the same cases"):
        build_paired(on, off, {**GROUPS, "control": GROUPS["control"][:2]})
    with pytest.raises(SystemExit, match="needs --groups"):
        main(["--paired", "--candidate", str(tmp_path / "on1.judged.json"), "--baseline", str(tmp_path / "off1.judged.json"), "--out", str(tmp_path / "x.md")])
    bare = write(tmp_path, "bare.json", {i: {k: v for k, v in row("pass").items() if k != "replans"} for i in IDS})
    with pytest.raises(SystemExit, match="no row of bare.json has a 'replans'"):
        build_paired([bare, bare], off, GROUPS)


def test_the_groups_by_what_the_on_row_did_split_the_non_control_pairs_and_keep_the_controls_apart(tmp_path):
    on = {"p01_reprint_receipt": [acted("pass"), "pass"], "p04_pix_fee": [acted("fail", "add_support"), "pass"], "p07_printer_not_printing": ["fail", "pass"],
          "c03_box_contents": [acted("pass"), "pass"]}
    off = {"p01_reprint_receipt": ["fail", "pass"], "p04_pix_fee": ["pass", "pass"], "p07_printer_not_printing": ["pass", "pass"], "c03_box_contents": ["pass", "pass"]}
    text = text_of(tmp_path, on, off)
    assert "## By what the ON row did (amendment 1 of the preregistration)" in text and "**The main reading is the `ON acted` line**" in text
    assert "| ON acted | 2 | 2 | 1 | 1 | +0 | 1 | 1 | add_support 1, retry_kb 1 |" in text  # p01 (retry_kb, fail -> pass) and p04 (add_support, pass -> fail); no control in it
    assert "| ON did not act | 16 | 16 | 15 | 16 | -1 | 0 | 1 | none |" in text  # the 18 other non-control pairs minus the 2 above: p07 repetition 1 went pass -> fail
    assert "| controls | 6 | 6 | 6 | 6 | +0 | 0 | 0 | retry_kb 1 |" in text  # a control that acted stays in the controls, not in `ON acted`
