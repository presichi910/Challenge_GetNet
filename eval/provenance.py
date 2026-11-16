"""Which code a run came from: the commit hash and whether the working tree was clean, written into every report and JSON.

A result is only worth citing if it can be tied to the code that produced it. `repo_state()` asks git for the commit (`HEAD`) and for whether
anything that is CODE has uncommitted changes. What a run itself writes (`eval/reports/`) does not count: the second run of a session
would otherwise find the first one's reports and call the tree dirty. Without git (the Docker image has none) it says so instead of guessing.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXCLUDE = (":(exclude)eval/reports",)


def _git(root: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.rstrip() if out.returncode == 0 else None  # NOT lstrip: porcelain lines start with a space


def repo_state(root: Path | str = ROOT) -> dict:
    """{"commit": full hash or "unknown", "tree_clean": True / False / "unknown", "dirty_files": [...]} (the files are listed when dirty)."""
    root = Path(root)
    commit = _git(root, "rev-parse", "HEAD")
    if commit is None:
        return {"commit": "unknown", "tree_clean": "unknown", "dirty_files": []}
    status = _git(root, "status", "--porcelain", "--untracked-files=all", "--", ".", *EXCLUDE)
    files = [line[3:] for line in (status or "").splitlines() if line.strip()]  # "XY path"
    return {"commit": commit, "tree_clean": not files, "dirty_files": files}


def require_clean(state: dict | None = None) -> tuple[bool, str]:
    """(ok, message). Not ok when the tree is dirty or when it cannot be known: a run that must be citable is not launched on a guess."""
    state = state or repo_state()
    if state["tree_clean"] is True:
        return True, f"commit {state['commit'][:12]}, working tree clean"
    if state["tree_clean"] == "unknown":
        return False, "cannot tell which commit this is (no git here)"
    shown = ", ".join(state["dirty_files"][:8]) + (" ..." if len(state["dirty_files"]) > 8 else "")
    return False, f"the working tree has uncommitted changes ({shown}): commit them first, so the run can cite its commit"
