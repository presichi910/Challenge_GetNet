"""M1: provider failover. Fireworks answers 503 -> the same model on Groq answers, and the turn does not end in the human queue."""
import httpx
import openai
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import Runnable
from langchain_core.runnables.fallbacks import RunnableWithFallbacks

import app.llm as llm_module
from app.agents.support import make_support_node
from app.config import get_settings
from app.graph import build_graph, next_node
from app.handoff import handoff_reason
from tests.conftest import ToolFakeModel

REQ = httpx.Request("POST", "https://api.fireworks.ai/inference/v1/chat/completions")


def status_error(code: int) -> Exception:
    cls = {400: openai.BadRequestError, 429: openai.RateLimitError, 503: openai.InternalServerError}[code]
    return cls(f"HTTP {code}", response=httpx.Response(code, request=REQ), body=None)


class Bindable(ToolFakeModel):
    """Like the real chat models, `bind_tools` is declared to return a Runnable: that is what makes `with_fallbacks` pass the call
    on to the backup as well (an unannotated fake would silently bypass the fallback and the turn test would prove nothing)."""

    def bind_tools(self, tools, **kwargs) -> Runnable:
        return self


def scripted(*messages: AIMessage) -> Bindable:
    return Bindable(messages=iter(messages))


class Down(Bindable):
    """A provider that is failing: every call raises the given error (and counts the calls)."""
    error: Exception | None = None
    calls: list = []

    def _generate(self, *args, **kwargs):
        self.calls.append(1)
        raise self.error


def down(code: int) -> Down:
    return Down(messages=iter([]), error=status_error(code), calls=[])


@pytest.fixture
def providers(monkeypatch):
    """AGENT_PROVIDER=fireworks with FALLBACK_PROVIDER=groq, and the two builders replaced by fakes. Returns the names asked for."""
    asked = {"fireworks": [], "groq": []}
    models = {"fireworks": None, "groq": None}

    def settle(primary=None, backup=None, fallback="groq", agent="fireworks"):
        models.update(fireworks=primary, groq=backup)
        monkeypatch.setenv("AGENT_PROVIDER", agent)
        monkeypatch.setenv("FALLBACK_PROVIDER", fallback)
        get_settings.cache_clear()
        for name in ("fireworks", "groq"):
            monkeypatch.setattr(llm_module, f"_{name}", lambda n, *a, _p=name: (asked[_p].append(n), models[_p])[1])
        return asked

    yield settle
    get_settings.cache_clear()


def test_the_model_name_is_translated_to_the_other_providers_id():
    f = llm_module.fallback_model_name
    assert f("accounts/fireworks/models/gpt-oss-120b", "groq") == "openai/gpt-oss-120b"
    assert f("accounts/fireworks/models/gpt-oss-20b", "groq") == "openai/gpt-oss-20b"
    assert f("openai/gpt-oss-120b", "fireworks") == "accounts/fireworks/models/gpt-oss-120b"
    assert f("some-other-model", "groq") == "some-other-model"


def test_without_a_fallback_provider_the_model_is_returned_as_it_was(providers):
    providers(primary=scripted(AIMessage(content="p")), fallback="")
    assert not isinstance(llm_module.get_llm(model="accounts/fireworks/models/gpt-oss-120b"), RunnableWithFallbacks)


def test_the_same_provider_as_fallback_is_no_fallback(providers):
    providers(primary=scripted(AIMessage(content="p")), fallback="fireworks")
    assert not isinstance(llm_module.get_llm(), RunnableWithFallbacks)


def test_an_unknown_fallback_provider_is_refused_at_once(providers):
    providers(primary=scripted(AIMessage(content="p")), fallback="openai")
    with pytest.raises(ValueError, match="FALLBACK_PROVIDER"):
        llm_module.get_llm()


@pytest.mark.parametrize("code", [503, 429])
def test_a_provider_outage_is_answered_by_the_backup_with_the_same_model_under_the_other_id(providers, code):
    primary = down(code)
    asked = providers(primary=primary, backup=scripted(AIMessage(content="from groq")))
    model = llm_module.get_llm(model="accounts/fireworks/models/gpt-oss-120b")
    assert model.invoke("hi").content == "from groq"
    assert len(primary.calls) == 1
    assert asked["fireworks"] == ["accounts/fireworks/models/gpt-oss-120b"] and asked["groq"] == ["openai/gpt-oss-120b"]


