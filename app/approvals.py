"""Human approval before a ticket is opened: the write action waits for a person (LangGraph `interrupt()`).

    customer turn   support agent -> create_support_ticket(...) does NOT write: it returns `pending_approval`
                    ... synthesizer -> verify -> request_approval  (records the request, tells the customer it is pending)
    operator        GET /operator/approvals, POST .../approve | .../reject
    approval graph  ONE thread per approval (`approval:<id>`): node `approve` runs `interrupt()`, and `Command(resume=...)`
                    continues it: approved opens the ticket (idempotent, see T1), rejected and expired do not.

Why a graph of its own and not an `interrupt()` in the conversation's graph: `interrupt()` re-runs its node from the start when
it resumes, and a customer who writes again while waiting starts a new run of the same thread, which orphans the pending
interrupt (checked: the second turn finishes the thread without it). A thread per approval cannot be touched by the chat. The
node keeps nothing before `interrupt()` that must not repeat (it reads the request, nothing else), so the re-run is harmless; the
checkpoints hold only the approval id, never what the customer wrote.

The approvals have their own SQLite file (table and checkpoints): the conversation memory's retention purge deletes any
checkpoint whose thread is not in its registry, and it would delete a pending approval.

Time limit: a request not decided within `APPROVAL_TTL_MINUTES` EXPIRES: no ticket is opened (silence is never an approval), the
customer is told, and the case goes to the human queue as `approval_expired`.
"""
from __future__ import annotations

import builtins
import hashlib
import json
import logging
import sqlite3
import threading
import time
import uuid
from typing import Any, Callable, Mapping, TypedDict

from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from app.data.repository import CustomerRepository, ticket_key

log = logging.getLogger("approvals")

PENDING_APPROVAL = "pending_approval"
STATUSES = ("pending", "approved", "rejected", "expired")
DECISIONS = ("approved", "rejected")
TOOL_CONTEXT_PREFIX = "[create_support_ticket] "

# what the customer reads. {refs}: the approval ids; {ticket}, {handoff}: references
REQUESTED = {
    "English": "I've sent your ticket request to our team for approval (reference {refs}). It is not open yet: I'll tell you here as soon as it is decided.",
    "Portuguese": "Enviei o seu pedido de chamado para aprovação da nossa equipe (referência {refs}). Ele ainda não foi aberto: aviso aqui assim que for decidido.",
    "Spanish": "He enviado tu solicitud de ticket a nuestro equipo para su aprobación (referencia {refs}). Todavía no está abierto: te avisaré aquí en cuanto se decida.",
}
OUTCOMES = {
    "approved": {
        "English": "Your ticket {ticket} is now open (request {ref}). A technician will follow up.",
        "Portuguese": "O seu chamado {ticket} está aberto (pedido {ref}). Um técnico dará seguimento.",
        "Spanish": "Tu ticket {ticket} ya está abierto (solicitud {ref}). Un técnico le dará seguimiento.",
    },
    "approved_existing": {
        "English": "Your request {ref} was approved, and you already had ticket {ticket} open for this issue, so no second one was created.",
        "Portuguese": "O pedido {ref} foi aprovado, e você já tinha o chamado {ticket} aberto para este assunto, então não foi criado um segundo.",
        "Spanish": "Tu solicitud {ref} fue aprobada, y ya tenías el ticket {ticket} abierto para este tema, así que no se creó un segundo.",
    },
    "rejected": {
        "English": "Our team did not approve opening a ticket for this (request {ref}), so none was opened. Tell me if you'd like to try something else or speak to a person.",
        "Portuguese": "Nossa equipe não aprovou a abertura de um chamado para isto (pedido {ref}), então nenhum foi aberto. Diga se quer tentar outra coisa ou falar com uma pessoa.",
        "Spanish": "Nuestro equipo no aprobó abrir un ticket para esto (solicitud {ref}), así que no se abrió ninguno. Dime si quieres intentar otra cosa o hablar con una persona.",
    },
    "expired": {
        "English": "Your ticket request {ref} was not reviewed in time, so no ticket was opened. I've passed your case to a human agent (reference {handoff}).",
        "Portuguese": "O seu pedido de chamado {ref} não foi analisado a tempo, então nenhum chamado foi aberto. Encaminhei o seu caso a um atendente humano (referência {handoff}).",
        "Spanish": "Tu solicitud de ticket {ref} no se revisó a tiempo, así que no se abrió ningún ticket. He pasado tu caso a un agente humano (referencia {handoff}).",
    },
    "expired_plain": {
        "English": "Your ticket request {ref} was not reviewed in time, so no ticket was opened. Ask me if you'd like to try again or speak to a person.",
        "Portuguese": "O seu pedido de chamado {ref} não foi analisado a tempo, então nenhum chamado foi aberto. Peça se quiser tentar de novo ou falar com uma pessoa.",
        "Spanish": "Tu solicitud de ticket {ref} no se revisó a tiempo, así que no se abrió ningún ticket. Pídemelo si quieres intentarlo de nuevo o hablar con una persona.",
    },
}


