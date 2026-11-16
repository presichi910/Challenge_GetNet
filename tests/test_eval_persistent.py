"""M3: the eval's multi-turn cases run on a REAL SQLite checkpointer with a restart in the middle, and check the memory deterministically."""
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.graph import build_graph
from eval.persistent import PersistentEvalService
from eval.run_eval import evaluate, summarize

HISTORY = [{"role": "user", "content": "I sell lunch boxes and my terminal is a Get Smart."},
           {"role": "assistant", "content": "Thanks! How can I help with your Get Smart?"}]
CASE = {"id": "m99_followup", "user_id": "cliente1988", "message": "what did I say I sell?", "history": HISTORY, "expected_agents": [],
        "expected_tools": [], "expect_handoff": False, "criteria": "uses the earlier turn"}
SINGLE = {"id": "q99_single", "user_id": "cliente1988", "message": "hello", "expected_agents": [], "expected_tools": [], "expect_handoff": False}


def build(checkpointer):
    """Stub nodes on the REAL graph shape (it must have a `handoff` node: the seed is written as coming from it)."""
    def router(state):
        return {"plan": [], "step": 0, "language": "English", "routing_reasoning": "stub", "agent_outputs": {}, "final_answer": "",
                "wants_human": False, "handoff": None}

    def synthesizer(state):
        answer = "I remember: " + " | ".join(str(m.content) for m in state["messages"] if isinstance(m, HumanMessage))
        return {"final_answer": answer, "messages": [AIMessage(content=answer)]}

    return build_graph(router, lambda s: {}, lambda s: {}, synthesizer, lambda s: {}, checkpointer=checkpointer)


@pytest.fixture
def service():
    s = PersistentEvalService(build=build)
    yield s
    s.close()


def test_the_earlier_turns_survive_a_restart_of_the_checkpointer(service):
    messages = [HumanMessage(content="first"), AIMessage(content="second")]
    service.seed("t1", "cliente1988", messages)
    path = service.memory.path
    service.restart()
    assert service.restarts == 1 and service.memory.path == path  # the same file, reopened
    assert [(m.type, m.content) for m in service.thread_messages("t1")] == [("human", "first"), ("ai", "second")]


def test_a_multi_turn_case_is_answered_after_the_restart_from_what_the_thread_holds(service):
    rows = evaluate([CASE], service, None, pause=0)
    assert rows[0]["answer"] == "I remember: I sell lunch boxes and my terminal is a Get Smart. | what did I say I sell?"
    assert service.restarts == 1 and rows[0]["memory_ok"] is True
    assert rows[0]["memory"] == {"restart": True, "seeded": 2, "restored": 2, "restored_intact": True, "after": 4, "answered_in_thread": True}
    assert summarize(rows)["memory_accuracy"] == 1.0


def test_a_single_turn_case_is_untouched_no_seed_no_restart_no_memory_field(service):
    row = evaluate([SINGLE], service, None, pause=0)[0]
    assert service.restarts == 0 and "memory_ok" not in row and "memory_accuracy" not in summarize([row])


def test_each_case_gets_its_own_thread_so_conversations_never_mix(service):
    other = {**CASE, "id": "m98_other", "history": [{"role": "user", "content": "I run a bakery."}, {"role": "assistant", "content": "Nice."}]}
    rows = evaluate([CASE, other], service, None, pause=0)
    assert "lunch boxes" in rows[0]["answer"] and "bakery" not in rows[0]["answer"]
    assert "bakery" in rows[1]["answer"] and "lunch boxes" not in rows[1]["answer"] and service.restarts == 2


def test_a_memory_that_loses_the_turns_in_the_restart_fails_the_check_deterministically(service, monkeypatch):
    monkeypatch.setattr(service, "thread_messages", lambda tid: [])  # what a broken persistence would look like
    rows = evaluate([CASE], service, None, pause=0)
    assert rows[0]["memory_ok"] is False and summarize(rows)["memory_accuracy"] == 0.0


def test_without_a_persistent_service_the_old_way_still_works_the_history_is_injected(service):
    from app.service import ChatService

    plain = ChatService(build(None))
    row = evaluate([CASE], plain, None, pause=0)[0]
    assert "lunch boxes" in row["answer"] and "memory_ok" not in row  # nothing to check: no checkpointer, no restart


def test_the_temporary_folder_is_removed_when_the_service_is_closed():
    s = PersistentEvalService(build=build)
    folder = Path(s.dir)
    assert folder.exists()
    s.close()
    assert not folder.exists()


def test_the_real_dataset_multi_turn_cases_are_the_ones_that_get_the_restart():
    from eval.run_eval import load_cases

    multi = [c["id"] for c in load_cases(None) if c.get("history")]
    assert len([i for i in multi if i.startswith("m")]) >= 5  # m01-m08 and any holdout with history all run on the persistent memory
