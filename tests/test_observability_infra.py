"""The observability files agree with the code: every metric an alert or a panel uses is one the API really exposes, and every alert has a unit test.
(`promtool test rules` itself needs Docker: CI runs it; see .github/workflows/ci.yml.)"""
import json
import re
from pathlib import Path

import yaml

from app.telemetry import Metrics

OBS = Path(__file__).resolve().parent.parent / "infra" / "observability"


def exposed_names() -> set[str]:
    """Every series name the API can expose: the metric families plus the suffixes Prometheus adds."""
    m = Metrics()
    names = set()
    for fam in m.registry.collect():
        for s in fam.samples:
            names.add(s.name)
        names.add(fam.name)
        names.add(fam.name + "_total")
    for h in ("getnet_turn_duration_seconds", "getnet_turn_cost_usd", "getnet_node_duration_seconds"):
        names |= {h + "_bucket", h + "_count", h + "_sum"}
    return names


def used_names(text: str) -> set[str]:
    return set(re.findall(r"\bgetnet_[a-z_]+", text))


def alerts() -> list[dict]:
    return [r for g in yaml.safe_load((OBS / "alerts.yml").read_text(encoding="utf-8"))["groups"] for r in g["rules"]]


def test_every_metric_an_alert_uses_is_one_the_api_exposes():
    known = exposed_names()
    for rule in alerts():
        missing = used_names(rule["expr"]) - known
        assert not missing, f"{rule['alert']} uses {missing}, which the API does not expose"


def test_every_metric_a_dashboard_panel_uses_is_one_the_api_exposes():
    known = exposed_names()
    dash = json.loads((OBS / "grafana" / "dashboards" / "getnet.json").read_text(encoding="utf-8"))
    assert dash["uid"] == "getnet-agents" and len(dash["panels"]) >= 8
    for p in dash["panels"]:
        assert p["targets"], p["title"]
        for t in p["targets"]:
            assert not (used_names(t["expr"]) - known), f"panel {p['title']!r} uses an unknown metric: {t['expr']}"
            assert t["datasource"]["uid"] == "prometheus"


def test_the_dashboard_datasource_uid_is_the_one_provisioned():
    ds = yaml.safe_load((OBS / "grafana" / "provisioning" / "datasources" / "prometheus.yml").read_text(encoding="utf-8"))["datasources"][0]
    assert ds["uid"] == "prometheus" and ds["type"] == "prometheus" and ds["url"] == "http://prometheus:9090"


def test_every_alert_has_a_severity_a_summary_and_a_unit_test_that_fires_it_and_one_that_keeps_it_quiet():
    tested = {}
    for t in yaml.safe_load((OBS / "alerts_test.yml").read_text(encoding="utf-8"))["tests"]:
        for a in t["alert_rule_test"]:
            tested.setdefault(a["alertname"], []).append(bool(a["exp_alerts"]))
    for rule in alerts():
        assert rule["labels"]["severity"] in ("warning", "critical") and rule["annotations"]["summary"] and rule["annotations"]["description"]
        assert True in tested.get(rule["alert"], []), f"{rule['alert']} has no test where it fires"
        assert False in tested.get(rule["alert"], []), f"{rule['alert']} has no test where it stays quiet"


def test_prometheus_scrapes_the_api_metrics_path_with_a_bearer_token_read_from_a_file_that_is_not_in_git():
    cfg = yaml.safe_load((OBS / "prometheus.yml").read_text(encoding="utf-8"))
    job = cfg["scrape_configs"][0]
    assert job["metrics_path"] == "/metrics" and job["static_configs"][0]["targets"] == ["api:8000"]
    assert job["authorization"]["type"] == "Bearer" and job["authorization"]["credentials_file"].endswith("metrics_token")
    assert "metrics_token" in (OBS.parent.parent / ".gitignore").read_text(encoding="utf-8") and not (OBS / "metrics_token").exists()


def test_the_observability_services_are_a_profile_bound_to_localhost_so_the_default_stack_is_unchanged():
    compose = yaml.safe_load((OBS.parent.parent / "docker-compose.yml").read_text(encoding="utf-8"))
    for name in ("jaeger", "prometheus", "grafana"):
        svc = compose["services"][name]
        assert svc["profiles"] == ["observability"] and all(p.startswith("127.0.0.1:") for p in svc["ports"])
        assert "@sha256" in svc["image"] or re.search(r":v?\d+\.\d+", svc["image"])  # a pinned version, never `latest`
    assert "profiles" not in compose["services"]["api"]
    assert {"prometheus_data", "grafana_data"} <= set(compose["volumes"])
