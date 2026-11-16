"""Customer data (mock). Swap this class for a real CRM and nothing else changes."""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import unicodedata
import uuid
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

DATA_FILE = Path(__file__).with_name("customers.json")


class CustomerRepository:
    def __init__(self, data_file: Path | None = None, tickets_file: str | Path | None = None,
                 today: date | None = None, dedup_window_s: float = 0, clock=time.time):
        raw = json.loads(Path(data_file or DATA_FILE).read_text(encoding="utf-8"))
        self._data = {k: v for k, v in raw.items() if not k.startswith("_")}
        self._today = today or date.today()
        self._tickets_file = Path(tickets_file) if tickets_file else None
        self._lock = threading.Lock()
        self._dedup_window_s = dedup_window_s  # same category within this many seconds = the same issue (0 = off)
        self._clock = clock
        self._created: list[dict] | None = None  # loaded from the tickets file on the first write

    # helpers
    def _iso(self, offset_days: int) -> str:
        return (self._today + timedelta(days=offset_days)).isoformat()

    def exists(self, user_id: str) -> bool:
        return user_id in self._data

    # reads
    def get_profile(self, user_id: str) -> dict | None:
        c = self._data.get(user_id)
        return deepcopy(c["profile"]) if c else None

    def get_terminal(self, user_id: str) -> dict | None:
        c = self._data.get(user_id)
        return deepcopy(c["terminal"]) if c else None

    def get_transactions(self, user_id: str, limit: int = 10, status: str | None = None) -> list[dict] | None:
        c = self._data.get(user_id)
        if not c:
            return None
        out = []
        for t in c["transactions"]:
            if status and t["status"] != status:
                continue
            t = deepcopy(t)
            t["date"] = self._iso(-t.pop("days_ago"))
            out.append(t)
        return out[:limit]

    def get_payouts(self, user_id: str) -> list[dict] | None:
        c = self._data.get(user_id)
        if not c:
            return None
        out = []
        for p in c["payouts"]:
            p = deepcopy(p)
            p["sales_date"] = self._iso(p.pop("sales_day_offset"))
            days = p.pop("expected_in_days")
            p["expected_date"] = self._iso(days)
            p["days_until_deposit"] = days  # so the model doesn't do date math
            out.append(p)
        return out

    def get_tickets(self, user_id: str) -> list[dict] | None:
        c = self._data.get(user_id)
        if not c:
            return None
        out = []
        for t in c["tickets"]:
            t = deepcopy(t)
            t["opened_on"] = self._iso(-t.pop("days_ago"))
            out.append(t)
        return out

    # writes
    def _load_created(self) -> list[dict]:
        """The tickets this system opened, read back from the tickets file: a restart (or a resumed graph) must not
        forget that a ticket was already opened. A line that does not parse is skipped, never fatal."""
        out: list[dict] = []
        if self._tickets_file and self._tickets_file.exists():
            for line in self._tickets_file.read_text(encoding="utf-8").splitlines():
                try:
                    rec = json.loads(line)
                    out.append({**rec, "_at": datetime.fromisoformat(rec["created_at"]).timestamp()})
                except (ValueError, KeyError, TypeError):
                    continue
        return out

    def _find_open_duplicate(self, user_id: str, category: str, key: str, now: float) -> dict | None:
        """The same issue again: an open ticket of this customer with the same key, or (window > 0) of the same category
        opened in the last `dedup_window_s` seconds (the model words the same problem differently on a retry)."""
        for rec in reversed(self._created):
            if rec.get("user_id") != user_id or rec.get("status") != "open":
                continue
            same_key = ticket_key(user_id, rec.get("category", ""), rec.get("summary", "")) == key
            # a handoff ticket is a different case each time (it has its own reference), so the window skips it
            recent = (self._dedup_window_s > 0 and category != "human_handoff" and rec.get("category") == category
                      and now - rec["_at"] <= self._dedup_window_s)
            if same_key or recent:
                return rec
        return None

    def create_ticket(self, user_id: str, category: str, summary: str) -> dict | None:
        """Opens a ticket AT MOST ONCE per issue: asking again for the same issue returns the ticket that is already open
        (`already_existed: true`) instead of a second one. The key is deterministic (customer, category, normalized
        summary), so a replayed node, a retried agent or a restart cannot duplicate it."""
        c = self._data.get(user_id)
        if not c:
            return None
        key = ticket_key(user_id, category, summary)
        with self._lock:  # the check and the write are one step: two simultaneous calls cannot both create
            if self._created is None:
                self._created = self._load_created()
            now = self._clock()
            found = self._find_open_duplicate(user_id, category, key, now)
            if found:
                return {"ticket_id": found["ticket_id"], "category": found["category"], "summary": found["summary"],
                        "status": found["status"], "days_ago": 0, "opened_on": self._iso(0), "already_existed": True}
            ticket = {
                "ticket_id": f"TKT-{uuid.uuid4().hex[:6].upper()}",
                "category": category,
                "summary": summary,
                "status": "open",
                "days_ago": 0,
            }
            c["tickets"].append(ticket)
            record = {**ticket, "user_id": user_id, "idem_key": key,
                      "created_at": datetime.fromtimestamp(now, timezone.utc).isoformat()}
            self._created.append({**record, "_at": now})
            if self._tickets_file:
                self._tickets_file.parent.mkdir(parents=True, exist_ok=True)
                with self._tickets_file.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return {**ticket, "opened_on": self._iso(0)}


def normalize_summary(text: str) -> str:
    """Case, accents, punctuation and spacing do not make a different issue."""
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", plain)).strip()


def ticket_key(user_id: str, category: str, summary: str) -> str:
    return hashlib.sha256(f"{user_id}\x1f{category}\x1f{normalize_summary(summary)}".encode("utf-8")).hexdigest()[:16]
