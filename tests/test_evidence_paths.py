"""docs/EVIDENCE.md and docs/REVIEW_RESPONSE.md point at files of this repository. Every path they cite in backticks must exist,
and a cited line number must be inside the file. No network, no keys, and no git hashes (the CI checks out a shallow copy)."""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCS = ("docs/EVIDENCE.md", "docs/REVIEW_RESPONSE.md")
TOP_DIRS = {"app", "eval", "tests", "docs", "infra", "scripts", ".github"} | {p.name for p in ROOT.iterdir() if p.is_dir() and not p.name.startswith(".")}
FILE_EXT = re.compile(r"\.(py|md|json|jsonl|yml|yaml|txt|csv|toml|ini|example)$")
EXTENSIONLESS = {"Dockerfile"}
CITED = re.compile(r"^(?P<path>[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)*/?)(?::(?P<first>\d+)(?:-(?P<last>\d+))?)?$")


def cited_paths(doc: str) -> list[tuple[str, str, int | None, int | None]]:
    """(doc, path, first line, last line) for each backticked token that is shaped like a path of this repository.
    A token with a slash counts when it starts at a top-level folder of the repo (so an API route like `/chat` or a model id like
    `openai/gpt-oss-120b` is not taken for a file); a token without a slash counts when it has a file extension."""
    out = []
    for token in re.findall(r"`([^`\n]+)`", (ROOT / doc).read_text(encoding="utf-8")):
        m = CITED.match(token)
        if not m:
            continue
        path = m["path"]
        if "/" in path:
            if path.split("/")[0] not in TOP_DIRS:
                continue
        elif not (FILE_EXT.search(path) or path in EXTENSIONLESS):
            continue
        first = int(m["first"]) if m["first"] else None
        last = int(m["last"]) if m["last"] else first
        out.append((doc, path, first, last))
    return out


ALL = [c for d in DOCS for c in cited_paths(d)]


def test_the_two_documents_exist_and_cite_a_real_number_of_paths():
    for doc in DOCS:
        assert (ROOT / doc).exists(), doc
    assert len(ALL) >= 60, f"only {len(ALL)} paths found: the extraction is probably broken"
    assert any(p.startswith("eval/reports/") for _, p, _, _ in ALL) and any(p.startswith("tests/") for _, p, _, _ in ALL)


@pytest.mark.parametrize("doc,path,first,last", sorted(set(ALL), key=lambda c: (c[0], c[1], c[2] or 0)))
def test_a_cited_path_exists_and_a_cited_line_is_inside_the_file(doc, path, first, last):
    target = ROOT / path.rstrip("/")
    assert target.exists(), f"{doc} cites {path}, which is not in the repository"
    if first is not None:
        assert target.is_file(), f"{doc} cites a line of {path}, which is not a file"
        lines = len(target.read_text(encoding="utf-8").splitlines())
        assert 1 <= first <= (last or first) <= lines, f"{doc} cites {path}:{first}-{last}, but the file has {lines} lines"


def test_the_extraction_ignores_routes_model_ids_and_commands():
    sample = "`/chat` `openai/gpt-oss-120b` `python -m eval.rejudge` `REPLAN_MAX=0` `app/auth.py:51` `eval/reports/<stamp>.json` `ho06`"
    found = re.findall(r"`([^`\n]+)`", sample)
    kept = [t for t in found if CITED.match(t) and (t.split(":")[0].split("/")[0] in TOP_DIRS)]
    assert kept == ["app/auth.py:51"]
