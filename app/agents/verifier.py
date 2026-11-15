"""Verify node: the last check before the answer leaves the graph (synthesizer -> verify -> END).

1. Groundedness, by citation. A cheap model lists every statement in the answer (step, figure, date, capability,
   condition) with the EXACT phrase of the evidence that backs it ("" if none). The model is not trusted to
   judge: the CODE checks that the quote really is in the evidence (`quote_in`). A statement without a valid
   quote is unsupported. If there are any, the answer is rewritten ONCE with those statements DELETED (not softened).
   In "segments" mode (VERIFIER_MODE=segments) the model does not choose what to audit: the code splits the answer
   (app.segments), the model must answer for EVERY numbered segment, and a segment it skips counts as unsupported.
2. Language. The synthesizer passes a single agent's answer through unchanged, so nothing forced the
   customer's language. If the answer is clearly in another language than the customer's own words
   (detected in code, see app.lang), it is translated, once.

Both checks fail open: if a model call fails, the answer is left as it was. The prompts live here and not in
app.prompts on purpose, so the prompt-hash pin test keeps meaning "the prompts the eval was run with".
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app.config import get_settings
from app.guard import leaked_run, refusal
from app.lang import detect_language
from app.llm import get_llm
from app.prompts import FALLBACK_MESSAGE
from app.segments import may_skip_evidence, split_segments

log = logging.getLogger(__name__)

LANGUAGE_NAMES = {"en": "English", "pt": "Portuguese", "es": "Spanish"}
MAX_EVIDENCE_CHARS = 24000
DIALOGUE_TURNS = 4

VERIFY_PROMPT = """\
You audit a customer-support answer against the evidence it was written from.

List every statement of the answer that the customer could act on or rely on: each step, menu path, figure, \
fee, date, deadline, product capability and eligibility condition. For each one, give in `evidence_quote` the \
EXACT phrase copied from the evidence that supports it, word for word. If the evidence has no such phrase, \
leave `evidence_quote` empty: do not paraphrase, do not stitch phrases together, do not quote the answer itself. \
A condition stated as a general rule when the evidence ties it to a specific case (a bank, a plan, a mode) has \
no supporting phrase.

