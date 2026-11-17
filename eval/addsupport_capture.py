"""Which claims make the observe step consider adding the support agent, and does the account lexicon get them right? A wrapper, not a change.

    python -m eval.addsupport_capture --regular-only --replan-max 2 --label "replan on add_support" --repeats 3 --no-judge --pause 3 --retries 3 --require-clean

Every argument goes to `eval.run_eval` exactly as typed, through its own `main()`: the same service, the same graph, the same reports, the same
output (one line is added at the end: where the capture went). What this module adds, only inside this process and only until it ends:

  * `app.agents.observe.can_add_support` is replaced by a function that calls the original, returns EXACTLY what it returned (an exception
    goes through untouched) and writes one JSON line per call to `eval/reports/ADDSUPPORT-CAPTURE-<timestamp>.jsonl`:
        case, repetition, call (the n-th call inside that case), question (the customer's last message: the dataset's own text),
        claims [{text (complete), account (is_account_claim)}], can_add_support (the original's answer), has_user_id (a bool, never the id),
        support_in_plan, support_in_outputs, plan.
    The observe step only calls it when claims are still unsupported after the knowledge-base retry, so the file is the whole universe of
    claims the lexicon saw: the ones that fired the support agent and the ones that did not. Whether it then RAN is in the row's `replans`.
  * `eval.run_eval.evaluate` is wrapped to know which case is running and which repetition it is (no change to run_eval.py): the cases are
    handed over in an iterable that records the current one as the loop reaches it.

A failure to write a line never changes a result: it is reported once on stderr and the run goes on. Nothing from `.env`, no key, no user id.
The file is under `eval/reports/` (ignored by git): `git add -f` it when it should be kept.
"""
from __future__ import annotations

import contextlib
import json
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

REPORTS = Path(__file__).parent / "reports"


class _Cases:
    """The cases of one repetition: iterating them records the one the loop is on. `evaluate` only loops over them."""

    def __init__(self, cases: list[dict], ctx: "Capture"):
        self.cases, self.ctx = cases, ctx

    def __iter__(self) -> Iterator[dict]:
        for case in self.cases:
            self.ctx.case, self.ctx.calls_in_case = case.get("id", ""), 0
            yield case

    def __len__(self) -> int:
        return len(self.cases)


class Capture:
    """The context of the capture and the file it writes to. One instance per run."""

    def __init__(self, path: Path):
        self.path = path
        self.case, self.repetition, self.calls_in_case = "", 0, 0
        self.lines = 0
        self._lock = threading.Lock()
        self._warned = False

    def write(self, record: dict) -> None:
        try:
            line = json.dumps(record, ensure_ascii=False)
            with self._lock, self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
                self.lines += 1
        except Exception as e:  # noqa: BLE001 - the measurement must never change the result it measures
            if not self._warned:
                self._warned = True
                print(f"addsupport_capture: could not write {self.path} ({type(e).__name__}); the run goes on", file=sys.stderr)

    def wrap_can_add_support(self, original: Callable[[dict, list[str]], bool], is_account_claim: Callable[[str], bool]) -> Callable[[dict, list[str]], bool]:
        def captured(state: dict, claims: list[str]) -> bool:
            result = original(state, claims)  # an exception from the original goes straight through: nothing is recorded, nothing is swallowed
            self.calls_in_case += 1
            try:
                messages = state.get("messages") or []
                question = next((str(m.content) for m in reversed(messages) if getattr(m, "type", "") == "human"), "")
                self.write({"case": self.case, "repetition": self.repetition, "call": self.calls_in_case, "question": question,
                            "claims": [{"text": c, "account": bool(is_account_claim(c))} for c in claims],
                            "can_add_support": bool(result), "has_user_id": bool(state.get("user_id")),
                            "support_in_plan": "support" in (state.get("plan") or []),
                            "support_in_outputs": "support" in (state.get("agent_outputs") or {}),
                            "plan": list(state.get("plan") or [])})
            except Exception as e:  # noqa: BLE001 - same rule: building the line must not change the answer
                print(f"addsupport_capture: could not build a line ({type(e).__name__}); the run goes on", file=sys.stderr)
            return result

        return captured


@contextlib.contextmanager
def capturing(path: Path | None = None) -> Iterator[Capture]:
    """Patch `observe.can_add_support` and `run_eval.evaluate` for the duration of the block, and put both back afterwards (even on an error)."""
    from app.agents import observe
    from eval import run_eval

    path = path or REPORTS / f"ADDSUPPORT-CAPTURE-{datetime.now().strftime('%Y%m%d-%H%M%S')}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()  # a run in which the lexicon is never asked still leaves an (empty) file: "zero calls" is a result too
    cap = Capture(path)
    original_can, original_evaluate = observe.can_add_support, run_eval.evaluate

    def evaluate(cases: Iterable[dict], *args: Any, **kwargs: Any) -> Any:
        cap.repetition += 1
        return original_evaluate(_Cases(list(cases), cap), *args, **kwargs)

    observe.can_add_support = cap.wrap_can_add_support(original_can, observe.is_account_claim)  # type: ignore[assignment]
    run_eval.evaluate = evaluate  # type: ignore[assignment]
    try:
        yield cap
    finally:
        observe.can_add_support = original_can
        run_eval.evaluate = original_evaluate


def main(argv: list[str] | None = None) -> int:
    from eval import run_eval

    args = list(sys.argv[1:] if argv is None else argv)
    saved_argv = sys.argv
    sys.argv = ["eval.run_eval", *args]  # run_eval.main reads its own arguments from here
    try:
        with capturing() as cap:
            run_eval.main()
    finally:
        sys.argv = saved_argv
    print(f"\nAdd-support capture: {cap.lines} calls of can_add_support -> {cap.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
