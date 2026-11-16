"""Regenerates `constraints.txt`: every dependency (direct and transitive) pinned to the version the image gets.

    python scripts/lock_deps.py

It installs requirements.txt INSIDE a clean python:3.12-slim container (Linux, the image's own base and Python) and writes
what `pip freeze` reports there. Never freeze the Windows venv: its Python and its wheels are not the image's. Needs Docker; adds
no dependency. After running it, rebuild the image and run the tests: a new transitive version is a change like any other.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HEADER = """# Pinned versions of every dependency (direct and transitive), generated INSIDE the image's own base (python:3.12-slim, Linux)
# with `python scripts/lock_deps.py` (it installs requirements.txt in a clean container and runs `pip freeze`).
# The Dockerfile installs with `-c constraints.txt`, so a rebuild cannot pick up a newer transitive release.
"""
INSIDE = ("cat > /tmp/r.txt && pip install -q --disable-pip-version-check -r /tmp/r.txt >&2 "
          "&& pip freeze --disable-pip-version-check")


def main() -> int:
    run = subprocess.run(["docker", "run", "-i", "--rm", "python:3.12-slim", "sh", "-c", INSIDE],
                         input=(ROOT / "requirements.txt").read_bytes(), capture_output=True)
    if run.returncode != 0 or not run.stdout.strip():
        sys.stderr.write(run.stderr.decode("utf-8", "replace")[-2000:])
        return run.returncode or 1
    (ROOT / "constraints.txt").write_text(HEADER + run.stdout.decode("utf-8"), encoding="utf-8", newline="\n")
    print(f"constraints.txt: {len(run.stdout.splitlines())} packages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
