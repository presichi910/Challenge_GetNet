"""Tell the operators when a case needs them: a signed webhook (Slack, Teams, a pager: whatever takes an HTTPS POST).

The operators work from a queue they have to open (`GET /operator/handoffs`). This pushes: a new handoff, a ticket request waiting for approval and a
handoff that waited past its SLA each become one POST to `WEBHOOK_URL`.

Three rules decide the shape of it:
  * It NEVER slows a customer's turn. `notify` only puts the event in a bounded queue; a background thread does the POST, with retries and backoff. A slow
    or dead endpoint costs the customer nothing; if the queue is full the OLDEST event is dropped (and counted), never the turn.
  * It carries NO customer text: ids, reason, priority, category, age. Not the message, not the conversation, not what the agents found (the same
    redaction as the audit file). The operator opens the case in the queue to read it.
  * It is SIGNED: `X-Getnet-Signature: sha256=<hmac of "<timestamp>.<body>">` with `X-Getnet-Timestamp`, so the receiver can refuse a forged or an old
    request (replay). Without `WEBHOOK_SIGNING_KEY` nothing is sent: an unsigned push is not offered.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import queue
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

log = logging.getLogger("webhook")

SIGNATURE_HEADER = "X-Getnet-Signature"
TIMESTAMP_HEADER = "X-Getnet-Timestamp"
HANDOFF_FIELDS = ("handoff_id", "created_at", "reason", "priority", "ticket_id", "language", "conversation_id", "user_id")


def sign(secret: str, timestamp: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), timestamp.encode("ascii") + b"." + body, hashlib.sha256).hexdigest()


def verify(secret: str, timestamp: str, body: bytes, signature: str, now: float | None = None, tolerance: float = 300.0) -> bool:
    """What a receiver does: the signature matches AND the timestamp is recent."""
    try:
        fresh = abs((time.time() if now is None else now) - float(timestamp)) <= tolerance
    except ValueError:
        return False
    return fresh and hmac.compare_digest(sign(secret, timestamp, body), signature)


def handoff_event(record: dict, event: str = "handoff.created") -> dict:
    """The redacted view of a handoff record: only the fields the operators' tooling needs to route it."""
    out = {k: record.get(k) for k in HANDOFF_FIELDS}
    out["event"] = event
    return out


def approval_event(row: dict) -> dict:
    return {"event": "approval.requested", "approval_id": row["id"], "category": row["category"], "conversation_id": row["conversation_id"],
            "user_id": row["user_id"], "expires_at": row["expires_at"], "created_at": row["created_at"]}


class WebhookNotifier:
    """A `HandoffNotifier` (it has `notify(record)`) that also takes other events through `send`."""

    def __init__(self, url: str, secret: str, *, timeout: float = 3.0, retries: int = 3, backoff: float = 1.0, max_queue: int = 200,
                 poster=None, sleep=time.sleep, clock=time.time):
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("WEBHOOK_URL must be an http(s) URL")
        if not secret:
            raise ValueError("WEBHOOK_SIGNING_KEY is required: an unsigned webhook is not offered")
        self.url, self.secret, self.timeout, self.retries, self.backoff = url, secret, timeout, retries, backoff
        self.host = parsed.netloc  # what is logged: never the path (it can hold a token, as Slack's does)
        self._post = poster or self._http_post
        self._sleep, self._clock = sleep, clock
        self._q: queue.Queue = queue.Queue(maxsize=max_queue)
        self.sent = self.failed = self.dropped = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="webhook", daemon=True)
        self._thread.start()

    # ---- what the rest of the app calls: never blocks, never raises ----
    def notify(self, record: dict) -> None:
        self.send(handoff_event(record))

    def send(self, event: dict) -> None:
        item = {**event, "sent_at": self._clock()}
        while True:
            try:
                self._q.put_nowait(item)
                return
            except queue.Full:
                try:
                    self._q.get_nowait()  # make room by dropping the OLDEST: the newest event is the one still worth sending
                    self._q.task_done()  # a dropped event is a finished one, or `flush` would wait for it forever
                    self.dropped += 1
                    log.warning("webhook queue full: dropped the oldest event (%d dropped so far)", self.dropped)
                except queue.Empty:
                    pass

    # ---- the background thread ----
    def _http_post(self, body: bytes, headers: dict) -> int:
        req = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:  # noqa: S310 - the scheme is checked in __init__  # nosec B310
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def _deliver(self, event: dict) -> bool:
        body = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        for attempt in range(1, self.retries + 1):
            ts = str(int(self._clock()))
            headers = {"Content-Type": "application/json", TIMESTAMP_HEADER: ts, SIGNATURE_HEADER: sign(self.secret, ts, body), "User-Agent": "getnet-agents-webhook"}
            try:
                status = self._post(body, headers)
                if 200 <= status < 300:
                    return True
                if 400 <= status < 500 and status != 429:
                    log.warning("webhook to %s refused the event with %s: not retried", self.host, status)
                    return False  # a rejected request will not become acceptable by waiting
            except Exception as e:  # noqa: BLE001 - a dead endpoint is a fact to log, not a crash
                log.warning("webhook to %s failed (attempt %d/%d): %s", self.host, attempt, self.retries, type(e).__name__)
            if attempt < self.retries:
                self._sleep(self.backoff * 2 ** (attempt - 1))
        return False

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                event = self._q.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                if self._deliver(event):
                    self.sent += 1
                else:
                    self.failed += 1
            finally:
                self._q.task_done()

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until everything queued has been tried (tests, shutdown). True if the queue emptied in time."""
        end = time.time() + timeout
        while time.time() < end:
            if self._q.unfinished_tasks == 0:
                return True
            time.sleep(0.01)
        return False

    def close(self) -> None:
        self.flush(2.0)
        self._stop.set()
        self._thread.join(timeout=2.0)
