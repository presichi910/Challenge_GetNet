"""The eval's service on a REAL SQLite checkpointer, with a RESTART in the middle of a multi-turn case.

Until now a multi-turn case handed the earlier turns to the graph as context in the same call: the checkpointer (the memory the API
really uses) and a process restart were never exercised by the eval. Here the earlier turns are WRITTEN INTO THE THREAD (no model
calls, so every run starts from exactly the same conversation, as before), the checkpointer and the graph are CLOSED AND OPENED
AGAIN (a restart), and only then the case's last message is answered. Two deterministic checks come with it (`memory_ok`):
the reopened thread holds exactly the turns that were written, and after the answer it holds them plus the new exchange.

The file lives in a temporary folder, never in ./data: the eval writes no conversation to disk that outlives it.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from langgraph.graph import END  # noqa: F401  (documents that `handoff` below is a node that ends the graph)

from app.memory import open_memory
from app.service import ChatService


class PersistentEvalService:
    def __init__(self, keep_verify_raw: bool = True, build=None):
        self.keep_verify_raw = keep_verify_raw
        self.build = build  # (checkpointer) -> graph; None = the real default graph
        self.dir = Path(tempfile.mkdtemp(prefix="getnet-eval-"))
        self.restarts = 0
        self.memory = None
        self._open()

    def _open(self) -> None:
        from app.graph import build_default_graph

        self.memory = open_memory(self.dir / "checkpoints.sqlite", ttl_days=0, max_threads_per_user=0)
        graph = self.build(self.memory.saver) if self.build else build_default_graph(checkpointer=self.memory.saver,
                                                                                     keep_verify_raw=self.keep_verify_raw)
        self.service = ChatService(graph)

    # what run_case / evaluate use
    def run_raw(self, *args, **kwargs):
        return self.service.run_raw(*args, **kwargs)

    def seed(self, thread_id: str, user_id: str, messages: list, conversation_id: str = "default") -> None:
        """Write earlier turns into the thread. `as_node="handoff"` marks the write as coming from a node that ends the graph, so
        nothing is left pending and the next call starts a normal turn from the beginning."""
        self.service.graph.update_state({"configurable": {"thread_id": thread_id}},
                                        {"messages": messages, "user_id": user_id, "conversation_id": conversation_id}, as_node="handoff")

    def restart(self) -> None:
        """Close the checkpointer file and the graph and open them again: what a process restart does to the conversation."""
        self.memory.close()
        self.restarts += 1
        self._open()

    def thread_messages(self, thread_id: str) -> list:
        return list(self.service.graph.get_state({"configurable": {"thread_id": thread_id}}).values.get("messages", []))

    def close(self) -> None:
        try:
            self.memory.close()
        finally:
            shutil.rmtree(self.dir, ignore_errors=True)
