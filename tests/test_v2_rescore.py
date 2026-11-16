"""The V2 re-score: 19 of the 27 rows, chosen from the ids alone with a seed fixed beforehand; three verdicts on the same rows, each with its n."""
import inspect
import json

import pytest

from eval.calibration import v2_rescore as mod
from eval.calibration.v2_rescore import SEED, build, choose, estimate_tokens, kappa, prepare, render, source_rows

IDS = [r["id"] for r in source_rows()]
# the draw as it was made and written in CLAUDE.md before anything ran: if this list changes, the seed or the way of choosing changed
FIXED_19 = ["q02_weather", "q04_pix_bank_account", "q05_terminal_offline", "q06_receivables_advance", "q08_decline_error", "q09_installments",
            "q10_payment_link_whatsapp", "x01_portuguese", "x02_greeting", "x03_unknown_customer", "x04_cross_customer_attempt", "h01_human_request",
            "m01_recall_color", "m02_recall_which_bigger", "m03_rephrase_previous", "m04_followup_product", "m05_followup_payout",
            "ho03_advance_enabled_and_how", "ho04_human_request_es"]


def test_the_draw_is_the_one_written_down_before_anything_ran():
    assert SEED == 20261116 and len(IDS) == 27 and choose(IDS) == FIXED_19


def test_it_is_nineteen_distinct_rows_in_the_order_of_the_source_and_the_same_every_time():
    a, b = choose(IDS), choose(list(reversed(IDS)))
    assert len(a) == len(set(a)) == 19 and set(a) <= set(IDS)
    assert a == [i for i in IDS if i in set(a)]  # the source order
    assert set(a) == set(b) == set(choose(IDS))  # the order the ids arrive in does not change which rows are drawn
    assert set(choose(IDS, seed=1)) != set(a)  # the seed is what decides


def test_the_choice_reads_nothing_but_the_ids_no_verdict_no_label_no_score():
    import ast
    import textwrap

    fn = ast.parse(textwrap.dedent(inspect.getsource(mod.choose))).body[0]
    body = fn.body[1:] if isinstance(fn.body[0], ast.Expr) and isinstance(getattr(fn.body[0], "value", None), ast.Constant) else fn.body  # without the docstring
    words = {n.id for stmt in body for n in ast.walk(stmt) if isinstance(n, ast.Name)} | {n.attr for stmt in body for n in ast.walk(stmt) if isinstance(n, ast.Attribute)}
    assert not {w for w in words if any(x in w.lower() for x in ("judge", "verdict", "human", "label", "score", "pass", "csv", "json", "read"))}, words
    assert list(inspect.signature(mod.choose).parameters) == ["ids", "k", "seed"]  # it cannot even be handed a result


def test_the_prepared_input_has_the_nineteen_rows_with_answer_and_evidence_and_no_old_verdict(tmp_path):
    out = prepare(FIXED_19, out=tmp_path / "in.json")
    data = json.loads(out.read_text(encoding="utf-8"))
    assert [r["id"] for r in data["rows"]] == FIXED_19
    assert all("judge" not in r and "judge_usage" not in r and r["answer"] and "evidence" in r for r in data["rows"])
    assert data["summary"]["seed"] == SEED and "CURRENT judge" in data["summary"]["label"]


def test_the_estimated_cost_is_a_plausible_number_of_tokens_for_nineteen_rows():
    from eval.run_eval import load_cases

    rows = [r for r in source_rows() if r["id"] in set(FIXED_19)]
    est = estimate_tokens(rows, {c["id"]: c for c in load_cases(None)})
    assert 20_000 < est < 60_000 and est / 19 < 3_000  # ~1.9K per row


# ---------------- the report ----------------

def old(verdict):
    return {"judge": {"verdict": verdict}}


IDS3 = ["a", "b", "c", "d"]


