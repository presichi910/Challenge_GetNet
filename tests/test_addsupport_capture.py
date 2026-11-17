"""eval/addsupport_capture.py: the wrapper returns what the original returns, writes one line per call, and puts everything back. Synthetic data, no network."""
import json
import sys

import pytest
from langchain_core.messages import AIMessage, HumanMessage

import eval.addsupport_capture as capture_module
from app.agents import observe
from eval import run_eval
from eval.addsupport_capture import capturing

ACCOUNT = "Your next payout is R$ 10 on Friday."
HOWTO = "Restart the terminal."


def state(plan=("knowledge",), outputs=None, user_id="cliente1988"):
    return {"messages": [HumanMessage(content="When is my payout?"), AIMessage(content="draft")], "plan": list(plan),
            "agent_outputs": outputs if outputs is not None else {"knowledge": {"answer": "x"}}, "user_id": user_id}


def lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_the_wrapper_returns_exactly_what_the_original_returns_and_writes_one_line_per_call(tmp_path):
    original = observe.can_add_support
    path = tmp_path / "cap.jsonl"
    cases = [(state(), [ACCOUNT, HOWTO]), (state(plan=("support",)), [ACCOUNT]), (state(user_id=""), [ACCOUNT]), (state(), [HOWTO])]
    expected = [original(s, c) for s, c in cases]
    with capturing(path):
        assert observe.can_add_support is not original  # patched while inside
        got = [observe.can_add_support(s, c) for s, c in cases]
    assert got == expected == [True, False, False, False]
    rows = lines(path)
    assert len(rows) == 4 and [r["can_add_support"] for r in rows] == expected
    first = rows[0]
    assert first["claims"] == [{"text": ACCOUNT, "account": True}, {"text": HOWTO, "account": False}]  # full text, with the lexicon's verdict
    assert first["question"] == "When is my payout?" and first["has_user_id"] is True and first["support_in_plan"] is False
    assert first["support_in_outputs"] is False and first["plan"] == ["knowledge"]
    assert rows[1]["support_in_plan"] is True and rows[2]["has_user_id"] is False
    assert "cliente1988" not in path.read_text(encoding="utf-8")  # the id itself is never written
    assert observe.can_add_support is original  # restored


def test_it_restores_both_functions_even_when_the_block_fails(tmp_path):
    original_can, original_evaluate = observe.can_add_support, run_eval.evaluate
    with pytest.raises(RuntimeError):
        with capturing(tmp_path / "cap.jsonl"):
            assert run_eval.evaluate is not original_evaluate
            raise RuntimeError("boom")
    assert observe.can_add_support is original_can and run_eval.evaluate is original_evaluate


def test_an_error_of_the_original_goes_through_and_nothing_is_recorded(tmp_path, monkeypatch):
    def boom(st, claims):
        raise ValueError("original failed")

    monkeypatch.setattr(observe, "can_add_support", boom)
    path = tmp_path / "cap.jsonl"
    with capturing(path):
        with pytest.raises(ValueError, match="original failed"):
            observe.can_add_support(state(), [ACCOUNT])
    assert lines(path) == [] and observe.can_add_support is boom


def test_the_case_and_the_repetition_come_from_the_eval_loop_without_changing_run_eval(tmp_path, monkeypatch):
    seen = []

    def fake_evaluate(cases, service, judge, pause, retries=0, sleep=None):  # the shape run_eval.evaluate is called with
        rows = []
        for c in cases:
            seen.append(c["id"])
            observe.can_add_support(state(), [ACCOUNT])
            if c["id"] == "q09":
                observe.can_add_support(state(), [HOWTO])  # a second call inside the same case (a retry of the turn)
            rows.append({"id": c["id"]})
        return rows

    monkeypatch.setattr(run_eval, "evaluate", fake_evaluate)
    path = tmp_path / "cap.jsonl"
    with capturing(path):
        for _ in range(2):  # two repetitions, as run_repeats does
            assert [r["id"] for r in run_eval.evaluate([{"id": "q04"}, {"id": "q09"}], None, None, 0, retries=0)] == ["q04", "q09"]
    assert seen == ["q04", "q09", "q04", "q09"]  # the fake still saw every case, in order
    assert [(r["case"], r["repetition"], r["call"]) for r in lines(path)] == [("q04", 1, 1), ("q09", 1, 1), ("q09", 1, 2), ("q04", 2, 1), ("q09", 2, 1), ("q09", 2, 2)]
    assert run_eval.evaluate is fake_evaluate


def test_a_line_that_cannot_be_written_never_changes_the_result(tmp_path, capsys):
    bad = tmp_path / "is_a_folder"
    with capturing(tmp_path / "ok.jsonl") as cap:
        cap.path = bad
        bad.mkdir()  # opening a directory for append fails
        assert observe.can_add_support(state(), [ACCOUNT]) is True and observe.can_add_support(state(), [ACCOUNT]) is True
    assert capsys.readouterr().err.count("could not write") == 1  # said once, not once per call


def test_main_forwards_every_argument_to_run_eval_and_leaves_argv_and_the_functions_as_they_were(tmp_path, monkeypatch, capsys):
    got = {}

    def fake_main():
        got["argv"] = list(sys.argv)
        run_eval.evaluate([{"id": "q04"}], None, None, 0)
        print("the normal output of the eval")

    monkeypatch.setattr(run_eval, "main", fake_main)
    monkeypatch.setattr(run_eval, "evaluate", lambda cases, *a, **k: [observe.can_add_support(state(), [ACCOUNT]) for _ in cases])
    monkeypatch.setattr(capture_module, "REPORTS", tmp_path)
    saved, original_can, original_evaluate = list(sys.argv), observe.can_add_support, run_eval.evaluate
    assert capture_module.main(["--regular-only", "--replan-max", "2", "--label", "replan on add_support", "--no-judge"]) == 0
    assert got["argv"] == ["eval.run_eval", "--regular-only", "--replan-max", "2", "--label", "replan on add_support", "--no-judge"]
    assert sys.argv == saved and observe.can_add_support is original_can and run_eval.evaluate is original_evaluate
    out = capsys.readouterr().out
    assert "the normal output of the eval" in out and "Add-support capture: 1 calls of can_add_support" in out
    files = list(tmp_path.glob("ADDSUPPORT-CAPTURE-*.jsonl"))
    assert len(files) == 1 and [(r["case"], r["repetition"]) for r in lines(files[0])] == [("q04", 1)]