def lang_key(language: str, table: dict) -> str:
    return next((k for k in table if (language or "").lower().startswith(k.lower()[:4])), "English")


class ApprovalNotFound(Exception):
    """No request has that id."""


class ApprovalConflict(Exception):
    """The request is not pending any more (decided, expired). Maps to 409."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS approvals (
    id              TEXT PRIMARY KEY,
    key             TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    category        TEXT NOT NULL,
    summary         TEXT NOT NULL,
    language        TEXT NOT NULL DEFAULT 'English',
    status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'expired')),
    created_at      REAL NOT NULL,
    expires_at      REAL NOT NULL,
    decided_by      TEXT,
    decided_at      REAL,
    reason          TEXT,
    ticket_id       TEXT,
    outcome         TEXT,
    resumed_at      REAL,
    delivered_at    REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS approvals_one_pending ON approvals (key) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS approvals_conversation ON approvals (user_id, conversation_id);
"""


class ApprovalStore:
    """The requests, in their own SQLite file, and the checkpointer of the approval graph (a second connection to the same file:
    the two never share a transaction)."""

    def __init__(self, path, *, clock: Callable[[], float] = time.time):
        self._clock = clock
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA secure_delete=ON")  # a deleted request's text is overwritten, not just unlinked
        self._lock = threading.Lock()
        with self._lock:
            self.conn.executescript(SCHEMA)
        self._saver_conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        self.saver = SqliteSaver(self._saver_conn)
        self.saver.setup()
        self._closed = False

    # ---- writes ----------------------------------------------------------------------------------------------
    def create(self, user_id: str, conversation_id: str, category: str, summary: str, language: str,
               ttl_s: float) -> tuple[dict, bool]:
        """(request, created). The same issue asked again while it is pending returns the pending request (the key is the one of
        the ticket, T1), so a retried turn does not stack requests."""
        key = ticket_key(user_id, category, summary)
        now = self._clock()
        with self._lock:
            row = self.conn.execute("SELECT * FROM approvals WHERE key = ? AND status = 'pending'", (key,)).fetchone()
            if row:
                return dict(row), False
            approval_id = f"APR-{uuid.uuid4().hex[:6].upper()}"
            self.conn.execute(
                "INSERT INTO approvals (id, key, user_id, conversation_id, category, summary, language, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (approval_id, key, user_id, conversation_id, category, summary, language, now, now + ttl_s))
            return dict(self.conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()), True

    def decide(self, approval_id: str, operator: str, decision: str, reason: str = "") -> dict:
        """pending -> approved | rejected, in ONE statement: two operators deciding at once, or a decision after the deadline, cannot
        both win. ApprovalConflict if it is not pending (or its time ran out), ApprovalNotFound if there is no such request."""
        if decision not in DECISIONS:
            raise ValueError(f"decision must be one of {DECISIONS}")
        now = self._clock()
        with self._lock:
            cur = self.conn.execute(
                "UPDATE approvals SET status = ?, decided_by = ?, decided_at = ?, reason = ? "
                "WHERE id = ? AND status = 'pending' AND expires_at > ?", (decision, operator, now, reason, approval_id, now))
            row = self.conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        if row is None:
            raise ApprovalNotFound(approval_id)
        if cur.rowcount != 1:
            state = "expired" if row["status"] == "pending" else row["status"]
            raise ApprovalConflict(f"This request is already {state}.")
        return dict(row)

    def expire_due(self) -> list[dict]:
        """Every pending request whose time ran out becomes `expired` (decided_by 'system'). Returns the ones changed."""
        now = self._clock()
        out = []
        with self._lock:
            due = self.conn.execute("SELECT id FROM approvals WHERE status = 'pending' AND expires_at <= ?", (now,)).fetchall()
            for r in due:
                cur = self.conn.execute("UPDATE approvals SET status = 'expired', decided_by = 'system', decided_at = ? "
                                        "WHERE id = ? AND status = 'pending' AND expires_at <= ?", (now, r["id"], now))
                if cur.rowcount == 1:
                    out.append(dict(self.conn.execute("SELECT * FROM approvals WHERE id = ?", (r["id"],)).fetchone()))
        return out

    def mark_resumed(self, approval_id: str, ticket_id: str | None, outcome: str) -> None:
        with self._lock:
            self.conn.execute("UPDATE approvals SET ticket_id = ?, outcome = ?, resumed_at = ? WHERE id = ? AND resumed_at IS NULL",
                              (ticket_id, outcome, self._clock(), approval_id))

    def pop_undelivered(self, user_id: str, conversation_id: str) -> list[dict]:
        """The outcomes of this conversation the customer has not been told yet, marked delivered in the same step."""
        now = self._clock()
        with self._lock:
            rows = [dict(r) for r in self.conn.execute(
                "SELECT * FROM approvals WHERE user_id = ? AND conversation_id = ? AND resumed_at IS NOT NULL "
                "AND delivered_at IS NULL ORDER BY decided_at", (user_id, conversation_id))]
            for r in rows:
                self.conn.execute("UPDATE approvals SET delivered_at = ? WHERE id = ?", (now, r["id"]))
        return rows

    def restore_undelivered(self, ids: list[str]) -> None:
        with self._lock:
            for i in ids:
                self.conn.execute("UPDATE approvals SET delivered_at = NULL WHERE id = ?", (i,))

    def delete_conversation(self, user_id: str, conversation_id: str) -> list[str]:
        """Erase the requests of a deleted conversation (their text is customer data). Returns their ids."""
        with self._lock:
            ids = [r["id"] for r in self.conn.execute("SELECT id FROM approvals WHERE user_id = ? AND conversation_id = ?",
                                                       (user_id, conversation_id))]
            self.conn.execute("DELETE FROM approvals WHERE user_id = ? AND conversation_id = ?", (user_id, conversation_id))
        return ids

    def purge_expired(self, ttl_days: float) -> int:
        """Retention: requests already decided, told to the customer, and older than `ttl_days` go. (0 = keep.)"""
        if not ttl_days:
            return 0
        cutoff = self._clock() - ttl_days * 86400
        with self._lock:
            cur = self.conn.execute("DELETE FROM approvals WHERE status != 'pending' AND delivered_at IS NOT NULL "
                                    "AND created_at < ?", (cutoff,))
        return cur.rowcount

    # ---- reads -----------------------------------------------------------------------------------------------
    def get(self, approval_id: str) -> dict | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        return dict(row) if row else None

    def list(self, status: str | None = None, limit: int = 100) -> list[dict]:
        sql, args = "SELECT * FROM approvals", ()  # type: tuple[str, tuple]
        if status:
            sql, args = sql + " WHERE status = ?", (status,)
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql + " ORDER BY created_at LIMIT ?", (*args, limit))]

    def unresumed(self) -> builtins.list[dict]:  # `list` is a method of this class, so the builtin is named explicitly
        """Decided (by an operator or by the clock) but the graph was not continued yet: a crash between the two."""
        with self._lock:
            return [dict(r) for r in self.conn.execute(
                "SELECT * FROM approvals WHERE status != 'pending' AND resumed_at IS NULL ORDER BY decided_at")]

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._lock:
            self.conn.close()
        self._saver_conn.close()


