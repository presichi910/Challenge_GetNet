"""eval/compare_runs.py: a scripted, deterministic comparison of one judged run with baseline runs. Synthetic files only, except one
regeneration check on the real files (skipped when any input is missing). No judge, no model, no network."""
import json
from pathlib import Path

import pytest

from eval.compare_runs import build, load_run, main

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "eval" / "reports"


def row(cid, verdict="pass", replans=None, g=5, c=5):
    r = {"id": cid, "answer": "a", "replans": replans or []}
    if verdict is not None:
        r["judge"] = {"verdict": verdict, "groundedness": g, "completeness": c}
    return r


def write(tmp_path, name, rows, **summary):
    p = tmp_path / name
    p.write_text(json.dumps({"summary": {"commit": "a" * 40, "tree_clean": True, **summary}, "rows": rows}), encoding="utf-8")
    return p


IDS = ["q01", "q02", "q03", "q04"]


def runs(tmp_path):
    cand = write(tmp_path, "cand.judged.json", [row("q01", "pass"), row("q02", "fail", ["retry_kb: 1 unsupported -> 5 passages -> 0 still unsupported"], g=3, c=2),
                                                row("q03", "pass"), row("q04", "fail", ["retry_kb: x", "add_support: y"]), row("ho01", "fail")], replan_max=2, label="on")
    b1 = write(tmp_path, "b1.judged.json", [row("q01", "pass"), row("q02", "pass"), row("q03", "fail"), row("q04", "pass"), row("ho01", "pass")], replan_max=0)
    b2 = write(tmp_path, "b2.judged.json", [row("q01", "pass"), row("q02", "pass"), row("q03", "pass"), row("q04", "pass"), row("ho01", "pass")], replan_max=0)
    return cand, [b1, b2]


def table_row(text, cid):
    return next(line for line in text.splitlines() if line.startswith(f"| {cid} |"))


def test_the_verdict_counts_of_the_candidate_and_of_each_baseline_are_right_and_the_holdout_is_left_out(tmp_path):
    cand, bases = runs(tmp_path)
    text = build(load_run(cand), [load_run(b) for b in bases])
    assert "| candidate | 2 | 2 | 0 | 0 |" in text  # 2 pass, 2 fail (ho01 is not counted)
    assert "| baseline 1 | 3 | 1 | 0 | 0 |" in text
    assert "| baseline 2 | 4 | 0 | 0 | 0 |" in text
    assert "ho01" not in text.replace("`ho`", "")  # ids that start with "ho" are not compared
    assert "4 regular rows" in text


def test_k_of_n_and_the_scores_are_per_case(tmp_path):
    cand, bases = runs(tmp_path)
    text = build(load_run(cand), [load_run(b) for b in bases])
    assert table_row(text, "q01") == "| q01 | pass | 2 of 2 | no | 5 | 5 |"
    assert table_row(text, "q02") == "| q02 | fail | 2 of 2 | yes: retry_kb | 3 | 2 |"
    assert table_row(text, "q03") == "| q03 | pass | 1 of 2 | no | 5 | 5 |"
    assert table_row(text, "q04") == "| q04 | fail | 2 of 2 | yes: retry_kb, add_support | 5 | 5 |"


def test_differences_are_split_by_whether_the_replan_fired_and_rows_with_split_baselines_are_not_listed(tmp_path):
    cand, bases = runs(tmp_path)
    text = build(load_run(cand), [load_run(b) for b in bases])
    fired = text.split("### Replan fired")[1].split("### Replan did not fire")[0]
    not_fired = text.split("### Replan did not fire")[1].split("## How to read")[0]
    assert "`q02`" in fired and "`q04`" in fired and "`q03`" not in fired
    assert "- none" in not_fired and "`q03`" not in not_fired  # q03: the baselines disagree (1 of 2), so there is no unanimous verdict
    assert "### Replan fired (2)" in text and "### Replan did not fire (0)" in text


def test_a_difference_where_the_replan_did_not_fire_is_listed_apart_and_the_fixed_paragraph_is_there(tmp_path):
    cand = write(tmp_path, "c.judged.json", [row("q01", "fail"), row("q02", "pass", ["retry_kb: a"])])
    base = write(tmp_path, "b.judged.json", [row("q01", "pass"), row("q02", "pass")])
    text = build(load_run(cand), [load_run(base)])
    assert "### Replan did not fire (1)" in text and "### Replan fired (0)" in text
    assert "- `q01`: candidate fail, baselines unanimous pass (1 of 1 passed)" in text.split("### Replan did not fire")[1]
    assert "cannot be attributed to the replan" in text and "ONE judged repetition" in text


