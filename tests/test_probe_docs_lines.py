"""The probe's numbers in the documents are not typed: every line inside a `<!-- probe:NAME:start -->` ... `<!-- probe:NAME:end -->` block of REPLAN_DEMO.md, REVIEW_RESPONSE.md and
EVIDENCE.md was copied by a script from the generated report eval/reports/REPLAN-PROBE-20261118-133249.md, and must still be, byte for byte, a line of that report.
Outside the blocks, the prose of the probe sections types no judge rate."""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "eval" / "reports" / "REPLAN-PROBE-20261118-133249.md"
DOCS = ("README.md", "docs/REPLAN_DEMO.md", "docs/REVIEW_RESPONSE.md", "docs/EVIDENCE.md")
BLOCK = re.compile(r"<!-- probe:(?P<name>[\w]+):start -->\n(?P<body>.*?)\n<!-- probe:(?P=name):end -->", re.S)


def blocks(doc: str) -> list[tuple[str, list[str]]]:
    text = (ROOT / doc).read_text(encoding="utf-8")
    return [(m["name"], [line[2:] if line.startswith("> ") else line for line in m["body"].split("\n")]) for m in BLOCK.finditer(text)]


def test_every_quoted_line_is_a_line_of_the_generated_report():
    if not REPORT.exists():
        pytest.skip(f"{REPORT.name} is not in this checkout")
    report = REPORT.read_text(encoding="utf-8").splitlines()
    seen = 0
    for doc in DOCS:
        for name, body in blocks(doc):
            assert body and all(line.strip() for line in body), (doc, name)
            for line in body:
                assert line in report, f"{doc}, block {name}: this line is not a line of {REPORT.name}: {line[:100]!r}"
                seen += 1
    assert seen >= 30  # the extraction found the blocks


def test_each_document_has_the_headline_block_and_the_demo_has_all_the_descriptive_ones():
    names = {doc: {n for n, _ in blocks(doc)} for doc in DOCS}
    assert "readme_outcome" in names["README.md"]
    assert "outcome" in names["docs/REPLAN_DEMO.md"] and "rr_outcome" in names["docs/REVIEW_RESPONSE.md"] and "ev_outcome" in names["docs/EVIDENCE.md"]
    assert {"outcome", "pairs", "by_group", "by_on", "noise", "cost", "over", "c03a", "c03b", "p07"} <= names["docs/REPLAN_DEMO.md"]


def test_the_probe_section_types_no_judge_rate_outside_the_quoted_blocks():
    text = (ROOT / "docs" / "REPLAN_DEMO.md").read_text(encoding="utf-8")
    prose = BLOCK.sub("", text[text.index("## The replan probe (2026-11-18)"):])
    assert not re.search(r"\d+\s*%", prose) and not re.search(r"\b\d+ (?:of|out of) \d+\b", prose) and not re.search(r"\bpass rate\b", prose, re.I)
    assert "REPLAN_MAX=0` stays the default" in prose and "Decision on Phase 5" in prose and "skipped, by decision of the owner" in prose


def test_the_report_headline_is_the_one_of_the_rule_and_says_it_is_generated():
    if not REPORT.exists():
        pytest.skip(f"{REPORT.name} is not in this checkout")
    first = REPORT.read_text(encoding="utf-8")
    assert "**Outcome of the preregistered rule:" in first and "applies the rule of the preregistration mechanically" in first
    assert "\r" not in first  # LF, as .gitattributes says for eval/reports/*.md
