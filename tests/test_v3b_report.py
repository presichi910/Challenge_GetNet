"""eval/calibration/v3b_report.py: Wilson interval, agreement and kappa for the 15 new rows and for the 30, from synthetic sheets. The real sheet and key are not read here."""
import csv
import json
from pathlib import Path

import pytest

from eval.calibration import v3b_report as mod
from eval.calibration.sample_v3b import sha256

KEY_COLS = ["id", "case_id", "judge_verdict", "judge_grounded", "judge_complete"]
BLIND_COLS = ["id", "question", "human_grounded", "human_complete", "human_pass", "notes"]


def write(path: Path, cols, rows):
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def sheets(tmp_path, prefix, pairs):
    """pairs: [(judge_verdict, human_pass)]; -> (blind path, key path)."""
    ids = [f"{prefix}{n:02d}" for n in range(1, len(pairs) + 1)]
    write(tmp_path / f"{prefix}_key.csv", KEY_COLS, [{"id": i, "case_id": f"case_{i}", "judge_verdict": j, "judge_grounded": 4, "judge_complete": 4} for i, (j, _) in zip(ids, pairs)])
    write(tmp_path / f"{prefix}_blind.csv", BLIND_COLS, [{"id": i, "question": "q", "human_grounded": 4, "human_complete": 4, "human_pass": h} for i, (_, h) in zip(ids, pairs)])
    return tmp_path / f"{prefix}_blind.csv", tmp_path / f"{prefix}_key.csv"


def test_the_wilson_interval_matches_known_values():
    lo, hi = mod.wilson(13, 15)
    assert (round(lo, 3), round(hi, 3)) == (0.621, 0.963)  # 13 of 15, z = 1.96
    lo, hi = mod.wilson(0, 10)
    assert lo == 0.0 and round(hi, 3) == 0.278
    lo, hi = mod.wilson(10, 10)
    assert hi == pytest.approx(1.0) and round(lo, 3) == 0.722
    assert mod.wilson(0, 0) is None


def test_the_report_has_the_new_rows_alone_and_the_thirty_together_each_with_its_n_and_its_interval(tmp_path):
    v3 = sheets(tmp_path, "a", [("pass", "yes")] * 8 + [("fail", "no")] * 5 + [("fail", "yes")] * 2)  # 13 of 15 agree
    v3b = sheets(tmp_path, "b", [("pass", "yes")] * 9 + [("fail", "no")] * 4 + [("pass", "no")] * 2)  # 13 of 15 agree
    text = mod.report(mod.joined(*v3, "V3"), mod.joined(*v3b, "V3b"))
    assert "## V3b alone (15 new cases)" in text and "## V3 and V3b together (30 rows of 30 cases)" in text
    assert text.count("labeled rows: 15 of 15") == 1 and text.count("labeled rows: 30 of 30") == 1
    assert text.count("pass/fail agreement: 13 of 15 = 87%; Wilson 95% interval 62% to 96%") == 1
    assert "pass/fail agreement: 26 of 30 = 87%; Wilson 95% interval 70% to 95%" in text
    assert "no interval" in text and "already had a verdict of the judge" in text
    assert "`case_b14`" in text and "`case_a14`" in text  # a disagreement is named by the real case id of the key


def test_a_sheet_with_unlabeled_rows_counts_only_the_labeled_ones(tmp_path):
    v3 = sheets(tmp_path, "a", [("pass", "yes")] * 3)
    v3b = sheets(tmp_path, "b", [("pass", "yes"), ("fail", "no"), ("pass", "")])
    text = mod.report(mod.joined(*v3, "V3"), mod.joined(*v3b, "V3b"))
    assert "labeled rows: 2 of 3" in text and "labeled rows: 5 of 6" in text


def test_it_refuses_a_key_that_is_not_the_one_that_was_sealed(tmp_path):
    v3 = sheets(tmp_path, "a", [("pass", "yes")])
    v3b = sheets(tmp_path, "b", [("pass", "yes")])
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({"key_sha256": sha256(v3b[1])}), encoding="utf-8")
    args = ["-o", str(tmp_path / "out.md"), "--v3-blind", str(v3[0]), "--v3-key", str(v3[1]), "--v3b-blind", str(v3b[0]), "--v3b-key", str(v3b[1]), "--manifest", str(manifest)]
    assert mod.main(args) == 0 and (tmp_path / "out.md").read_text(encoding="utf-8").startswith("# Judge calibration V3b")
    v3b[1].write_text(v3b[1].read_text(encoding="utf-8").replace("pass", "fail"), encoding="utf-8")  # the key edited after the draw
    with pytest.raises(SystemExit, match="was edited after the draw"):
        mod.main(args)
