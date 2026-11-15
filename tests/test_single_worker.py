"""One worker, on purpose. The rate limit and the key cache live in the process, and the conversations in one SQLite file."""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app, validate_single_worker
from tests.test_auth_rs256 import RecordingService

ROOT = Path(__file__).parent.parent


def test_the_dockerfile_pins_one_worker():
    cmd = [l for l in (ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines() if l.startswith("CMD")]
    assert len(cmd) == 1 and re.search(r'"--workers",\s*"1"', cmd[0])


def test_compose_pins_one_worker_for_the_api():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    api = compose[compose.index("  api:"):compose.index("  # one-off jobs")]
    assert 'WEB_CONCURRENCY: "1"' in api


def test_the_readme_says_why_there_is_one_worker():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "One worker, on purpose" in readme and "WEB_CONCURRENCY" in readme


@pytest.mark.parametrize("value", ["2", "4", " 8 "])
def test_more_than_one_worker_is_refused_at_startup(auth_env, monkeypatch, value):
    monkeypatch.setenv("WEB_CONCURRENCY", value)
    with pytest.raises(RuntimeError, match="ONE worker"):
        validate_single_worker()
    app.state.service = RecordingService()
    try:
        with pytest.raises(RuntimeError, match="ONE worker"):
            with TestClient(app):
                pass
    finally:
        app.state.service = None


@pytest.mark.parametrize("value", [None, "", "1", "abc"])
def test_one_worker_or_nothing_set_starts(auth_env, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    else:
        monkeypatch.setenv("WEB_CONCURRENCY", value)
    validate_single_worker()


def test_the_revocations_file_lives_in_the_data_volume_with_the_other_sqlite_files():
    env = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    for var in ("CHECKPOINT_DB_PATH", "HANDOFFS_DB_PATH", "REVOCATIONS_DB_PATH"):
        assert re.search(rf"{var}=/app/data/\S+\.sqlite", env), var  # a real line break, not a literal "\n"
    assert (chr(92) + "n") not in env.split("ENV ", 1)[1].split("USER app")[0]  # a backslash followed by an n