def test_no_interpretation_words_in_the_output(tmp_path):
    cand, bases = runs(tmp_path)
    text = build(load_run(cand), [load_run(b) for b in bases]).lower()
    for word in ("better", "worse", "improve", "regress", "degrad"):
        assert word not in text


def test_ids_that_do_not_match_fail_with_a_clear_message(tmp_path):
    cand = write(tmp_path, "c.judged.json", [row("q01"), row("q02")])
    base = write(tmp_path, "b.judged.json", [row("q01"), row("q03")])
    with pytest.raises(SystemExit) as e:
        build(load_run(cand), [load_run(base)])
    msg = str(e.value)
    assert "b.judged.json" in msg and "only in the candidate: ['q02']" in msg and "only in the baseline: ['q03']" in msg


def test_a_candidate_without_replans_fields_is_refused(tmp_path):
    rows = [{"id": "q01", "judge": {"verdict": "pass"}}]
    cand = write(tmp_path, "c.judged.json", rows)
    base = write(tmp_path, "b.judged.json", rows)
    with pytest.raises(SystemExit, match="cannot tell where the replan fired"):
        build(load_run(cand), [load_run(base)])


def test_errors_and_unscored_rows_are_counted_apart_and_never_as_a_verdict(tmp_path):
    cand = write(tmp_path, "c.judged.json", [row("q01", "error"), row("q02", None)])
    base = write(tmp_path, "b.judged.json", [row("q01", "pass"), row("q02", "error")])
    text = build(load_run(cand), [load_run(base)])
    assert "| candidate | 0 | 0 | 1 | 1 |" in text and "| baseline 1 | 1 | 0 | 1 | 0 |" in text
    assert "(1 without verdict)" in table_row(text, "q02")
    assert "- none" in text.split("### Replan fired")[1]  # an error or an unscored row is never a difference


def test_two_runs_of_the_script_give_the_same_bytes_and_the_cli_writes_the_file(tmp_path):
    cand, bases = runs(tmp_path)
    out1, out2 = tmp_path / "one.md", tmp_path / "two.md"
    args = ["--candidate", str(cand), "--baseline", *map(str, bases)]
    assert main([*args, "--out", str(out1)]) == 0 and main([*args, "--out", str(out2)]) == 0
    assert out1.read_bytes() == out2.read_bytes()
    assert out1.read_text(encoding="utf-8").startswith("# Candidate run against baseline runs")


# ---- the real files: regenerating the committed report must give exactly its content ----

CANDIDATES = [REPORTS / f"{s}.judged.json" for s in ("20261116-234030", "20261116-234325", "20261116-234617")]
BASELINES = [REPORTS / f"{s}.judged.json" for s in ("20261115-202155", "20261115-202732", "20261115-203324", "20261115-203912", "20261115-204526")]
COMMITTED = REPORTS / "REPLAN-AB-20261116-234030.md"


def test_the_committed_report_is_what_the_script_gives_for_the_real_files(tmp_path):
    missing = [p.name for p in [*CANDIDATES, *BASELINES, COMMITTED] if not p.exists()]
    if missing:
        pytest.skip(f"not in this checkout: {missing}")
    out = tmp_path / "regenerated.md"
    assert main(["--candidate", *map(str, CANDIDATES), "--baseline", *map(str, BASELINES), "--out", str(out)]) == 0
    assert out.read_bytes() == COMMITTED.read_bytes()


# ---- the headline line ----

def headline_of(text):
    lines = [line for line in text.splitlines() if line.startswith("**Headline:**")]
    assert len(lines) == 1
    return lines[0]


def test_the_headline_has_the_exact_format_and_sits_before_the_counts_table(tmp_path):
    cand, bases = runs(tmp_path)  # candidate: 2 pass of 4; baselines: 3 and 4 pass; replan fired in 2 rows
    text = build(load_run(cand), [load_run(b) for b in bases])
    assert headline_of(text) == ("**Headline:** candidate 2 pass of 4 regular rows; baseline repetitions 3-4 pass of 4; "
                                 "the candidate count is outside that range; replan fired in 2 of 4 rows.")
    assert text.index("**Headline:**") < text.index("## Verdict counts")
    line = headline_of(text).lower()
    assert "%" not in line and "judge pass rate" not in line
    for word in ("better", "worse", "improve", "regress"):
        assert word not in line


