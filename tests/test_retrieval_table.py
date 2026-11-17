"""The retrieval table of the README is typed by hand. It must say what the saved retrieval report says: for each set of questions
(the tune half, the report half, the holdout and all 21) and for each of the three modes, hit@1, hit@5, recall@5, MRR and nDCG@5.
No network, no keys. Skipped when the report is not in the checkout."""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "eval" / "reports" / "RETRIEVAL-20261115-182559.md"
README = ROOT / "README.md"
MODES = ("dense", "hybrid", "hybrid_rerank")
METRICS = ("hit@1", "hit@5", "recall@5", "MRR", "nDCG@5")
# the README label of each set -> the heading of its table in the report
SETS = {"tune half": "regular, tune half", "report half": "regular, report half", "holdout": "holdout", "all": "all"}


def cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def readme_rows() -> dict[str, tuple[int, dict[str, list[str]]]]:
    """{set label: (n, {metric: [dense, hybrid, hybrid_rerank]})} from the README table whose header starts with `| set (n) | mode |`."""
    lines = README.read_text(encoding="utf-8").splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("| set (n) | mode |")), None)
    assert start is not None, "the README has no retrieval table with the header '| set (n) | mode |'"
    header = cells(lines[start])
    columns = {m: header.index(m) for m in METRICS}
    out = {}
    for line in lines[start + 2:]:
        if not line.startswith("|"):
            break
        row = cells(line)
        m = re.fullmatch(r"(.+?) \((\d+)\)", row[0])
        assert m, f"cannot read the set cell {row[0]!r}"
        assert row[1] == " / ".join(MODES), f"the mode cell of {row[0]!r} is not 'dense / hybrid / hybrid_rerank'"
        values = {metric: [v.strip() for v in row[col].split("/")] for metric, col in columns.items()}
        assert all(len(v) == 3 for v in values.values()), f"{row[0]!r} does not have three values per metric"
        out[m.group(1)] = (int(m.group(2)), values)
    return out


def report_tables() -> dict[str, dict[str, dict[str, str]]]:
    """{heading: {mode: {metric (and 'n'): value}}} for the `### <heading>` tables that list the three modes."""
    text = REPORT.read_text(encoding="utf-8")
    out: dict[str, dict[str, dict[str, str]]] = {}
    for block in re.split(r"(?m)^### ", text)[1:]:
        heading, _, body = block.partition("\n")
        table: list[str] = []
        for line in body.splitlines():  # the first table of the block only: a later table of the same block has other columns
            if line.startswith("|"):
                table.append(line)
            elif table:
                break
        if len(table) < 3 or "mode" not in cells(table[0]):
            continue
        header = cells(table[0])
        rows = {cells(line)[0]: dict(zip(header, cells(line), strict=True)) for line in table[2:]}
        if set(MODES) <= set(rows):
            out[heading.strip()] = rows
    return out


@pytest.fixture(scope="module")
def report():
    if not REPORT.exists():
        pytest.skip(f"not in this checkout: {REPORT.name}")
    return report_tables()


def test_the_report_has_a_table_with_the_three_modes_for_every_set_the_readme_shows(report):
    for label, heading in SETS.items():
        assert heading in report, f"the report has no table with the three modes under '### {heading}' (the README row '{label}')"


@pytest.mark.parametrize("label", list(SETS))
def test_each_row_of_the_readme_table_is_the_report_for_the_three_modes(report, label):
    rows = readme_rows()
    assert label in rows, f"the README table has no row '{label} (n)'"
    n, values = rows[label]
    table = report[SETS[label]]
    for i, mode in enumerate(MODES):
        assert int(table[mode]["n"]) == n, f"{label}: the README says n = {n}, the report {table[mode]['n']}"
        for metric in METRICS:
            assert values[metric][i] == table[mode][metric], f"{label}, {mode}, {metric}: README {values[metric][i]} against report {table[mode][metric]}"


def test_the_readme_table_has_exactly_the_four_sets_and_the_questions_add_up():
    rows = readme_rows()
    assert set(rows) == set(SETS)
    assert rows["tune half"][0] + rows["report half"][0] == 15 and rows["tune half"][0] + rows["report half"][0] + rows["holdout"][0] == rows["all"][0]


def test_the_parser_reads_a_header_in_any_column_order(tmp_path, monkeypatch):
    table = ("| set (n) | mode | MRR | hit@5 | hit@1 | nDCG@5 | recall@5 |\n|---|---|---|---|---|---|---|\n"
             "| all (2) | dense / hybrid / hybrid_rerank | 0.1 / 0.2 / 0.3 | 0.4 / 0.5 / 0.6 | 0.7 / 0.8 / 0.9 | 0.11 / 0.12 / 0.13 | 0.14 / 0.15 / 0.16 |\n")
    fake = tmp_path / "README.md"
    fake.write_text(table, encoding="utf-8")
    monkeypatch.setitem(readme_rows.__globals__, "README", fake)
    n, values = readme_rows()["all"]
    assert n == 2 and values["hit@1"] == ["0.7", "0.8", "0.9"] and values["MRR"] == ["0.1", "0.2", "0.3"] and values["recall@5"] == ["0.14", "0.15", "0.16"]
