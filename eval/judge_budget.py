"""Keeps the judge inside Groq's free-tier limits, from OUTSIDE `eval/judge.py` (which is frozen: the calibration depends on it).

The judge (`qwen/qwen3.8-27b` on Groq, free tier) has THREE limits: 8,000 tokens a minute, 1,000 requests a day and 200,000 tokens a
rolling day. The headers show only the first two; the daily one appears in the 429 message. So every call's tokens go into a LEDGER on
disk (`eval/reports/judge_ledger.json`): the rolling 24 h total is known across runs, and a run stops BEFORE the cap, not on the error.

    scorer = BudgetedScorer(judge, Ledger())
    result, status = scorer.score(question, answer, evidence, criteria)      # status: "ok" | "daily_cap" | "error"

What it adds around `Judge.score` (which returns a `verdict: "error"` row instead of raising):
  * pacing: it waits until the last minute's tokens leave room for the next call (no 429 to begin with);
  * a per-minute 429 is waited out (the "try again in Xs" hint) and the call retried; a daily 429 stops the run, never retried;
  * an answer the judge could not turn into JSON is retried (it costs tokens) a limited number of times, then reported as an error:
    counted apart, never as a fail and never averaged in.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

DEFAULT_LEDGER = Path(__file__).parent / "reports" / "judge_ledger.json"
DAY = 86400.0
DAILY_LIMIT = 200_000  # tokens, rolling 24 h (measured 2026-11-15, see CLAUDE.md)
MARGIN = 10_000  # stop this far below the cap: the estimate of the next call can be off
MINUTE_LIMIT = 8_000
MINUTE_USE = 0.85  # use at most this share of the per-minute budget
DEFAULT_ESTIMATE = 2_400  # tokens of one call before there is any history (they measure ~1.9K, long evidence more)


class Ledger:
    """Tokens spent by the judge, one entry per call, in a JSON file. Several runs (and the replan measurement) share it."""

    def __init__(self, path: str | Path | None = None, clock=time.time):
        self.path = Path(path) if path else DEFAULT_LEDGER
        self.clock = clock
        self.entries: list[dict] = []
        if self.path.exists():
            try:
                self.entries = json.loads(self.path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                self.entries = []  # a damaged ledger must not stop a run; it is rebuilt from here on

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.entries), encoding="utf-8")
        os.replace(tmp, self.path)

    def record(self, tokens: int, label: str = "") -> None:
        self.entries.append({"at": self.clock(), "tokens": int(tokens), "label": label})
        cutoff = self.clock() - 2 * DAY  # nothing older than the window matters
        self.entries = [e for e in self.entries if e["at"] >= cutoff]
        self._save()

    def spent(self, window_s: float = DAY) -> int:
        now = self.clock()
        return sum(e["tokens"] for e in self.entries if now - e["at"] < window_s)

    def recent_average(self, n: int = 10) -> int:
        tokens = [e["tokens"] for e in self.entries[-n:] if e["tokens"] > 0]
        return int(sum(tokens) / len(tokens)) if tokens else DEFAULT_ESTIMATE


def is_rate_limit(reason: str) -> bool:
    return bool(re.search(r"429|rate.?limit|too many requests|tokens per minute|\(TPM\)|requests per minute|\(RPM\)", reason or "", re.I))


def is_daily(reason: str) -> bool:
    return bool(re.search(r"per day|\(TPD\)|\(RPD\)", reason or "", re.I))


def wait_hint(reason: str, floor: float = 5.0, cap: float = 70.0) -> float:
    """Seconds to wait after a per-minute limit: the 'try again in 7.2s' hint plus a margin."""
    ms = re.search(r"try again in ([\d.]+)ms", reason or "")
    m = re.search(r"try again in (?:(\d+)m(?!s))?(?:([\d.]+)s)?", reason or "")
    hinted = float(ms.group(1)) / 1000 if ms else (int(m.group(1) or 0) * 60 + float(m.group(2) or 0)) if m else 0.0
    return min(cap, max(floor, hinted + 2))


class BudgetedScorer:
    def __init__(self, judge, ledger: Ledger, *, daily_limit: int = DAILY_LIMIT, margin: int = MARGIN, minute_limit: int = MINUTE_LIMIT,
                 rate_retries: int = 4, parse_retries: int = 2, sleep=time.sleep, collector_factory=None):
        self.judge, self.ledger = judge, ledger
        self.ceiling = daily_limit - margin
        self.minute_budget = int(minute_limit * MINUTE_USE)
        self.rate_retries, self.parse_retries = rate_retries, parse_retries
        self.sleep = sleep
        self.collector_factory = collector_factory
        self.calls = 0
        self.tokens = 0  # spent by THIS scorer

    def room(self) -> int:
        """Tokens left before the stop line."""
        return self.ceiling - self.ledger.spent()

    def can_afford_next(self) -> bool:
        return self.room() >= self.ledger.recent_average()

    def _pace(self) -> None:
        """Wait until the tokens spent in the last minute leave room for another call."""
        est = self.ledger.recent_average()
        for _ in range(8):
            used = self.ledger.spent(60.0)
            if used + est <= self.minute_budget:
                return
            oldest = min((e["at"] for e in self.ledger.entries if self.ledger.clock() - e["at"] < 60.0), default=self.ledger.clock())
            self.sleep(max(1.0, 60.0 - (self.ledger.clock() - oldest) + 0.5))

    def _call(self, question, answer, evidence, criteria):
        collector = self.collector_factory() if self.collector_factory else None
        try:
            out = self.judge.score(question, answer, evidence, criteria, callbacks=[collector]) if collector else \
                self.judge.score(question, answer, evidence, criteria)
        except TypeError:  # a judge whose score() takes no callbacks
            out = self.judge.score(question, answer, evidence, criteria)
        used = 0
        if collector is not None and getattr(collector, "spans", None):
            used = int(collector.usage().get("total_tokens") or 0)
        return out, used

    def score(self, question: str, answer: str, evidence: list[str], criteria: str = "", label: str = "") -> tuple[dict | None, str]:
        """(judgement, status). status: "ok", "daily_cap" (nothing was spent; stop the run) or "error" (the judgement is an error row)."""
        parse_left, rate_left = self.parse_retries, self.rate_retries
        while True:
            if not self.can_afford_next():
                return None, "daily_cap"
            self._pace()
            out, used = self._call(question, answer, evidence, criteria)
            spent = used or (0 if out.get("verdict") == "error" and is_rate_limit(out.get("reason", "")) else self.ledger.recent_average())
            if spent:
                self.ledger.record(spent, label)
                self.tokens += spent
            self.calls += 1
            if out.get("verdict") != "error":
                return out, "ok"
            reason = out.get("reason", "")
            if is_daily(reason):  # the provider says the day is over: believe it over the ledger
                return None, "daily_cap"
            if is_rate_limit(reason):
                if rate_left <= 0:
                    return out, "error"
                rate_left -= 1
                self.sleep(wait_hint(reason))
                continue
            if parse_left <= 0:
                return out, "error"
            parse_left -= 1