def test_the_headline_says_inside_when_the_candidate_count_is_in_the_range_ends_included(tmp_path):
    cand = write(tmp_path, "c.judged.json", [row("q01", "pass"), row("q02", "fail"), row("q03", "pass"), row("q04", "fail")])
    low = write(tmp_path, "l.judged.json", [row("q01", "pass"), row("q02", "fail"), row("q03", "fail"), row("q04", "fail")])
    high = write(tmp_path, "h.judged.json", [row("q01", "pass"), row("q02", "pass"), row("q03", "pass"), row("q04", "fail")])
    text = build(load_run(cand), [load_run(low), load_run(high)])
    assert headline_of(text) == ("**Headline:** candidate 2 pass of 4 regular rows; baseline repetitions 1-3 pass of 4; "
                                 "the candidate count is inside that range; replan fired in 0 of 4 rows.")
    edge = build(load_run(cand), [load_run(high)])  # one baseline with 3 passes: the range is 3-3, so 2 is outside
    assert "baseline repetitions 3-3 pass of 4; the candidate count is outside that range" in headline_of(edge)
    same = build(load_run(cand), [load_run(cand)])
    assert "baseline repetitions 2-2 pass of 4; the candidate count is inside that range" in headline_of(same)


def test_an_error_or_an_unscored_row_is_not_counted_as_a_pass_in_the_headline(tmp_path):
    cand = write(tmp_path, "c.judged.json", [row("q01", "pass"), row("q02", "error"), row("q03", None)])
    base = write(tmp_path, "b.judged.json", [row("q01", "pass"), row("q02", "pass"), row("q03", "pass")])
    assert "candidate 1 pass of 3 regular rows; baseline repetitions 3-3 pass of 3" in headline_of(build(load_run(cand), [load_run(base)]))


# ---- the documents that quote the headline carry it exactly, as the script wrote it ----

QUOTING_DOCS = ("docs/REVIEW_RESPONSE.md", "docs/REPLAN_DEMO.md", "docs/FINAL_REPORT.md")


def test_the_documents_that_quote_the_headlines_contain_exactly_every_line_of_the_report():
    if not COMMITTED.exists():
        pytest.skip(f"not in this checkout: {COMMITTED.name}")
    lines = [line for line in COMMITTED.read_text(encoding="utf-8").splitlines() if line.startswith("**Headline:**")]
    assert len(lines) == len(CANDIDATES), "the committed report must have one Headline line per candidate repetition"
    for doc in QUOTING_DOCS:
        text = (ROOT / doc).read_text(encoding="utf-8")
        for line in lines:
            assert line in text, f"{doc} does not contain the Headline line {line!r} of {COMMITTED.name}"


def timed(cid, verdict, ms, usd, replans=None):
    r = row(cid, verdict, replans)
    r["latency_ms"], r["usage"] = ms, {"cost_usd": usd}
    return r


def test_the_cost_table_lists_only_the_rows_where_the_replan_fired_with_the_baseline_mean(tmp_path):
    cand = write(tmp_path, "c.judged.json", [timed("q01", "pass", 9000, 0.004, ["retry_kb: a"]), timed("q02", "pass", 1000, 0.001)])
    b1 = write(tmp_path, "b1.judged.json", [timed("q01", "pass", 3000, 0.001), timed("q02", "pass", 1000, 0.001)])
    b2 = write(tmp_path, "b2.judged.json", [timed("q01", "pass", 5000, 0.002), timed("q02", "pass", 1000, 0.001)])
    section = build(load_run(cand), [load_run(b1), load_run(b2)]).split("## Latency and agent cost")[1].split("## How to read")[0]
    assert "| q01 | retry_kb | 9,000 | 4,000 | $0.00400 | $0.00150 |" in section
    assert "q02" not in section


def test_the_cost_table_says_none_when_nothing_fired(tmp_path):
    cand = write(tmp_path, "c.judged.json", [row("q01")])
    base = write(tmp_path, "b.judged.json", [row("q01")])
    assert "| none |" in build(load_run(cand), [load_run(base)]).split("## Latency and agent cost")[1]


# ---- several candidates in one report ----

def many(tmp_path):
    c1 = write(tmp_path, "c1.judged.json", [timed("q01", "pass", 3000, 0.001), timed("q02", "fail", 9000, 0.004, ["retry_kb: a", "add_support: b"]), timed("q03", "pass", 1000, 0.001)], replan_max=2, label="on")
    c2 = write(tmp_path, "c2.judged.json", [timed("q01", "fail", 3100, 0.001, ["retry_kb: a", "offer_ticket: b"]), timed("q02", "pass", 8000, 0.003, ["retry_kb: a", "add_support: b"]), timed("q03", "pass", 1100, 0.001)], replan_max=2, label="on")
    b1 = write(tmp_path, "b1.judged.json", [timed("q01", "pass", 3000, 0.001), timed("q02", "fail", 2000, 0.001), timed("q03", "pass", 1000, 0.001)], replan_max=0)
    b2 = write(tmp_path, "b2.judged.json", [timed("q01", "pass", 3000, 0.001), timed("q02", "fail", 4000, 0.002), timed("q03", "pass", 1000, 0.001)], replan_max=0)
    return [c1, c2], [b1, b2]


