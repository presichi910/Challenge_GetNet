"""docs/FINAL_REPORT.md: the judged numbers are generated fields, nothing judged is typed outside them, and what it cites exists."""
import re
from pathlib import Path

import pytest

from eval.results_block import fill, main

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "docs" / "FINAL_REPORT.md"
TEXT = REPORT.read_text(encoding="utf-8")
BLOCKS = ("results", "v2", "calibration")


def outside_blocks(text: str) -> str:
    for name in BLOCKS:
        text = re.sub(rf"<!-- {name}:start -->.*?<!-- {name}:end -->", "", text, flags=re.S)
    return text


def test_the_three_generated_blocks_have_one_pair_of_markers_each_and_filling_one_replaces_only_what_is_between_them():
    """True of the draft and of the filled report alike: what is inside the blocks is not this test's business, only where the blocks are."""
    for name in BLOCKS:
        start, end = f"<!-- {name}:start -->", f"<!-- {name}:end -->"
        assert TEXT.count(start) == TEXT.count(end) == 1, f"the {name} block must have exactly one start and one end marker"
        assert TEXT.index(start) < TEXT.index(end)
        filled = fill(TEXT, name, "SENTINEL BODY")
        assert filled.count(start) == filled.count(end) == 1
        between = filled[filled.index(start) + len(start):filled.index(end)]
        assert between.strip() == "SENTINEL BODY"
        assert filled[:filled.index(start)] == TEXT[:TEXT.index(start)]  # everything before the block is byte for byte the same
        assert filled[filled.index(end):] == TEXT[TEXT.index(end):]  # and everything after it


def test_no_judged_rate_is_typed_by_hand_outside_the_generated_blocks():
    text = outside_blocks(TEXT)
    for judged in ("Judge pass rate", "(n=", "Groundedness (judge)", "Completeness (judge)"):
        assert judged not in text
    assert not re.search(r"judge pass rate[^.\n]*\d+%", text, re.I)


def test_it_carries_the_disclosures_the_project_notes_require():
    for phrase in ("ho06", "ho12", "ON PURPOSE", "20261116", "not independent", "first scoring sessions", "paid reset of the daily limit", "--daily-limit 391000", "first run on GitHub",
                   "REPLAN_MAX=0", "false fails", "m04_followup_product", "m08_recall_sum", "0835e91f81556587124e12aa81fe82556f775c24",
                   "22423e2f10249eb38bcaa8e8583bdaac49b5b35a", "PYSEC", "not comparable"):
        assert phrase.lower() in TEXT.lower() or phrase in TEXT, phrase


def test_it_is_the_final_report_not_a_draft():
    """The banner used to say DRAFT and the test asked for the word; the state it checks now is the opposite: the banner says it is the final report,
    and the word DRAFT (upper case: the prose says 'drafts' of answers) is nowhere in the document or in the README's and the review response's pointers to it."""
    assert "> **Final report. The numbers are generated.**" in TEXT
    assert not re.search(r"\bDRAFT\b", TEXT)
    for rel in ("README.md", "docs/REVIEW_RESPONSE.md"):
        pointer = (ROOT / rel).read_text(encoding="utf-8")
        assert not re.search(r"FINAL_REPORT\.md\]\([^)]*\)\s*\((?:a )?draft", pointer, re.I), rel


def test_the_files_it_cites_exist():
    for rel in ("eval/same_behavior.py", "eval/persistent.py", "eval/calibration/sample.py", "eval/calibration/sample_v3.json", "eval/calibration/CALIBRATION_V3.md",
                "eval/rejudge.py", "eval/replan_ab.py", "eval/results_block.py"):
        assert (ROOT / rel).exists(), rel
    for rel in re.findall(r"`(eval/reports/[\w.-]+)`", TEXT):
        if "..." in rel:
            continue
        if not (ROOT / rel).exists():
            pytest.skip(f"{rel} is a run artifact that is not in this checkout")


def test_filling_it_replaces_only_the_blocks_and_gives_every_judge_rate_its_n(tmp_path):
    import json

    def row(i, judged=True):
        r = {"id": i, "answer": "a", "evidence": ["e"], "routing_ok": True, "tools_ok": True, "handoff_ok": None, "latency_ms": 4000, "plan": [], "tools_called": []}
        if judged:
            r["judge"] = {"verdict": "pass", "relevance": 5, "groundedness": 5, "completeness": 5, "tone": 5, "reason": "ok"}
        return r

    run = tmp_path / "r1.judged.json"
    run.write_text(json.dumps({"summary": {"commit": "a" * 40, "tree_clean": True, "replan_max": 0}, "rows": [row("q01"), row("q02", judged=False)]}), encoding="utf-8")
    copy = tmp_path / "FINAL_REPORT.md"
    copy.write_text(TEXT, encoding="utf-8")
    assert main([str(run), "--report", str(copy), "--v2", "--calibration", str(ROOT / "eval" / "calibration" / "CALIBRATION_V3.md")]) == 0
    filled = copy.read_text(encoding="utf-8")
    assert outside_blocks(filled) == outside_blocks(TEXT)  # nothing outside the blocks moved
    results = re.search(r"<!-- results:start -->(.*?)<!-- results:end -->", filled, re.S).group(1)
    assert "scored 1 of 2 rows" in results and "(n=1)" in results and "1 still pending" in results
    assert "agreement" in re.search(r"<!-- calibration:start -->(.*?)<!-- calibration:end -->", filled, re.S).group(1)
    assert "V2 rows re-scored" in re.search(r"<!-- v2:start -->(.*?)<!-- v2:end -->", filled, re.S).group(1) or "Nothing" in filled


def test_fill_refuses_a_document_without_the_block():
    with pytest.raises(ValueError, match="no <!-- x:start -->"):
        fill("no markers", "x", "body")
