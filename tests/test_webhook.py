"""Operator notification: a signed webhook that never slows the customer's turn and never carries what the customer wrote.
A real HTTP server on localhost for the delivery tests; fakes for timing."""
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.approvals import ApprovalStore, make_request_approval_node
from app.webhook import SIGNATURE_HEADER, TIMESTAMP_HEADER, WebhookNotifier, approval_event, handoff_event, sign, verify

KEY = "k" * 32
RECORD = {"handoff_id": "HND-ABC123", "created_at": "2026-11-15T20:00:00+00:00", "user_id": "cliente1988", "conversation_id": "c-1", "reason": "agent_unresolved",
          "priority": "normal", "ticket_id": "TKT-111111", "language": "English",
          "user_message": "my card number is 4111 1111 1111 1111", "conversation": [{"role": "human", "content": "secret talk"}],
          "agent_findings": {"support": "the customer owes R$ 900"}, "tool_calls": [{"agent": "support", "name": "get_payout_schedule", "args": {}}],
          "earlier_summary": "they asked about a loan", "case_summary": "Customer: my card number is 4111"}


class Receiver:
    """A local HTTP server that records what it gets and answers with a scripted status per request."""

    def __init__(self, statuses=(200,), delay=0.0):
        self.requests, self.statuses, self.delay = [], list(statuses), delay
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.requests.append({"body": body, "headers": dict(self.headers)})
                time.sleep(outer.delay)
                code = outer.statuses.pop(0) if len(outer.statuses) > 1 else outer.statuses[0]
                self.send_response(code)
                self.end_headers()

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}/hook/TOKEN-IN-THE-PATH"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def receiver():
    made = []

    def make(**kw):
        r = Receiver(**kw)
        made.append(r)
        return r

    yield make
    for r in made:
        r.close()


# ---------------- the signature ----------------

def test_a_signature_verifies_and_a_wrong_key_a_changed_body_or_an_old_timestamp_does_not():
    body, ts = b'{"event":"x"}', "1800000000"
    sig = sign(KEY, ts, body)
    now = 1_800_000_010.0
    assert verify(KEY, ts, body, sig, now=now)
    assert not verify("other" * 8, ts, body, sig, now=now) and not verify(KEY, ts, body + b" ", sig, now=now)
    assert not verify(KEY, ts, body, sig, now=now + 3600)  # a replay of an hour-old request
    assert not verify(KEY, "not-a-number", body, sig, now=now) and not verify(KEY, ts, body, "sha256=00", now=now)


def test_a_webhook_needs_an_http_url_and_a_signing_key():
    with pytest.raises(ValueError, match="http"):
        WebhookNotifier("file:///etc/passwd", KEY)
    with pytest.raises(ValueError, match="http"):
        WebhookNotifier("ftp://example.com/x", KEY)
    with pytest.raises(ValueError, match="SIGNING_KEY"):
        WebhookNotifier("https://example.com/hook", "")


# ---------------- what it says ----------------

def test_the_event_carries_ids_and_routing_data_and_none_of_what_the_customer_wrote():
    event = handoff_event(RECORD)
    assert event == {"handoff_id": "HND-ABC123", "created_at": "2026-11-15T20:00:00+00:00", "reason": "agent_unresolved", "priority": "normal",
                     "ticket_id": "TKT-111111", "language": "English", "conversation_id": "c-1", "user_id": "cliente1988", "event": "handoff.created"}
    dumped = json.dumps(event)
    for secret in ("4111", "secret talk", "R$ 900", "loan", "get_payout_schedule"):
        assert secret not in dumped


def test_the_approval_event_has_no_summary_text(tmp_path):
    store = ApprovalStore(tmp_path / "a.sqlite")
    summary = "Terminal offline, card 4111 1111 1111 1111"
    row, _ = store.create("cliente1988", "c-1", "terminal", summary, "English", 3600)
    event = approval_event(row)
    store.close()
    assert event["event"] == "approval.requested" and event["approval_id"] == row["id"] and event["category"] == "terminal"
    # What the customer contributed must not be in the event. The check does not look for the bare digits "4111": the event also holds
    # generated values (the request id and two timestamps in seconds) that can contain them by chance.
    dumped = json.dumps(event)
    for contributed in (summary, "Terminal offline", "4111 1111 1111 1111", "4111111111111111"):
        assert contributed not in dumped
    assert "summary" not in event
    assert set(event) == {"event", "approval_id", "category", "conversation_id", "user_id", "expires_at", "created_at"}


# ---------------- delivery ----------------

def test_a_handoff_is_posted_signed_and_the_receiver_can_verify_it(receiver):
    r = receiver()
    hook = WebhookNotifier(r.url, KEY, backoff=0.01)
    hook.notify(RECORD)
    assert hook.flush() and hook.sent == 1 and len(r.requests) == 1
    got = r.requests[0]
    assert verify(KEY, got["headers"][TIMESTAMP_HEADER], got["body"], got["headers"][SIGNATURE_HEADER])
    assert json.loads(got["body"])["handoff_id"] == "HND-ABC123" and got["headers"]["Content-Type"] == "application/json"
    hook.close()