def test_a_healthy_primary_never_touches_the_backup(providers):
    backup = down(503)
    providers(primary=scripted(AIMessage(content="from fireworks")), backup=backup)
    assert llm_module.get_llm().invoke("hi").content == "from fireworks" and backup.calls == []


def test_a_bad_request_is_not_an_outage_and_is_not_sent_to_the_other_provider(providers):
    backup = scripted(AIMessage(content="should not be used"))
    providers(primary=down(400), backup=backup)
    with pytest.raises(openai.BadRequestError):
        llm_module.get_llm().invoke("hi")


def test_both_providers_down_raises_so_the_agent_can_report_a_technical_failure(providers):
    providers(primary=down(503), backup=down(503))
    with pytest.raises(openai.InternalServerError):
        llm_module.get_llm().invoke("hi")


def test_the_real_models_with_a_fallback_still_bind_tools_and_structured_output_on_both(monkeypatch):
    """create_agent and the router call these on the model they get. The real classes (built offline, no call is made)."""
    from app.agents.router import RoutingDecision

    monkeypatch.setenv("AGENT_PROVIDER", "fireworks")
    monkeypatch.setenv("FALLBACK_PROVIDER", "groq")
    get_settings.cache_clear()
    try:
        model = llm_module.get_llm(model="accounts/fireworks/models/gpt-oss-20b")
    finally:
        get_settings.cache_clear()
    assert isinstance(model, RunnableWithFallbacks)
    assert [type(model.runnable).__name__, *[type(f).__name__ for f in model.fallbacks]] == ["ChatOpenAI", "ChatGroq"]
    assert model.fallbacks[0].model_name == "openai/gpt-oss-20b"
    for bound in (model.bind_tools([]), model.with_structured_output(RoutingDecision, method="function_calling")):
        assert isinstance(bound, RunnableWithFallbacks) and len(bound.fallbacks) == 1  # the backup got the same binding


# ---- the turn ---------------------------------------------------------------------------------------------------
def run_turn(model, repo):
    """The real support node and the real routing rules (next_node, handoff_reason), a stub router and synthesizer."""
    reached = []

    def router(state):
        return {"plan": ["support"], "step": 0, "language": "English", "routing_reasoning": "t", "agent_outputs": {}, "final_answer": ""}

    def synthesizer(state):
        reached.append("synthesizer")
        text = state["agent_outputs"]["support"]["answer"]
        return {"final_answer": text, "messages": [AIMessage(content=text)]}

    def handoff(state):
        reached.append("handoff")
        return {"final_answer": "handed to a human", "messages": [AIMessage(content="handed to a human")]}

    graph = build_graph(router, lambda s: {}, make_support_node(llm=model, repo=repo), synthesizer, handoff)
    out = graph.invoke({"messages": [HumanMessage(content="is my terminal ok?")], "user_id": "cliente1988"},
                       config={"configurable": {"thread_id": "t"}})
    return out, reached


def backup_that_answers():
    return scripted(AIMessage(content="", tool_calls=[{"name": "get_terminal_status", "args": {}, "id": "c1"}]),
                    AIMessage(content="Your terminal is offline, it was last seen 95 minutes ago."))


def test_without_failover_a_provider_outage_ends_the_turn_in_the_human_queue(repo):
    """The control: this is what happened before M1."""
    out, reached = run_turn(down(503), repo)
    assert reached == ["handoff"] and out["final_answer"] == "handed to a human"


def test_with_failover_the_same_outage_is_answered_and_the_turn_never_reaches_a_human(providers, repo):
    providers(primary=down(503), backup=backup_that_answers())
    out, reached = run_turn(llm_module.get_llm(model="accounts/fireworks/models/gpt-oss-20b"), repo)
    assert reached == ["synthesizer"] and "offline" in out["final_answer"]
    assert handoff_reason(out) is None and next_node({**out, "step": 1}) == "synthesizer"
    assert out["agent_outputs"]["support"]["tool_calls"][0]["name"] == "get_terminal_status"