def lines_with(text, prefix):
    return [line for line in text.splitlines() if line.startswith(prefix)]


def built(tmp_path):
    from eval.compare_runs import build_many

    cands, bases = many(tmp_path)
    return build_many([load_run(c) for c in cands], [load_run(b) for b in bases])


def test_several_candidates_give_one_report_with_a_headline_per_candidate_in_the_same_exact_format(tmp_path):
    text = built(tmp_path)
    assert text.startswith("# 2 candidate repetitions against 2 baseline repetitions")
    assert lines_with(text, "**Headline:**") == [
        "**Headline:** candidate 2 pass of 3 regular rows; baseline repetitions 2-2 pass of 3; the candidate count is inside that range; replan fired in 1 of 3 rows.",
        "**Headline:** candidate 2 pass of 3 regular rows; baseline repetitions 2-2 pass of 3; the candidate count is inside that range; replan fired in 2 of 3 rows.",
    ]
    assert text.count("## Verdict counts") == 1 and "| candidate 1 | 2 | 1 | 0 | 0 |" in text and "| candidate 2 | 2 | 1 | 0 | 0 |" in text and "| baseline 2 | 2 | 1 | 0 | 0 |" in text


def test_the_per_case_table_has_a_verdict_and_a_replan_column_per_candidate_and_the_baselines_k_of_n(tmp_path):
    text = built(tmp_path)
    assert table_row(text, "q01") == "| q01 | pass | fail | 2 of 2 | no | yes: retry_kb, offer_ticket | 5 / 5 | 5 / 5 |"
    assert table_row(text, "q02").startswith("| q02 | fail | pass | 0 of 2 | yes: retry_kb, add_support | yes: retry_kb, add_support |")


def test_differences_per_candidate_are_split_by_whether_the_replan_fired(tmp_path):
    text = built(tmp_path)
    c1 = text.split("\n## Candidate 1:")[1].split("\n## Candidate 2:")[0]
    c2 = text.split("\n## Candidate 2:")[1].split("\n## Rows where the replan fired in at least one candidate")[0]
    assert "### Candidate 1: replan fired (0)" in c1 and "### Candidate 1: replan did not fire (0)" in c1
    assert "### Candidate 2: replan fired (2)" in c2 and "`q01`: candidate fail, baselines unanimous pass" in c2 and "`q02`: candidate pass, baselines unanimous fail" in c2
    assert "### Candidate 2: replan did not fire (0)" in c2


def test_the_table_of_rows_where_the_replan_fired_in_at_least_one_candidate_has_verdicts_k_of_n_latency_and_cost(tmp_path):
    section = built(tmp_path).split("## Rows where the replan fired in at least one candidate")[1].split("## How to read")[0]
    assert "| q01 | no | yes: retry_kb, offer_ticket | pass | fail | 2 of 2 |" in section
    assert "| q02 | yes: retry_kb, add_support | yes: retry_kb, add_support | fail | pass | 0 of 2 |" in section
    assert "q03" not in section
    assert "| q02 | 9,000 | 8,000 | 3,000 | $0.00400 | $0.00300 | $0.00150 |" in section


def test_the_fixed_paragraphs_say_no_attribution_without_a_replan_and_no_statistical_inference(tmp_path):
    text = built(tmp_path)
    assert "cannot be attributed to the replan" in text
    assert "With 2 repetitions with the replan on against 2 base repetitions and a judge that is not deterministic, this report makes no statistical inference" in text
    low = text.lower()
    for word in ("better", "worse", "improve", "regress", "judge pass rate", "%"):
        assert word not in low


def test_candidates_whose_ids_do_not_match_the_baselines_fail_and_the_cli_takes_both_syntaxes(tmp_path):
    from eval.compare_runs import build_many

    cands, bases = many(tmp_path)
    odd = write(tmp_path, "odd.judged.json", [timed("q01", "pass", 1, 0.1), timed("q09", "pass", 1, 0.1)])
    with pytest.raises(SystemExit, match="odd.judged.json"):
        build_many([load_run(cands[0]), load_run(odd)], [load_run(b) for b in bases])
    out1, out2, out3 = tmp_path / "1.md", tmp_path / "2.md", tmp_path / "3.md"
    assert main(["--candidate", *map(str, cands), "--baseline", *map(str, bases), "--out", str(out1)]) == 0
    assert main(["--candidate", str(cands[0]), "--candidate", str(cands[1]), "--baseline", *map(str, bases), "--out", str(out2)]) == 0
    assert out1.read_bytes() == out2.read_bytes()
    assert main(["--candidate", str(cands[0]), "--baseline", *map(str, bases), "--out", str(out3)]) == 0  # one candidate: the single report, as before
    assert out3.read_text(encoding="utf-8").startswith("# Candidate run against baseline runs")