def numbers(old_v, new_v, human_v, scored=("a", "b", "c", "d")):
    olds = {i: old(v) for i, v in zip(IDS3, old_v)}
    news = {i: {"judge": {"verdict": v}} for i, v in zip(IDS3, new_v) if i in scored}
    return build(olds, news, dict(zip(IDS3, human_v)), IDS3, IDS3 + ["z"] if False else IDS3)


def test_three_pass_rates_on_the_same_rows_each_with_its_n():
    r = numbers(["pass", "pass", "fail", "fail"], ["pass", "fail", "fail", "pass"], [True, False, False, True])
    assert (r["n_selected"], r["n_rescored"]) == (4, 4)
    assert (r["old_same"], r["new_same"], r["human_same"]) == (2, 2, 2)
    assert (r["agree_old"], r["agree_new"]) == (2, 4) and r["changed"] == [("b", "pass", "fail"), ("d", "fail", "pass")]
    text = render(r)
    assert "| old judge (the one V2 was measured with) | 4 | 2/4 = 50% |" in text and "| **current judge** | 4 | **2/4 = 50%** |" in text
    assert "| human (V2 labels) | 4 | 2/4 = 50% |" in text and "| current | 4/4 = 100% |" in text and "b (pass -> fail)" in text


def test_with_a_partial_rescore_only_the_scored_rows_are_compared_and_the_report_says_how_many():
    r = numbers(["pass", "pass", "fail", "fail"], ["pass", "fail", "fail", "pass"], [True, False, False, True], scored=("a", "b"))
    assert r["n_rescored"] == 2 and r["pending"] == ["c", "d"] and (r["old_same"], r["new_same"], r["human_same"]) == (2, 1, 1)
    text = render(r)
    assert "Scored by the current judge so far: **2 of 4** (2 still pending)" in text and "| **current judge** | 2 | **1/2 = 50%** |" in text
    assert "on all 4 selected rows" in text and "a difference of one row is 50 points" in text


def test_a_judge_error_is_left_out_and_named_never_counted_as_a_fail():
    olds = {i: old("pass") for i in IDS3}
    news = {"a": {"judge": {"verdict": "pass"}}, "b": {"judge": {"verdict": "error"}}}
    r = build(olds, news, {i: True for i in IDS3}, IDS3, IDS3)
    assert r["n_rescored"] == 1 and r["judge_errors"] == ["b"] and r["pending"] == ["c", "d"]
    assert "1 judge error(s) left out" in render(r)


def test_the_report_always_says_v2_is_not_independent_and_discloses_the_draw():
    olds = {i: old("pass" if i != "d" else "fail") for i in IDS3 + ["z"]}
    r = build(olds, {}, {"a": True, "b": True, "c": True, "d": True, "z": True}, IDS3, IDS3 + ["z"])
    text = render(r)
    assert "ADJUSTED on" in text and "NOT independent" in text and "V3" in text
    assert "About the draw" in text and "3/4 = 75%" in text and "1/1 = 100%" in text and "seed was not changed" in text


def test_the_real_old_judge_numbers_match_the_published_calibration_report():
    """CALIBRATION_V2.md: the judge said pass on 16 of the 27 and the human on 17. The V2 run and the blind sheet must give the same."""
    import csv

    old_rows = {r["id"]: r for r in source_rows()}
    human = {r["id"]: r["human_pass"].strip().lower() == "yes" for r in csv.DictReader(mod.SHEET.open(encoding="utf-8"))}
    r = build(old_rows, {}, human, IDS, IDS)
    assert (r["old_all"], r["human_all"]) == (16, 17)


def test_kappa_on_known_values():
    assert kappa([(True, True), (False, False)]) == 1.0 and kappa([(True, False), (False, True)]) == -1.0
    assert kappa([(True, True), (True, True)]) is None and kappa([]) is None
    assert kappa([(True, True), (False, False), (True, False), (False, True)]) == pytest.approx(0.0)
