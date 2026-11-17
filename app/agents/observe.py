"""Observe: look at the answer before it is sent, and decide what to do about what the evidence does not back.

It sits after the synthesizer (synthesizer -> observe -> verify). With REPLAN_MAX=0 it does nothing and the graph is
exactly what it was. With REPLAN_MAX >= 1 it audits the answer like the verifier does (same checker, same quote check)
and, if some claims have no support, it does NOT just delete them. It first tries once more to find the support:

  replan 1  retry_kb      ONE search of the knowledge base with the unsupported claims grouped into a single query (one
                          Voyage call), the same answer is audited again against the old evidence plus the new passages,
                          and only the claims that STILL have no support are deleted (one rewrite).
  replan 2  ONE action (REPLAN_MAX >= 2; any larger value behaves exactly like 2), the first that applies:
            add_support   the router sent only the knowledge agent, claims are still unsupported and one of them is about
                          the customer's own account (`is_account_claim`): the support agent runs with the customer's user
                          id, the two answers are merged by the synthesizer, and the merge is audited again.
            offer_ticket  otherwise, if the answer had steps (things to do) and none is left, the customer is offered a
            or escalate   support ticket. If NOTHING in the answer was backed, it is not sent: the case goes to a human
                          (handoff, reason agent_unresolved).
            add_support never goes together with offer_ticket or escalate in the same turn (one action per turn); if the
            added support agent itself cannot resolve the case, the existing handoff edge takes it. If add_support FAILS
            (support, the merge or its audit), nothing was done: the turn goes on to the ticket offer or the escalation.

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
import unicodedata
from typing import Callable

from langchain_core.messages import AIMessage, RemoveMessage

from app.agents.verifier import (LANGUAGE_NAMES, MAX_EVIDENCE_CHARS, audit_claims, evidence_of, revised_note, rewrite_without,
                                 user_language)
from app.config import get_settings
from app.guard import leaked_run
from app.handoff import handoff_reason
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


# "Is this claim about the customer's own account?" A plain lexicon, no model: a possessive or a "you have" form together with
# a noun of the account (plan, balance, payout, terminal, fee...) or a figure, in a statement that is not a step (a step such as
# "check your plan" is a how-to, not data about the customer). Accents are folded, so one pattern serves en / pt / es.
POSSESSIVE = re.compile(r"\b(your|yours|you have|you've|you are|you're|seu|sua|seus|suas|voce tem|voce possui|voce esta|"
                        r"tu|tus|tienes|usted tiene|su|sus)\b")
ACCOUNT_NOUNS = re.compile(
    r"\b(plan|plano|account|accounts|conta|contas|cuenta|cuentas|balance|saldo|payouts?|repasses?|pagos?|cobros?|receivables?|"
    r"recebiveis|recebivel|bank|banco|pix key|chave pix|llave pix|terminals?|maquininha|equipo|devices?|"
    r"transactions?|transacoes|transacao|transacciones|transaccion|sales|vendas|ventas|limits?|limite|limites|rates?|taxas?|tasas?|fees?|"
    r"comision|comissao|status|estado|profile|perfil|tickets?|chamados?|chargebacks?|estornos?|contracargos?|settlement|liquidacao|"
    r"liquidacion|store|loja|tienda|business|negocio|empresa|email|e-mail|correo|address|endereco|direccion|model|modelo|"
    r"serial|firmware|merchant)\b")
FIGURE = re.compile(r"\d|r\$|brl")


def _fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text.lower()) if unicodedata.category(c) != "Mn")


def is_account_claim(claim: str) -> bool:
    if is_step(claim):
        return False
    folded = _fold(claim)
    return bool(POSSESSIVE.search(folded) and (ACCOUNT_NOUNS.search(folded) or FIGURE.search(folded)))


def can_add_support(state: dict, claims: list[str]) -> bool:
    """The router did not send the support agent, there is a customer to look up, the turn used the knowledge base (the caller
    already checked getnet_context) and at least one claim that is still unsupported is about that customer's account."""
    outs, plan = state.get("agent_outputs") or {}, state.get("plan") or []
    return bool(state.get("user_id")) and "support" not in outs and "support" not in plan and any(is_account_claim(c) for c in claims)


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


