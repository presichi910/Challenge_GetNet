"""The SLA of the human queue: a handoff nobody claims within the limit is escalated once (priority high, an audit event, a webhook)."""
import sqlite3
import time

import pytest

from app.handoff_store import HandoffStore
from app.main import _summary_fields, sweep_once
from app.schemas import HandoffSummary

SLA = 30 * 60


def record(hid, reason="agent_unresolved"):
    return {"handoff_id": hid, "created_at": "2026-11-15T20:00:00+00:00", "user_id": "cliente1988", "conversation_id": "c1", "reason": reason,
            "priority": "normal", "ticket_id": None, "language": "English", "user_message": "help", "conversation": [], "agent_findings": {}, "tool_calls": []}


@pytest.fixture
def store(tmp_path):
    s = HandoffStore(tmp_path / "h.sqlite", lease_seconds=900)
    yield s
    s.close()


def events(store, hid):
    return [r["action"] for r in store._read("SELECT action FROM handoff_events WHERE handoff_id = ? ORDER BY id", (hid,))]


def test_an_unclaimed_handoff_past_the_sla_is_escalated_once_with_high_priority_and_an_audit_event(store):
    store.notify(record("HND-AAAAAA"))
    t0 = time.time()
    assert store.escalate_overdue(SLA, now=t0 + SLA - 60) == []  # not yet
    out = store.escalate_overdue(SLA, now=t0 + SLA + 60)
    assert [o["id"] for o in out] == ["HND-AAAAAA"] and out[0]["priority"] == "high" and out[0]["waited_s"] >= SLA
    row = store.get("HND-AAAAAA")
    assert row["priority"] == "high" and row["escalated_at"] and "sla_escalated" in events(store, "HND-AAAAAA")
    assert store.escalate_overdue(SLA, now=t0 + 5 * SLA) == []  # once: a second sweep does not announce it again


def test_a_case_somebody_holds_or_that_is_resolved_is_not_escalated(store):
    for hid in ("HND-CLAIMD", "HND-RESOLV", "HND-WAITIN"):
        store.notify(record(hid))
    store.claim("HND-CLAIMD", "ops1")
    store.claim("HND-RESOLV", "ops1")
    store.reply("HND-RESOLV", "ops1", "approve", "Done", "key-0000001")
    late = time.time() + SLA + 60
    # the claim of HND-CLAIMD runs out after 15 min: by `late` it is back in the queue, unclaimed and old, and the SLA applies to it
    out = {o["id"] for o in store.escalate_overdue(SLA, now=late)}
    assert out == {"HND-CLAIMD", "HND-WAITIN"} and store.get("HND-RESOLV")["escalated_at"] is None


def test_a_case_claimed_just_before_the_sweep_is_not_escalated(store):
    store.notify(record("HND-RACE01"))
    store.claim("HND-RACE01", "ops1")
    assert store.escalate_overdue(SLA, now=time.time() + 60) == []  # held, young claim: nothing to escalate


def test_the_sla_can_be_turned_off(store):
    store.notify(record("HND-AAAAAA"))
    assert store.escalate_overdue(0, now=time.time() + 10 ** 6) == [] and store.get("HND-AAAAAA")["escalated_at"] is None


def test_an_existing_queue_file_from_before_the_sla_gets_its_column(tmp_path):
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript("""CREATE TABLE handoffs (id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, merchant_id TEXT NOT NULL, reason TEXT NOT NULL,
        priority TEXT NOT NULL DEFAULT 'normal', summary TEXT NOT NULL, record TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
        claimed_by TEXT, claimed_at REAL, lease_expires_at REAL, resolution TEXT, operator_reply TEXT, resolved_at REAL, created_at REAL NOT NULL, idempotency_key TEXT);""")
    conn.commit()
    conn.close()
    s = HandoffStore(path)
    s.notify(record("HND-AAAAAA"))
    assert s.escalate_overdue(SLA, now=time.time() + SLA + 5)[0]["id"] == "HND-AAAAAA"
    s.close()


def test_the_queue_listing_shows_the_escalation(store):
    store.notify(record("HND-AAAAAA"))
    store.escalate_overdue(SLA, now=time.time() + SLA + 5)
    summary = HandoffSummary(**_summary_fields(store.get("HND-AAAAAA")))
    assert summary.priority == "high" and summary.escalated_at is not None


# ---------------- the sweep that runs it ----------------

class FakeHook:
    def __init__(self):
        self.events = []

    def send(self, event):
        self.events.append(event)


class FakeApprovals:
    def __init__(self, boom=False):
        self.calls, self.boom = 0, boom

    def sweep(self):
        self.calls += 1
        if self.boom:
            raise RuntimeError("approvals down")


def test_the_sweep_escalates_announces_by_webhook_and_a_failing_part_does_not_skip_the_other(store, monkeypatch):
    store.notify(record("HND-AAAAAA"))
    monkeypatch.setattr(store, "_now", lambda now=None: time.time() + SLA + 120 if now is None else now)
    hook, approvals = FakeHook(), FakeApprovals(boom=True)
    out = sweep_once(approvals, store, SLA, hook)
    assert approvals.calls == 1 and [o["id"] for o in out] == ["HND-AAAAAA"]
    assert hook.events == [{"event": "handoff.sla_breached", "handoff_id": "HND-AAAAAA", "reason": "agent_unresolved", "priority": "high",
                            "conversation_id": "c1", "user_id": "cliente1988", "waited_seconds": hook.events[0]["waited_seconds"]}]
    assert hook.events[0]["waited_seconds"] >= SLA
    assert "text" not in hook.events[0] and "user_message" not in hook.events[0]  # no customer text in the announcement


def test_the_sweep_with_no_sla_and_no_webhook_still_runs_the_approvals(store):
    approvals = FakeApprovals()
    assert sweep_once(approvals, store, 0.0, None) == [] and approvals.calls == 1
    assert sweep_once(None, None, SLA, None) == []
