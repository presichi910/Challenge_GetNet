"""The quality gates are in the CI, they all block, and the one documented exception (the ChromaDB server advisories) is the only one."""
import re
import shlex
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
CI = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
CI_TEXT = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
ACCEPTED = {"PYSEC-2026-311", "PYSEC-2026-3813", "PYSEC-2026-3814", "PYSEC-2026-3815"}

# The trivy gate is a direct `docker run` of the official image, pinned by digest. Every flag it may carry is listed here: a flag that is not
# (`--skip-dirs`, `--ignorefile`, `--ignore-policy`, ...) is a way of making it quieter and fails the test.
TRIVY_IMAGE = re.compile(r"^aquasec/trivy@sha256:[0-9a-f]{64}$")
TRIVY_FLAGS_WITH_VALUE = {"--exit-code", "--severity", "--pkg-types", "--format", "--timeout"}
TRIVY_FLAGS_WITHOUT_VALUE = {"--ignore-unfixed", "--quiet", "--no-progress"}
SCANNED_IMAGE = "getnet-agents:ci"  # the tag the docker job builds in its first step
_PINNED = re.search(r"aquasec/trivy@sha256:[0-9a-f]{64}", CI_TEXT)
TRIVY_REF = _PINNED.group(0) if _PINNED else "aquasec/trivy@sha256:<missing>"  # a missing pin fails the gate test, not the collection


def trivy_steps(ci: dict) -> list[dict]:
    return [s for job in ci["jobs"].values() for s in job["steps"] if "aquasec/trivy" in str(s.get("run", "")) or "trivy-action" in str(s.get("uses", ""))]


def trivy_problems(step: dict) -> list[str]:
    """Everything wrong with the trivy step, empty if it is the blocking gate: exit 1 on a fixable HIGH or CRITICAL, image pinned by digest."""
    problems = []
    if "uses" in step:
        return ["it must be a `run:` of the official image, not an action (trivy-action pulls a second action by a tag that can disappear)"]
    if "if" in step or "continue-on-error" in step:
        problems.append("the step is conditional or may fail quietly")
    tokens = shlex.split(str(step.get("run", "")))
    if tokens[:2] != ["docker", "run"] or "image" not in tokens:
        return problems + ["it is not `docker run ... aquasec/trivy@sha256:... image ...`"]
    before, after = tokens[: tokens.index("image")], tokens[tokens.index("image") + 1 :]
    if not any(TRIVY_IMAGE.match(t) for t in before):
        problems.append("the trivy image is not pinned by digest (aquasec/trivy@sha256:<64 hex>)")
    if "/var/run/docker.sock:/var/run/docker.sock" not in before:
        problems.append("the docker socket is not mounted: trivy cannot read the image that was built")
    flags: dict[str, str | None] = {}
    positional = []
    i = 0
    while i < len(after):
        t = after[i]
        if t in TRIVY_FLAGS_WITH_VALUE and i + 1 < len(after):
            flags[t], i = after[i + 1], i + 2
        elif t in TRIVY_FLAGS_WITHOUT_VALUE:
            flags[t], i = None, i + 1
        elif t.startswith("-"):
            return problems + [f"flag `{t}` is not one the gate allows"]
        else:
            positional.append(t)
            i += 1
    if flags.get("--exit-code") != "1":
        problems.append("--exit-code must be 1")
    if flags.get("--severity") != "HIGH,CRITICAL":
        problems.append("--severity must be exactly HIGH,CRITICAL")
    if "--ignore-unfixed" not in flags:
        problems.append("--ignore-unfixed is missing (the gate is about vulnerabilities that have a fix)")
    if flags.get("--pkg-types") != "os,library":
        problems.append("--pkg-types must be os,library (the system packages AND the Python libraries)")
    if positional != [SCANNED_IMAGE]:
        problems.append(f"it must scan exactly {SCANNED_IMAGE}, the image built in the job")
    return problems


def steps():
    return [s for job in CI["jobs"].values() for s in job["steps"]]


def test_every_gate_is_a_step_of_the_ci():
    text = " ".join(str(s.get("run", "")) + str(s.get("uses", "")) for s in steps())
    for gate in ("ruff check", "mypy", "bandit -r app scripts -ll", "pip-audit", "aquasec/trivy@sha256:", "promtool", "pytest", "docker compose", "docker build"):
        assert gate in text, f"the CI has no `{gate}` step"


def test_nothing_in_the_ci_is_allowed_to_fail_without_failing_the_build():
    assert "continue-on-error" not in CI_TEXT and "|| true" not in CI_TEXT and "exit-code: \"0\"" not in CI_TEXT