def make_observe_node(llm=None, keep_raw: bool = False, retrieve_fn=None, replan_max: int | None = None,
                      support_fn: Callable | None = None, synth_fn: Callable | None = None):
    """`support_fn` and `synth_fn` are the graph's own support and synthesizer nodes. Without them the add_support action does
    not exist (a node built by hand, as most tests do, behaves as it did)."""

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
        language = LANGUAGE_NAMES.get(target or "") or state.get("language") or "English"
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
            if claims and cap >= 2 and support_fn and synth_fn and can_add_support(state, claims):
                added = _add_support(state, observed, claims, model, language, keep_raw, support_fn, synth_fn)
                if added is not None:  # replan 2 is ONE action, and this was it
                    update.update(added)
                    return update
                # it failed (the trace says why in observed["replans"]): nothing was spent, go on with the ticket offer or the escalation
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


def _add_support(state: dict, observed: dict, claims: list[str], model, language: str, keep_raw: bool,
                 support_fn: Callable, synth_fn: Callable) -> dict | None:
    """Run the support agent the router did not send, merge its answer with the knowledge agent's (the synthesizer's own merge
    prompt) and audit the merge against all the evidence. -> the state update, or None if anything failed (the caller then goes
    on with the answer it had; what failed is in `observed["replans"]`).

    The support agent starts with NO findings from the draft (its unsupported claims must not become its "prior context") and
    without the draft in its dialogue. It has all its tools and the authenticated user id, exactly as when the router sends it:
    a ticket it proposes is recorded by the last node, and if it escalates, the existing handoff edge takes the case."""
    messages = state.get("messages") or []
    draft = messages[-1] if messages and getattr(messages[-1], "type", "") == "ai" else None
    earlier = messages[:-1] if draft is not None else list(messages)
    outputs = state.get("agent_outputs") or {}
    try:
        got = support_fn({**state, "messages": earlier, "agent_outputs": {}})
        result = (got.get("agent_outputs") or {}).get("support") or {}
        if result.get("error") or not result.get("answer"):
            raise RuntimeError(result.get("error") or "empty_answer")
        merged = {"support": result, **outputs}  # support first, as when the router plans both
        grown = {**state, "agent_outputs": merged, "plan": ["support", *[p for p in state.get("plan") or [] if p != "support"]]}
        tools = f"{len(result.get('tool_calls') or [])} tool calls"
        if handoff_reason(grown):  # the support agent could not resolve it either: a person takes the case, no merge needed
            observed["replans"].append(f"add_support: support ran ({tools}) -> it could not resolve the case -> escalate")
            observed["escalate"] = True
            update: dict = {"agent_outputs": merged, "verify_note": "escalated; added_support"}
            if draft is not None:
                update["messages"] = [RemoveMessage(id=draft.id)]  # the draft is not kept in the conversation
            return update
        merge = synth_fn({**grown, "messages": earlier})
        answer = str(merge.get("final_answer") or "").strip()
        if merge.get("synth_error") or not answer:
            raise RuntimeError("merge_failed")
        combined = (evidence_of(grown) + "\n\n" + "\n\n".join(observed.get("evidence") or []))[:MAX_EVIDENCE_CHARS * 2]
        new_claims, raw, _ = audit_claims(model, answer, combined, messages, keep_raw=keep_raw)
        if new_claims:
            answer = rewrite_without(model, answer, new_claims, language)
    except Exception as e:  # noqa: BLE001 - fail open: the answer and the state stay as they were
        log.warning("observe: adding the support agent failed (%s); keeping the answer as it was", type(e).__name__)
        observed["replans"].append(f"add_support_failed: {type(e).__name__}")
        return None
    observed["claims"] = list(new_claims)
    observed["replans"].append(f"add_support: {len(claims)} unsupported -> support ran ({tools}) -> merged -> {len(new_claims)} still unsupported")
    note = (revised_note(new_claims) + "; " if new_claims else "") + "added_support"
    out: dict = {"agent_outputs": merged, "final_answer": answer, "verify_note": note}
    if keep_raw:
        out["verify_raw"] = raw
    if draft is not None:
        out["messages"] = [AIMessage(content=answer, id=draft.id)]  # same id: add_messages replaces the draft
    return out


def _retrieve(query: str, k: int) -> list[tuple]:
    from app.tools.knowledge_tools import retrieve  # imported late: it needs Chroma and the Voyage client

    return retrieve(query, k)
