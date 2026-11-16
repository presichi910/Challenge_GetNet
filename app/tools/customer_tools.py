"""Tools for the Support agent.

The tools are already tied to one user_id and take no user argument, so the LLM can't pick
another customer (even with a prompt injection).
"""
from __future__ import annotations

import json
import re
from typing import Literal

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, ConfigDict

from app.data.repository import CustomerRepository

NOT_FOUND = json.dumps({"error": "customer_not_found", "message": "No account data was found for this user."})
SUMMARY_MIN, SUMMARY_MAX = 10, 500
INVALID_SUMMARY = json.dumps({"error": "invalid_summary", "message": f"`summary` must be {SUMMARY_MIN}-{SUMMARY_MAX} characters "
                                                                    "describing the problem. No ticket was opened."})


class TicketArgs(BaseModel):
    """Strict: an argument the tool does not declare (a hostile `user_id`, `status`, `priority`...) is an error, not ignored."""
    model_config = ConfigDict(extra="forbid")
    category: Literal["terminal", "payout", "billing", "account", "other"]
    summary: str


def clean_summary(text: str) -> str | None:
    """Control characters (newlines included) become spaces, so a ticket is always one line of one record; the length is
    bounded. None when what is left is too short to describe anything."""
    one_line = re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", text)).strip()
    if len(one_line) < SUMMARY_MIN:
        return None
    return one_line[:SUMMARY_MAX]


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def make_support_tools(user_id: str, repo: CustomerRepository) -> list[BaseTool]:
    @tool
    def get_customer_profile() -> str:
        """Get the customer's profile: name, business, plan, account status, linked bank account,
        whether Pix and receivables advance are enabled."""
        profile = repo.get_profile(user_id)
        return _dump(profile) if profile else NOT_FOUND

    @tool
    def get_recent_transactions(limit: int = 10, status: Literal["approved", "declined", "all"] = "all") -> str:
        """Get the customer's most recent sales/transactions (newest first). Declined transactions
        include a decline code and reason. Use status='declined' to focus on declines."""
        txs = repo.get_transactions(user_id, limit=limit, status=None if status == "all" else status)
        return _dump(txs) if txs is not None else NOT_FOUND

    @tool
    def get_payout_schedule() -> str:
        """Get the customer's payouts (deposits/settlements): which sales each payout covers, net
        amount after fees, expected deposit date and status (scheduled / processing / paid).
        Use this to answer 'when will I receive the money for my sales?'."""
        payouts = repo.get_payouts(user_id)
        return _dump(payouts) if payouts is not None else NOT_FOUND

    @tool
    def get_terminal_status() -> str:
        """Get the customer's card machine (terminal) status: online/offline, connection type,
        signal strength, minutes since last seen, last error, firmware and battery."""
        terminal = repo.get_terminal(user_id)
        return _dump(terminal) if terminal else NOT_FOUND

    @tool
    def get_support_tickets() -> str:
        """List the customer's existing support tickets and their status."""
        tickets = repo.get_tickets(user_id)
        return _dump(tickets) if tickets is not None else NOT_FOUND

    @tool(args_schema=TicketArgs)
    def create_support_ticket(category: Literal["terminal", "payout", "billing", "account", "other"], summary: str) -> str:
        """Open a support ticket for a human agent/technician. Only use when the issue cannot be
        solved with the available data or the customer asks for it. `summary` must be a concise,
        self-contained description of the problem (include codes/errors found). Asking again for the
        same issue returns the ticket that is already open (`already_existed`), it never opens a second."""
        clean = clean_summary(summary)
        if clean is None:
            return INVALID_SUMMARY
        ticket = repo.create_ticket(user_id, category, clean)
        return _dump(ticket) if ticket else NOT_FOUND

    return [
        get_customer_profile,
        get_recent_transactions,
        get_payout_schedule,
        get_terminal_status,
        get_support_tickets,
        create_support_ticket,
    ]
