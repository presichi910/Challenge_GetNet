"""LLM judge: scores one answer. Uses a different model family than the agents, to avoid bias."""
from __future__ import annotations

import json
import re

from langchain_core.messages import HumanMessage, SystemMessage

from app.llm import get_judge_llm
from app.prompts import JUDGE_PROMPT

SCORE_KEYS = ("relevance", "groundedness", "completeness", "tone")


def parse_judgement(text: str) -> dict:
    m = re.search(r"\{.*\}", text, flags=re.S)
    if not m:
        raise ValueError(f"no JSON in judge output: {text[:200]!r}")
    data = json.loads(m.group(0))
    for k in SCORE_KEYS:
        data[k] = max(1, min(5, int(data[k])))
    raw = data.get("unsupported_claims")
    claims = []
    for c in raw if isinstance(raw, list) else []:
        if isinstance(c, str) and c.strip():
            claims.append({"claim": c, "actionable": False})
        elif isinstance(c, dict) and c.get("claim"):
            claims.append({"claim": str(c["claim"]), "actionable": c.get("actionable") is True})
    data["unsupported_claims"] = claims
    # the caps are enforced here, so a model that lists a claim but still scores 5 cannot pass it
    cap = 3 if any(c["actionable"] for c in claims) else 4 if claims else 5
    data["groundedness"] = min(data["groundedness"], cap)
    data["language_match"] = str(data.get("language_match", True)).lower() != "false"
    ok = (data["relevance"] >= 4 and data["groundedness"] >= 4 and data["completeness"] >= 3
          and data["language_match"])
    data["verdict"] = "pass" if data.get("verdict") == "pass" and ok else "fail"  # only ever downgrades
    data.setdefault("reason", "")
    return data


class Judge:
    def __init__(self, llm=None):
        self.llm = llm or get_judge_llm(temperature=0.0, max_tokens=500)  # always Groq, whatever AGENT_PROVIDER

    def score(self, question: str, answer: str, evidence: list[str], criteria: str = "", callbacks=None) -> dict:
        ev = "\n\n".join(evidence) if evidence else "(no evidence: the assistant used no tools)"
        note = f"## Case-specific expected behavior (takes precedence)\n{criteria}\n\n" if criteria else ""
        user = f"{note}## User question\n{question}\n\n## Assistant answer\n{answer}\n\n## Evidence available to the assistant\n{ev}"
        last_err = None
        for _ in range(2):
            try:
                msg = self.llm.invoke([SystemMessage(content=JUDGE_PROMPT), HumanMessage(content=user)],
                                      config={"callbacks": callbacks} if callbacks else None)
                return parse_judgement(str(msg.content))
            except Exception as e:  # noqa: BLE001
                last_err = e
        return {"relevance": 0, "groundedness": 0, "completeness": 0, "tone": 0, "verdict": "error",
                "reason": f"judge_error: {last_err}"}