Do NOT list: polite phrases, offers of further help, honest statements that something could not be confirmed, \
what the customer said in the conversation, or what the conversation already established."""

REVISE_PROMPT = """\
Rewrite the customer-support answer below in {language}. DELETE each of these statements, which the evidence \
does not support. Delete them; do not soften, hedge or replace them:
{claims}
Keep every other statement exactly as it is, in the same tone and not longer than before: the facts the \
evidence supports (the customer's own account data, what the tools returned) must stay. \
Then read the result once more. If a sentence still points at what you deleted (for example "these steps", \
"after these actions", "the list above", "if that does not work"), delete or reword that sentence so that \
nothing refers to missing content. \
If, after the deletions, the answer gives the customer nothing to do (no step, figure, date or condition to \
act on), add one sentence saying that there are no documented steps for this, and offer to help further. \
Say nothing new. Output only the rewritten answer."""

TRANSLATE_PROMPT = """\
Translate the customer-support message below into {language}. Keep every number, date, currency amount, \
product name and reference id exactly as written. Output only the translation."""


class Statement(BaseModel):
    statement: str = Field(description="The statement of the answer, briefly.")
    evidence_quote: str = Field(default="", description="Exact phrase of the evidence that supports it; empty if none.")


class Verification(BaseModel):
    statements: list[Statement] = Field(default_factory=list)


def _words(text: str) -> list[str]:
    """Lowercase, accent-free, punctuation-free words: what two texts are compared on."""
    plain = unicodedata.normalize("NFKD", (text or "").casefold())
    plain = "".join(c for c in plain if not unicodedata.combining(c))
    return re.findall(r"\w+", plain)


def _trigrams(words: list[str]) -> set[tuple[str, ...]]:
    return {tuple(words[i:i + 3]) for i in range(len(words) - 2)}


QUOTE_MIN_WORDS = 3      # a shorter quote proves nothing unless it is literal
QUOTE_MIN_OVERLAP = 0.6  # share of the quote's word triplets that must appear in the evidence


def quote_in(quote: str, evidence: str) -> bool:
    """Is `quote` really in `evidence`? A normalized substring, or (for 3+ words) >= 60% of its word triplets."""
    q, e = _words(quote), _words(evidence)
    if not q or not e:
        return False
    if f" {' '.join(q)} " in f" {' '.join(e)} ":
        return True
    if len(q) < QUOTE_MIN_WORDS:
        return False
    grams = _trigrams(q)
    return len(grams & _trigrams(e)) / len(grams) >= QUOTE_MIN_OVERLAP


def unsupported_statements(statements: list[Statement], evidence: str) -> list[str]:
    return [s.statement.strip() for s in statements if s.statement.strip() and not quote_in(s.evidence_quote, evidence)]


def user_language(messages) -> str | None:
    """Language of the customer's own words: the last message, or the last two when it is too short to tell."""
    texts = [str(m.content) for m in messages if getattr(m, "type", "") == "human"]
    if not texts:
        return None
    return detect_language(texts[-1]) or detect_language(" ".join(texts[-2:]))


def evidence_of(state: dict) -> str:
    parts = [c for out in (state.get("agent_outputs") or {}).values() for c in (out.get("contexts") or [])]
    return "\n\n".join(parts)[:MAX_EVIDENCE_CHARS]


def dialogue_of(messages) -> str:
    """The turns before the final answer, so 'the second one' is not flagged as invented."""
    earlier = [m for m in messages[:-1] if getattr(m, "type", "") in ("human", "ai")][-DIALOGUE_TURNS:]
    return "\n".join(f"{'Customer' if m.type == 'human' else 'Assistant'}: {m.content}" for m in earlier)


SEGMENT_VERIFY_PROMPT = """\
You audit a customer-support answer against the evidence it was written from. The answer is split into numbered \
segments. Return ONE entry for EVERY number from 1 to the last one, in order, none skipped.

For each segment:
- is_claim = false ONLY for a greeting, thanks, an apology, or a closing or offer of help that states no fact, \
number, step, date, fee, capability or condition. Everything else is a claim (is_claim = true): steps, menu paths, \
figures, dates, fees, capabilities, eligibility conditions, any statement about the customer's own data and any \
line that introduces a step.
- For a claim, evidence_quote = the EXACT phrase copied from the evidence, word for word, that supports it. If the \
evidence has no such phrase, leave it empty: do not paraphrase, do not stitch phrases together, do not quote the \
answer itself. A condition stated as a general rule when the evidence ties it to a specific case has no supporting \
phrase. What the customer said in the conversation may be quoted for what the customer said, and for nothing else."""


class SegmentAudit(BaseModel):
    id: int = Field(description="The number of the segment, as given.")
    is_claim: bool = Field(default=True, description="False only for a greeting, thanks, apology or closing.")
    evidence_quote: str = Field(default="", description="Exact phrase of the evidence that supports it; empty if none.")


class SegmentedVerification(BaseModel):
    segments: list[SegmentAudit] = Field(default_factory=list)


def customer_said(messages) -> str:
    """What the customer wrote in this conversation: a quote of it may back a segment that repeats it."""
    return "\n".join(str(m.content) for m in messages if getattr(m, "type", "") == "human")


def audit_segments(segments: list[str], audit: SegmentedVerification, evidence: str, said: str) -> tuple[list[str], dict]:
    """The segments the evidence does not back, and the whole table (for verify_raw). The code decides each one:
    a segment is fine if it is a closing the code recognises, or if its quote is really in the evidence (or in what
    the customer said). A segment the model did not answer is unsupported: `omitted`."""
    answered: dict[int, SegmentAudit] = {}
    for a in audit.segments:
        answered.setdefault(a.id, a)  # the first answer for a number wins
    source = f"{evidence}\n{said}"
    rows, unsupported, omitted = [], [], []
    for i, text in enumerate(segments, 1):
        a = answered.get(i)
        if a is None:
            omitted.append(i)
            unsupported.append(text)
            rows.append({"id": i, "text": text, "omitted": True})
            continue
        honored = (not a.is_claim) and may_skip_evidence(text)  # the label is only believed for a real closing
        found = quote_in(a.evidence_quote, source)
        rows.append({"id": i, "text": text, "is_claim": a.is_claim, "label_honored": honored,
                     "evidence_quote": a.evidence_quote, "quote_found": found})
        if not (honored or found):
            unsupported.append(text)
    return unsupported, {"mode": "segments", "segments": rows, "omitted": omitted}


def raw_audit(statements: list[Statement], evidence: str) -> str:
    """What the checker returned, plus what the code decided about each quote. For the eval only (see keep_raw)."""
    return json.dumps({"statements": [{"statement": s.statement, "evidence_quote": s.evidence_quote,
                                       "quote_found": quote_in(s.evidence_quote, evidence)} for s in statements]},
                      ensure_ascii=False)


def make_verify_node(llm=None, keep_raw: bool = False, mode: str | None = None):
    """`keep_raw=True` (only the eval asks for it) also returns `verify_raw`: the checker's raw statements and quotes.
    It is never set otherwise: the state is checkpointed, and the API and the logs must not carry evidence text.
    `mode` ("statements" or "segments") overrides the VERIFIER_MODE setting (the eval compares both)."""

    def verify_node(state: dict) -> dict:
        original = (state.get("final_answer") or "").strip()
        raw = ""
        if not original or original == FALLBACK_MESSAGE or state.get("synth_error"):
            return {"verify_note": "", **({"verify_raw": ""} if keep_raw else {})}

        messages = state.get("messages") or []
        target = user_language(messages)
        language = LANGUAGE_NAMES.get(target) or state.get("language") or "English"
        answer, notes, model = original, [], llm

        if leaked_run(original, get_settings().prompt_leak_min_words):  # the system's own instructions in the answer
            log.warning("verify: the answer repeated the system prompt; replaced by a refusal")
            answer = refusal(target or language)
            update: dict = {"verify_note": "guard:prompt_leak", "final_answer": answer}
            if keep_raw:
                update["verify_raw"] = ""
            if messages and getattr(messages[-1], "type", "") == "ai":
                update["messages"] = [AIMessage(content=answer, id=messages[-1].id)]
            return update

        evidence = evidence_of(state)
        if evidence:
            try:
                model = model or get_llm(temperature=0.0, model=get_settings().light_model)
                segments = split_segments(answer) if (mode or get_settings().verifier_mode) == "segments" else None
                if segments:  # the code chose what to audit: every segment, none can be skipped
                    checker = model.with_structured_output(SegmentedVerification, method="function_calling")
                    numbered = "\n".join(f"[{i}] {t}" for i, t in enumerate(segments, 1))
                    body = (f"## Conversation so far\n{dialogue_of(messages)}\n\n## Evidence\n{evidence}\n\n"
                            f"## Answer, in {len(segments)} numbered segments\n{numbered}")
                    audit = checker.invoke([SystemMessage(content=SEGMENT_VERIFY_PROMPT), HumanMessage(content=body)])
                    claims, table = audit_segments(segments, audit, evidence, customer_said(messages))
                    if keep_raw:
                        raw = json.dumps(table, ensure_ascii=False)
                else:  # the original check: the model lists the statements it finds
                    checker = model.with_structured_output(Verification, method="function_calling")
                    body = f"## Conversation so far\n{dialogue_of(messages)}\n\n## Evidence\n{evidence}\n\n## Answer\n{answer}"
                    audit = checker.invoke([SystemMessage(content=VERIFY_PROMPT), HumanMessage(content=body)])
                    if keep_raw:
                        raw = raw_audit(audit.statements, evidence)
                    claims = unsupported_statements(audit.statements, evidence)
                if claims:
                    listed = "\n".join(f"- {c}" for c in claims)
                    msg = model.invoke([SystemMessage(content=REVISE_PROMPT.format(language=language, claims=listed)),
                                        HumanMessage(content=answer)])
                    answer = str(msg.content).strip() or answer
                    notes.append(f"revised:{len(claims)} [{' | '.join(c[:80] for c in claims)}]")
            except Exception as e:  # noqa: BLE001
                log.exception("verify: groundedness check failed, keeping the answer")
                if keep_raw and not raw:
                    raw = json.dumps({"error": type(e).__name__})

        try:
            shown = detect_language(answer)
            if target and shown and shown != target:
                model = model or get_llm(temperature=0.0, model=get_settings().light_model)
                msg = model.invoke([SystemMessage(content=TRANSLATE_PROMPT.format(language=language)),
                                    HumanMessage(content=answer)])
                answer = str(msg.content).strip() or answer
                notes.append(f"translated:{shown}->{target}")
        except Exception:  # noqa: BLE001
            log.exception("verify: language check failed, keeping the answer")

        update: dict = {"verify_note": "; ".join(notes)}
        if keep_raw:
            update["verify_raw"] = raw
        if answer != original:
            update["final_answer"] = answer
            if messages and getattr(messages[-1], "type", "") == "ai":
                update["messages"] = [AIMessage(content=answer, id=messages[-1].id)]  # same id: add_messages replaces it
        return update

    return verify_node