# ---- the conversation side ------------------------------------------------------------------------------------

def proposals_in(state: Mapping[str, Any]) -> builtins.list[dict]:
    """The ticket requests the support agent made THIS turn that are waiting for approval, read from the tool's own output."""
    seen, out = set(), []
    for c in ((state.get("agent_outputs") or {}).get("support") or {}).get("contexts", []):
        if not c.startswith(TOOL_CONTEXT_PREFIX):
            continue
        try:
            d = json.loads(c[len(TOOL_CONTEXT_PREFIX):])
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("status") == PENDING_APPROVAL and d.get("category") and d.get("summary"):
            k = (d["category"], d["summary"])
            if k not in seen:
                seen.add(k)
                out.append(d)
    return out


def make_request_approval_node(store: ApprovalStore, ttl_s: float, on_created=None):
    """Last node of a turn that asked for a ticket: records each request and tells the customer it is waiting for a person. The
    answer is final here (the synthesizer and the verifier already ran), so the notice is appended, not written by a model."""

    def request_approval(state: dict) -> dict:
        proposals = proposals_in(state)
        if not proposals:
            return {}
        language = state.get("language", "English")
        made = [store.create(state["user_id"], state.get("conversation_id") or "default", p["category"], p["summary"], language, ttl_s)
                for p in proposals]
        rows = [r for r, _ in made]
        for row, created in made:
            if created and on_created is not None:  # a NEW request: tell the operators (a retried turn does not announce it twice)
                try:
                    on_created(row)
                except Exception:  # noqa: BLE001 - the customer's turn never fails because of a notification
                    log.warning("approval notification failed for %s", row["id"], exc_info=True)
        notice = REQUESTED[lang_key(language, REQUESTED)].format(refs=", ".join(r["id"] for r in rows))
        answer = f"{(state.get('final_answer') or '').rstrip()}\n\n{notice}".strip()
        update: dict = {"final_answer": answer,
                        "approvals": [{"id": r["id"], "category": r["category"], "status": r["status"], "expires_at": r["expires_at"]}
                                      for r in rows]}
        messages = state.get("messages") or []
        if messages and getattr(messages[-1], "type", "") == "ai":
            update["messages"] = [AIMessage(content=answer, id=messages[-1].id)]  # same id: add_messages replaces it
        return update

    return request_approval


