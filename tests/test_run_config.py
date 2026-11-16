"""Every run says how the system was configured (REPLAN_MAX, a label such as "replan on"), so one can never be mistaken for another."""

from app.config import get_settings
from eval import repeats, run_eval
from eval.run_eval import run_config, run_repeats


def test_the_main_run_says_out_loud_that_the_observe_step_is_off_and_nothing_else_is_hidden():
    cfg = run_config()
    assert cfg["replan_max"] == 0 and "label" not in cfg
    assert cfg["approvals"] == "off (eval)" and cfg["fallback_provider"] == "off (eval)" and cfg["rag_mode"] == "dense"


def test_a_labeled_run_carries_its_label_and_the_replan_setting_in_force(monkeypatch):
    monkeypatch.setenv("REPLAN_MAX", "2")
    get_settings.cache_clear()
    cfg = run_config("replan on")
    assert cfg["replan_max"] == 2 and cfg["label"] == "replan on"


def test_the_configuration_goes_into_every_summary_and_the_repeats_report(tmp_path, monkeypatch):
    written = []
    monkeypatch.setattr(run_eval, "write_report", lambda rows, summary, models=None, update_latest=True: (written.append(summary), tmp_path / f"r{len(written)}.md")[1])
    monkeypatch.setattr(repeats, "ROOT", tmp_path)

    class Service:
        def run_raw(self, message, user_id, thread_id=None, **kw):
            return {"final_answer": "a", "plan": [], "agent_outputs": {}}, 5

    case = {"id": "q99", "user_id": "u", "message": "hi", "expected_agents": [], "expected_tools": [], "expect_handoff": False}
    cfg = run_config("replan on")
    done, path = run_repeats([case], Service(), None, 2, pause=0, config=cfg)
    assert all(s["label"] == "replan on" and s["replan_max"] == cfg["replan_max"] for s in written) and len(written) == 2
    text = path.read_text(encoding="utf-8")
    assert "Configuration:" in text and "label=replan on" in text
    assert "replan_max" not in text.split("## Cases")[0].split("Configuration:")[1].split("\n", 2)[2]  # not repeated as a "metric" row
