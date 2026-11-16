"""Observe: look at the answer before it is sent, and decide what to do about what the evidence does not back.

It sits after the synthesizer (synthesizer -> observe -> verify). With REPLAN_MAX=0 it does nothing and the graph is
exactly what it was. With REPLAN_MAX >= 1 it audits the answer like the verifier does (same checker, same quote check)
and, if some claims have no support, it does NOT just delete them. It first tries once more to find the support:

  replan 1  retry_kb      ONE search of the knowledge base with the unsupported claims grouped into a single query (one
                          Voyage call), the same answer is audited again against the old evidence plus the new passages,
                          and only the claims that STILL have no support are deleted (one rewrite).
  replan 2  offer_ticket  (REPLAN_MAX >= 2) if the answer had steps (things to do) and none is left,
            or escalate   the customer is offered a support ticket. If NOTHING in the answer was backed, it is not sent:
                          the case goes to a human (handoff, reason agent_unresolved).

Decisions are made by code on the checker's result. The model only audits and rewrites; it never decides to replan.
It only acts on a turn that used Getnet's knowledge base or the support agent (a weather or exchange-rate answer is left to the
verify node: another Getnet search or a Getnet ticket would not help).
Everything fails open: if a model or the knowledge base fails, the answer goes on to the verify node as it was.
Everything it did is in `state["observe"]["replans"]` (also in the API response, `routing.replans`).

No threshold on retrieval scores: they do not separate answerable from unanswerable questions (see app.handoff).
The two numbers that exist are plain counts: any unsupported claim triggers the retry, and "nothing backed" means 0.
"""
from __future__ import annotations

import logging

from langchain_core.messages import AIMessage, RemoveMessage

from app.agents.verifier import (LANGUAGE_NAMES, MAX_EVIDENCE_CHARS, audit_claims, evidence_of, revised_note, rewrite_without,
                                 user_language)
from app.config import get_settings
from app.guard import leaked_run
from app.llm import get_llm
from app.prompts import FALLBACK_MESSAGE
from app.segments import IMPERATIVES, _starts_with_action_verb, split_segments  # noqa: F401 - IMPERATIVES documents the rule
import re

log = logging.getLogger(__name__)

QUERY_CHARS = 600  # the grouped claims, cut here: one embedding call
OFFERS = {  # the rewrite already says there are no documented steps; this only adds the way forward
    "en": "If you'd like, I can open a support ticket so a technician can look into this.",
    "pt": "Se quiser, posso abrir um chamado para que um técnico analise o caso.",
    "es": "Si quieres, puedo abrir un ticket para que un técnico lo revise.",
}


def is_step(segment: str) -> bool:
    """Something the customer is told to DO: it starts with an action verb, or it is a menu path. Figures and dates are facts,
    not fixes (a serial number or "95 minutes ago" is not a step), so they do not count: what the ticket offer is for is the
    answer that promised a fix and has none left."""
    return bool(re.search(r"→|\w\s>\s\w", segment)) or _starts_with_action_verb(segment)


def getnet_context(state: dict) -> bool:
    """Did this turn work with Getnet's own knowledge or the customer's account? A web answer (weather, exchange rates)
    gains nothing from another search of Getnet's knowledge base, and a Getnet ticket is no answer to it."""
    outs = state.get("agent_outputs") or {}
    return "support" in outs or any(tc.get("name") == "search_getnet_kb" for o in outs.values() for tc in o.get("tool_calls", []))


def has_steps(text: str) -> bool:
    return any(is_step(s) for s in split_segments(text) or [])


def format_hits(hits: list[tuple]) -> tuple[list[str], list[dict]]:
    """The passages as the knowledge tool shows them, and their sources (the same shape the API returns)."""
    blocks, sources, seen = [], [], set()
    for i, (doc, dist) in enumerate(hits, 1):
        url, title = doc.metadata.get("url", ""), doc.metadata.get("title", "")
        blocks.append(f"[{i}] (relevance={1 - dist:.2f}) {title} <{url}>\n{doc.page_content}")
        if url not in seen:
            seen.add(url)
            sources.append({"url": url, "title": title, "type": "kb", "relevance": round(1 - dist, 3)})
    return blocks, sources


