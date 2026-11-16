"""M2: the conversation id is made by the SERVER. The real rule, through the API with a real memory registry (marked strict_ids: the
rest of the tests run with this one check off, see conftest)."""
import re

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage

from app.graph import build_graph
from app.main import app
from app.memory import Memory
from app.service import ChatService, new_conversation_id
from tests.conftest import bearer, make_jwt

pytestmark = pytest.mark.strict_ids
A, B = "cliente1988", "cliente2024"


@pytest.fixture
def client(auth_env, tmp_path):
    def router(state):
        return {"plan": [], "step": 0, "language": "English", "routing_reasoning": "stub", "agent_outputs": {}, "final_answer": "",
                "wants_human": False, "handoff": None}

    def synthesizer(state):  # lists every customer message the conversation holds, so the reply shows what the thread remembers
        answer = "seen=" + "|".join(m.content for m in state["messages"] if isinstance(m, HumanMessage))
        return {"final_answer": answer, "messages": [AIMessage(content=answer)]}

    mem = Memory(tmp_path / "c.sqlite", ttl_days=30, max_threads_per_user=3)
    app.state.service = ChatService(build_graph(router, lambda s: {}, lambda s: {}, synthesizer, lambda s: {}, checkpointer=mem.saver), memory=mem)
    with TestClient(app) as c:
        yield c, mem
    app.state.service = None
    mem.close()


def chat(c, user, message, conv=None):
    body = {"message": message, **({"conversation_id": conv} if conv else {})}
    return c.post("/chat", json=body, headers=bearer(make_jwt(user)))


def test_without_an_id_the_server_starts_a_new_conversation_and_returns_its_id(client):
    c, mem = client
    first, second = chat(c, A, "hello").json(), chat(c, A, "hello again").json()
    assert re.fullmatch(r"c-[0-9a-f]{16}", first["conversation_id"]) and first["conversation_id"] != second["conversation_id"]
    assert first["response"] == "seen=hello" and second["response"] == "seen=hello again"  # no shared hidden "default" thread any more
    assert mem.thread_count(A) == 2


def test_sending_back_the_id_the_server_gave_continues_that_conversation(client):
    c, _ = client
    conv = chat(c, A, "my terminal is offline").json()["conversation_id"]
    again = chat(c, A, "and it is still offline", conv).json()
    assert again["conversation_id"] == conv and again["response"] == "seen=my terminal is offline|and it is still offline"


def test_an_id_the_client_invented_is_a_404_and_creates_nothing(client):
    c, mem = client
    r = chat(c, A, "hi", "my-own-id")
    assert r.status_code == 404 and r.json()["detail"] == "Conversation not found." and mem.thread_count(A) == 0
    assert c.get("/conversations", headers=bearer(make_jwt(A))).json()["count"] == 0


def test_somebody_elses_real_id_is_the_same_404_so_nothing_leaks(client):
    c, mem = client
    theirs = chat(c, B, "private question").json()["conversation_id"]
    probe = chat(c, A, "hi", theirs)
    missing = chat(c, A, "hi", "c-0000000000000000")
    assert probe.status_code == missing.status_code == 404 and probe.json() == missing.json()
    assert mem.thread_count(A) == 0 and chat(c, B, "still mine", theirs).json()["response"].endswith("private question|still mine")


def test_a_malformed_id_is_still_rejected_by_validation(client):
    c, _ = client
    assert chat(c, A, "hi", "has a space").status_code == 422 and chat(c, A, "hi", "x" * 65).status_code == 422


def test_the_thread_limit_still_applies_to_the_conversations_the_server_starts(client):
    c, mem = client
    assert [chat(c, A, f"q{i}").status_code for i in range(3)] == [200, 200, 200]
    assert chat(c, A, "one too many").status_code == 429 and mem.thread_count(A) == 3
    assert chat(c, A, "continue", c.get("/conversations", headers=bearer(make_jwt(A))).json()["conversations"][0]["conversation_id"]).status_code == 200


def test_generated_ids_are_unique_and_unguessable_in_shape():
    ids = {new_conversation_id() for _ in range(2000)}
    assert len(ids) == 2000 and all(re.fullmatch(r"c-[0-9a-f]{16}", i) for i in ids)