def test_a_server_error_is_retried_with_backoff_until_it_works(receiver):
    r = receiver(statuses=(500, 503, 200))
    hook = WebhookNotifier(r.url, KEY, retries=3, backoff=0.01)
    hook.notify(RECORD)
    assert hook.flush() and len(r.requests) == 3 and hook.sent == 1 and hook.failed == 0
    hook.close()


def test_a_refusal_by_the_receiver_is_not_retried(receiver):
    r = receiver(statuses=(403,))
    hook = WebhookNotifier(r.url, KEY, retries=3, backoff=0.01)
    hook.notify(RECORD)
    assert hook.flush() and len(r.requests) == 1 and hook.failed == 1 and hook.sent == 0  # waiting will not make a 403 acceptable
    hook.close()


def test_when_it_never_works_it_gives_up_after_the_retries_and_counts_the_failure(receiver):
    r = receiver(statuses=(502,))
    hook = WebhookNotifier(r.url, KEY, retries=3, backoff=0.01)
    hook.notify(RECORD)
    assert hook.flush() and len(r.requests) == 3 and hook.failed == 1
    hook.close()


# ---------------- never in the customer's way ----------------

def test_a_dead_endpoint_costs_the_caller_nothing_and_never_raises():
    def refused(body, headers):
        raise ConnectionRefusedError("nothing listens")

    hook = WebhookNotifier("http://127.0.0.1:9/hook", KEY, retries=2, backoff=0.01, poster=refused)
    t0 = time.perf_counter()
    for _ in range(20):
        hook.notify(RECORD)
    assert time.perf_counter() - t0 < 0.5  # the 20 calls only queued
    assert hook.flush(10) and hook.failed == 20 and hook.sent == 0
    hook.close()


def test_a_really_unreachable_port_is_survived_too():
    hook = WebhookNotifier("http://127.0.0.1:9/hook", KEY, retries=1, timeout=0.5)  # nothing listens on the discard port (Windows takes ~2 s to say so)
    hook.notify(RECORD)
    assert hook.flush(20) and hook.failed == 1
    hook.close()


def test_a_slow_endpoint_does_not_slow_the_caller(receiver):
    r = receiver(delay=0.5)
    hook = WebhookNotifier(r.url, KEY)
    t0 = time.perf_counter()
    hook.notify(RECORD)
    assert time.perf_counter() - t0 < 0.1
    hook.flush(5)
    hook.close()


def test_when_the_queue_is_full_the_oldest_event_is_dropped_and_the_newest_is_kept():
    gate, seen = threading.Event(), []

    def blocked_poster(body, headers):
        gate.wait(5)
        seen.append(json.loads(body)["n"])
        return 200

    hook = WebhookNotifier("http://example.invalid/h", KEY, max_queue=2, poster=blocked_poster)
    for n in range(6):
        hook.send({"event": "x", "n": n})
        time.sleep(0.02)
    gate.set()
    assert hook.flush(5)
    hook.close()
    assert hook.dropped >= 3 and 5 in seen and 0 not in seen[1:]  # the newest got through; the oldest were the ones dropped


def test_the_log_names_the_host_and_never_the_path_that_can_hold_a_token(caplog):
    caplog.set_level(logging.WARNING, logger="webhook")
    hook = WebhookNotifier("http://127.0.0.1:9/services/T000/B000/XOXB-SECRET-TOKEN", KEY, retries=1, timeout=0.3)
    hook.notify(RECORD)
    hook.flush(5)
    hook.close()
    assert "127.0.0.1:9" in caplog.text and "SECRET-TOKEN" not in caplog.text and "XOXB" not in caplog.text


# ---------------- who calls it ----------------

def test_a_new_ticket_request_is_announced_once_and_a_failing_announcement_never_fails_the_turn(tmp_path):
    store = ApprovalStore(tmp_path / "a.sqlite")
    announced = []
    pending = {"status": "pending_approval", "category": "terminal", "summary": "Terminal does not connect since this morning"}
    state = {"user_id": "cliente1988", "conversation_id": "c-1", "language": "English", "final_answer": "ok",
             "agent_outputs": {"support": {"contexts": ["[create_support_ticket] " + json.dumps(pending)]}}, "messages": []}
    node = make_request_approval_node(store, 3600, on_created=announced.append)
    node(state)
    node(state)  # the same turn retried: the request already exists
    assert [a["category"] for a in announced] == ["terminal"]
    boom = make_request_approval_node(store, 3600, on_created=lambda row: (_ for _ in ()).throw(RuntimeError("webhook down")))
    other = {**state, "agent_outputs": {"support": {"contexts": ["[create_support_ticket] " + json.dumps({**pending, "category": "billing"})]}}}
    update = boom(other)
    assert update["approvals"] and "reference APR-" in update["final_answer"]  # the customer still got the reply
    store.close()