def make_observe_node(llm=None, keep_raw: bool = False, retrieve_fn=None, replan_max: int | None = None):
    def observe_node(state: dict) -> dict:
        cap = get_settings().replan_max if replan_max is None else replan_max
        original = (state.get("final_answer") or "").strip()
        if (cap <= 0 or not original or original == FALLBACK_MESSAGE or state.get("synth_error") or state.get("handoff")
                or leaked_run(original, get_settings().prompt_leak_min_words)):  # a leak is the verify node's guard to stop
            return {}
        evidence = evidence_of(state)
        if not evidence or not getnet_context(state):
            return {}  # nothing to check against, or a web answer: the verify node deletes what is unbacked, nothing more

        messages = state.get("messages") or []
        target = user_language(messages)
        language = LANGUAGE_NAMES.get(target) or state.get("language") or "English"
        code = target or {"english": "en", "portuguese": "pt", "spanish": "es"}.get(str(language).lower(), "en")
        observed: dict = {"replans": [], "evidence": [], "sources": [], "tool_calls": []}
        try:
            model = llm or get_llm(temperature=0.0, model=get_settings().light_model)
            claims, raw, supported = audit_claims(model, original, evidence, messages, keep_raw=keep_raw)
            if claims and cap >= 1:  # replan 1: look for the support once more before deleting anything
                first = len(claims)
                try:
                    query = " ; ".join(claims)[:QUERY_CHARS]
                    hits = (retrieve_fn or _retrieve)(query, get_settings().retrieval_k)
                    blocks, sources = format_hits(hits)
                    observed["tool_calls"].append({"name": "search_getnet_kb", "args": {"query": query}})
                    if blocks:
                        observed["evidence"], observed["sources"] = blocks, sources
                        combined = (evidence + "\n\n" + "\n\n".join(blocks))[:MAX_EVIDENCE_CHARS * 2]
                        claims, raw2, supported = audit_claims(model, original, combined, messages, keep_raw=keep_raw)
                        raw = raw2 or raw
                    observed["replans"].append(f"retry_kb: {first} unsupported -> {len(blocks)} passages -> {len(claims)} still unsupported")
                except Exception as e:  # noqa: BLE001 - the knowledge base or the checker failed: go on with what we had
                    log.warning("observe: retry of the knowledge base failed (%s); keeping the original claims", type(e).__name__)
                    observed["replans"].append(f"retry_kb_failed: {type(e).__name__}")
            observed["claims"] = list(claims)
            update: dict = {"observe": observed}
            if keep_raw:
                update["verify_raw"] = raw
            answer, note = original, ""
            if claims and cap >= 2 and supported == 0 and has_steps(original):
                observed["replans"].append("escalate: nothing in the answer is backed by the evidence")
                observed["escalate"] = True
                update["verify_note"] = f"escalated; {revised_note(claims)}"
                last = messages[-1] if messages else None
                if last is not None and getattr(last, "type", "") == "ai":
                    update["messages"] = [RemoveMessage(id=last.id)]  # the draft is not kept in the conversation
                return update
            if claims:
                answer = rewrite_without(model, original, claims, language)
                note = revised_note(claims)
                if cap >= 2 and has_steps(original) and not has_steps(answer):
                    answer = answer.rstrip() + "\n\n" + OFFERS.get(code, OFFERS["en"])
                    observed["replans"].append("offer_ticket: no step is left")
                    note += "; offered_ticket"
            update["verify_note"] = note
            if answer != original:
                update["final_answer"] = answer
                if messages and getattr(messages[-1], "type", "") == "ai":
                    update["messages"] = [AIMessage(content=answer, id=messages[-1].id)]  # same id: add_messages replaces it
            return update
        except Exception:  # noqa: BLE001 - fail open: the verify node audits it on its own
            log.exception("observe: could not audit the answer, leaving it to the verify node")
            return {}

    return observe_node


def _retrieve(query: str, k: int) -> list[tuple]:
    from app.tools.knowledge_tools import retrieve  # imported late: it needs Chroma and the Voyage client

    return retrieve(query, k)
