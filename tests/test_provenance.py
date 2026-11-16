"""Every report cites the commit it came from; a run that must be citable refuses a dirty tree. Real git in a temporary repository."""
import subprocess

import pytest

from eval.provenance import repo_state, require_clean


def git(root, *args):
    subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", *args], check=True, capture_output=True)


@pytest.fixture
def repo_dir(tmp_path):
    git(tmp_path, "init", "-q")
    (tmp_path / "code.py").write_text("x = 1\n")
    (tmp_path / "eval" / "reports").mkdir(parents=True)
    git(tmp_path, "add", "."), git(tmp_path, "commit", "-q", "-m", "first")
    return tmp_path


def test_a_clean_tree_reports_its_full_commit_hash(repo_dir):
    head = subprocess.run(["git", "-C", str(repo_dir), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    state = repo_state(repo_dir)
    assert state == {"commit": head, "tree_clean": True, "dirty_files": []} and len(head) == 40
    ok, message = require_clean(state)
    assert ok and head[:12] in message


def test_a_modified_or_new_code_file_makes_the_tree_dirty_and_names_it(repo_dir):
    (repo_dir / "code.py").write_text("x = 2\n")
    (repo_dir / "new.py").write_text("y = 1\n")
    state = repo_state(repo_dir)
    assert state["tree_clean"] is False and sorted(state["dirty_files"]) == ["code.py", "new.py"]  # whole names, not clipped
    ok, message = require_clean(state)
    assert not ok and "code.py" in message and "commit them first" in message


def test_what_a_run_writes_does_not_make_the_tree_dirty_so_the_second_run_of_a_session_can_start(repo_dir):
    (repo_dir / "eval" / "reports" / "REPEATS-20261115-210000.md").write_text("a report")
    (repo_dir / "eval" / "reports" / "20261115-210000.json").write_text("{}")
    assert repo_state(repo_dir)["tree_clean"] is True


def test_without_git_it_says_unknown_and_a_citable_run_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr("eval.provenance._git", lambda *a: None)
    state = repo_state(tmp_path)
    assert state == {"commit": "unknown", "tree_clean": "unknown", "dirty_files": []}
    ok, message = require_clean(state)
    assert not ok and "no git" in message


def test_every_run_config_carries_the_commit_and_the_tree_state():
    from eval.run_eval import run_config

    cfg = run_config("replan on")
    assert len(cfg["commit"]) in (40, 7) or cfg["commit"] == "unknown"
    assert "tree_clean" in cfg and cfg["label"] == "replan on"