def test_the_trivy_gate_blocks_on_a_fixable_high_or_critical_in_the_image_built_by_the_job_and_the_scanner_is_pinned_by_digest():
    found = trivy_steps(CI)
    assert len(found) == 1, "exactly one trivy step"
    assert trivy_problems(found[0]) == []
    docker_job = next(job for job in CI["jobs"].values() if found[0] in job["steps"])
    runs = [str(s.get("run", "")) for s in docker_job["steps"]]
    built = next(i for i, r in enumerate(runs) if "docker build" in r and f"-t {SCANNED_IMAGE}" in r)
    assert built < docker_job["steps"].index(found[0]), "trivy must scan after the image is built"


@pytest.mark.parametrize("old, new", [
    ("--exit-code 1", "--exit-code 0"),
    ("--exit-code 1 ", ""),
    ("--severity HIGH,CRITICAL", "--severity CRITICAL"),
    ("--severity HIGH,CRITICAL", "--severity HIGH,CRITICAL,MEDIUM"),
    (" --ignore-unfixed", ""),
    ("--pkg-types os,library", "--pkg-types os"),
    ("--pkg-types os,library", "--pkg-types os,library --skip-dirs /opt/venv"),
    ("--pkg-types os,library", "--pkg-types os,library --ignorefile .trivyignore"),
    (TRIVY_REF, "aquasec/trivy:0.70.0"),  # a tag can move; a digest cannot
    (TRIVY_REF, "aquasec/trivy:latest"),
    ("-v /var/run/docker.sock:/var/run/docker.sock", ""),
    ("getnet-agents:ci", "alpine:3"),
])
def test_relaxing_the_trivy_gate_is_caught(old, new):
    step = dict(trivy_steps(CI)[0])
    run = " ".join(str(step["run"]).split())
    assert old in run, f"the step no longer contains `{old}`: update this case"
    step["run"] = run.replace(old, new)
    assert trivy_problems(step), f"`{old}` -> `{new}` went unnoticed"


@pytest.mark.parametrize("extra", [{"continue-on-error": True}, {"if": "false"}, {"uses": "aquasecurity/trivy-action@v0.28.0"}])
def test_a_trivy_step_that_can_be_skipped_or_is_an_action_is_caught(extra):
    assert trivy_problems({**trivy_steps(CI)[0], **extra})


def test_the_only_accepted_vulnerabilities_are_the_documented_chroma_server_ones_and_the_reason_is_in_the_readme():
    ignored = set(re.findall(r"--ignore-vuln (\S+)", CI_TEXT))
    assert ignored == ACCEPTED
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for vuln in ACCEPTED:
        assert vuln in readme, f"{vuln} is ignored in the CI but not explained in the README"


def test_the_claim_behind_that_exception_holds_chroma_is_never_used_as_a_server_here():
    """The advisories are about ChromaDB's REST server and its multi-tenant authorization. If the code starts using either, the exception is void."""
    code = "\n".join(p.read_text(encoding="utf-8") for folder in ("app", "scripts", "eval") for p in (ROOT / folder).rglob("*.py"))
    for server_api in ("HttpClient", "AsyncHttpClient", "chroma run", "chromadb.server", "AuthorizationProvider", "CHROMA_SERVER"):
        assert server_api not in code, f"{server_api} is used: the pip-audit exception for the Chroma server advisories no longer holds"
    assert "persist_directory" in code
    assert "chroma" not in {s.lower() for s in yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))["services"]}


def test_the_dev_requirements_pin_every_gate_and_the_configs_exist():
    dev = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    for tool in ("ruff", "mypy", "bandit", "pip-audit"):
        assert re.search(rf"^{tool}==\d", dev, re.M), f"{tool} is not pinned in requirements-dev.txt"
    assert (ROOT / "ruff.toml").exists() and (ROOT / "mypy.ini").exists()


def test_the_dockerfile_labels_the_image_with_the_commit_it_was_built_from():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "ARG REVISION" in dockerfile and 'org.opencontainers.image.revision="${REVISION}"' in dockerfile
    assert "--build-arg REVISION" in CI_TEXT and "org.opencontainers.image.revision" in CI_TEXT


def test_the_final_image_applies_the_security_updates_of_its_base_packages_before_dropping_privileges():
    """trivy found two HIGH zlib1g advisories (with a fix) in the base image: the final stage upgrades the system packages as root, then switches to `app`."""
    final = (ROOT / "Dockerfile").read_text(encoding="utf-8").split("AS build")[1].split("FROM python:3.12-slim")[1]
    assert "apt-get -y --no-install-recommends upgrade" in final and "rm -rf /var/lib/apt/lists/*" in final
    assert final.index("apt-get") < final.index("USER app")