# ---- the approval graph ----------------------------------------------------------------------------------------

class ApprovalState(TypedDict, total=False):
    approval_id: str
    outcome: dict


def apply_decision(row: dict, decision: dict, repo: CustomerRepository, notifier=None) -> dict:
    """What happens once somebody (or the clock) decided. Runs after `interrupt()` returns. Everything here is idempotent:
    the ticket by its key (T1), the handoff by an id derived from the request's."""
    status = decision.get("decision")
    if status not in ("approved", "rejected", "expired"):
        raise ValueError(f"unknown decision {status!r}")
    language, ref = row.get("language") or "English", row["id"]
    lang = lang_key(language, REQUESTED)
    if status == "approved":
        ticket = repo.create_ticket(row["user_id"], row["category"], row["summary"])
        if ticket is None:
            raise RuntimeError(f"customer {row['user_id']} not found: the ticket of {ref} could not be opened")
        existed = bool(ticket.get("already_existed"))
        text = OUTCOMES["approved_existing" if existed else "approved"][lang].format(ticket=ticket["ticket_id"], ref=ref)
        return {"status": status, "ticket_id": ticket["ticket_id"], "already_existed": existed, "message": text}
    if status == "rejected":
        return {"status": status, "ticket_id": None, "message": OUTCOMES["rejected"][lang].format(ref=ref)}
    handoff_id = None
    if notifier is not None:
        handoff_id = "HND-" + hashlib.sha256(ref.encode()).hexdigest()[:6].upper()
        record = {"handoff_id": handoff_id, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
                  "user_id": row["user_id"], "conversation_id": row["conversation_id"], "reason": "approval_expired",
                  "priority": "high", "ticket_id": None, "language": language,
                  "user_message": f"[ticket request {ref}, category {row['category']}] {row['summary']}",
                  "conversation": [], "agent_findings": {"support": f"A ticket was requested ({row['category']}) and nobody "
                                                                    "approved or rejected it before the deadline."},
                  "tool_calls": []}
        try:
            notifier.notify(record)
        except Exception:  # noqa: BLE001 - a replay finds the id already queued; anything else is logged, the customer is still told
            log.warning("handoff for the expired request %s was not queued (already there, or the queue failed)", ref, exc_info=True)
    text = (OUTCOMES["expired"][lang].format(ref=ref, handoff=handoff_id) if handoff_id
            else OUTCOMES["expired_plain"][lang].format(ref=ref))
    return {"status": status, "ticket_id": None, "handoff_id": handoff_id, "message": text}


