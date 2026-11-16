"""P1c: the lock file agrees with requirements.txt, and the Dockerfile uses it. No network, no Docker."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def pins(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        m = re.fullmatch(r"([A-Za-z0-9_.\-]+)(?:\[[^\]]*\])?==([A-Za-z0-9_.\-+!]+)", line)
        if m:
            out[re.sub(r"[-_.]+", "-", m.group(1)).lower()] = m.group(2)
    return out


def test_every_requirement_is_pinned_in_the_lock_file_to_the_same_version():
    wanted, locked = pins(ROOT / "requirements.txt"), pins(ROOT / "constraints.txt")
    assert len(wanted) >= 15
    assert {n: v for n, v in wanted.items() if locked.get(n) != v} == {}, "run `python scripts/lock_deps.py` after changing requirements.txt"


def test_the_lock_file_pins_the_transitive_dependencies_too_and_has_no_local_paths():
    locked = pins(ROOT / "constraints.txt")
    wanted = pins(ROOT / "requirements.txt")
    assert len(locked) > len(wanted) + 40  # the sub-dependencies are in it
    text = (ROOT / "constraints.txt").read_text(encoding="utf-8")
    assert " @ " not in text and "file:" not in text  # a freeze of a local checkout would have these


def test_the_image_installs_with_the_lock_in_a_build_stage_and_runs_without_pip_caches():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert dockerfile.count("FROM python:3.12-slim") == 2 and "AS build" in dockerfile
    assert "pip install -r requirements.txt -c constraints.txt" in dockerfile
    assert "COPY --from=build /opt/venv /opt/venv" in dockerfile
    final = dockerfile.split("COPY --from=build")[1]
    assert "pip install" not in final and "HEALTHCHECK" in final and "USER app" in final
