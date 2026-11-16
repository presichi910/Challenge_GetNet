"""The quality gates are in the CI, they all block, and the one documented exception (the ChromaDB server advisories) is the only one."""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CI = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
CI_TEXT = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
ACCEPTED = {"PYSEC-2026-311", "PYSEC-2026-3813", "PYSEC-2026-3814", "PYSEC-2026-3815"}


def steps():
    return [s for job in CI["jobs"].values() for s in job["steps"]]


def test_every_gate_is_a_step_of_the_ci():
    text = " ".join(str(s.get("run", "")) + str(s.get("uses", "")) for s in steps())
    for gate in ("ruff check", "mypy", "bandit -r app scripts -ll", "pip-audit", "trivy-action", "promtool", "pytest", "docker compose", "docker build"):
        assert gate in text, f"the CI has no `{gate}` step"


def test_nothing_in_the_ci_is_allowed_to_fail_without_failing_the_build():
    assert "continue-on-error" not in CI_TEXT and "|| true" not in CI_TEXT and "exit-code: \"0\"" not in CI_TEXT
    trivy = next(s for s in steps() if "trivy-action" in str(s.get("uses", "")))
    assert trivy["with"]["exit-code"] == "1" and trivy["with"]["severity"] == "HIGH,CRITICAL"


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