def build_approval_graph(store: ApprovalStore, repo: CustomerRepository, notifier=None, checkpointer=None):
    def approve(state: ApprovalState) -> dict:
        row = store.get(state["approval_id"])
        if row is None:  # the conversation was deleted while the request waited
            return {"outcome": {"status": "gone", "ticket_id": None, "message": ""}}
        # everything above this line runs again when the graph resumes: it only READS
        decision = interrupt({"approval_id": row["id"], "category": row["category"], "expires_at": row["expires_at"]})
        return {"outcome": apply_decision(row, decision, repo, notifier)}

    g = StateGraph(ApprovalState)
    g.add_node("approve", approve)
    g.add_edge(START, "approve")
    g.add_edge("approve", END)
    return g.compile(checkpointer=checkpointer or store.saver)


class ApprovalService:
    """Starts, decides, expires and resumes the approvals. One per process (the API runs one worker)."""

    def __init__(self, store: ApprovalStore, graph):
        self.store = store
        self.graph = graph

    @staticmethod
    def _config(approval_id: str) -> dict:
        return {"configurable": {"thread_id": f"approval:{approval_id}"}}

    def start(self, approval_id: str) -> None:
        """Run the approval graph until `interrupt()` (instant). Safe to call again: a thread that already waits is left alone."""
        cfg = self._config(approval_id)
        snap = self.graph.get_state(cfg)
        if snap.next or snap.values.get("outcome") is not None:
            return
        self.graph.invoke({"approval_id": approval_id}, cfg)

    def _finish(self, row: dict) -> None:
        """Continue the graph of a request that is decided and record what it did. If the graph already ran (a crash after it,
        before this bookkeeping) it is NOT run again: its outcome is read back."""
        cfg = self._config(row["id"])
        snap = self.graph.get_state(cfg)
        if not snap.next and snap.values.get("outcome") is None:
            self.graph.invoke({"approval_id": row["id"]}, cfg)  # it never started (the start failed): get it to the interrupt
            snap = self.graph.get_state(cfg)
        if snap.next:
            payload = {"decision": row["status"], "operator": row.get("decided_by") or "", "reason": row.get("reason") or ""}
            outcome = self.graph.invoke(Command(resume=payload), cfg)["outcome"]
        else:
            outcome = snap.values["outcome"]
        self.store.mark_resumed(row["id"], outcome.get("ticket_id"), outcome.get("message", ""))
        try:
            self.store.saver.delete_thread(f"approval:{row['id']}")  # done: nothing left to keep
        except Exception:  # noqa: BLE001
            log.warning("could not delete the checkpoints of %s", row["id"], exc_info=True)

    def decide(self, approval_id: str, operator: str, decision: str, reason: str = "") -> dict:
        self.sweep()
        row = self.store.decide(approval_id, operator, decision, reason)  # ApprovalConflict / ApprovalNotFound
        try:
            self._finish(row)
        except Exception:  # noqa: BLE001 - the decision stands and is recorded; `reconcile` finishes what failed
            log.exception("approval %s was %s but the graph could not be continued; it will be retried", approval_id, decision)
        log.info("approval decided id=%s operator=%s decision=%s", approval_id, operator, decision)
        return self.store.get(approval_id) or row

    def sweep(self) -> None:
        """Expire what ran out of time and finish every decided request whose graph was not continued."""
        for row in self.store.expire_due():
            log.warning("approval expired id=%s user=%s category=%s", row["id"], row["user_id"], row["category"])
        self.reconcile(start_pending=False)

    def reconcile(self, start_pending: bool = True) -> None:
        for row in self.store.unresumed():
            try:
                self._finish(row)
            except Exception:  # noqa: BLE001
                log.exception("could not finish approval %s", row["id"])
        if start_pending:
            for row in self.store.list("pending", 500):
                try:
                    self.start(row["id"])
                except Exception:  # noqa: BLE001
                    log.exception("could not start approval %s", row["id"])

    def delete_conversation(self, user_id: str, conversation_id: str) -> int:
        ids = self.store.delete_conversation(user_id, conversation_id)
        for i in ids:
            try:
                self.store.saver.delete_thread(f"approval:{i}")
            except Exception:  # noqa: BLE001
                log.warning("could not delete the checkpoints of %s", i, exc_info=True)
        return len(ids)
