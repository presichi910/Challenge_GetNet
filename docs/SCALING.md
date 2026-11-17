# Scaling beyond one worker

> **This page is a DESIGN. NONE OF IT IS IMPLEMENTED.** It says what would have to change, in what order, and what is not known. It promises no date, no result and no performance figure, and nothing here has been tried on Postgres, Redis or a broker.

## One worker, on purpose

The API runs a single uvicorn worker. The `Dockerfile` starts it with `--workers 1` (line 77), `docker-compose.yml` sets `WEB_CONCURRENCY=1`, and `app/main.py` (`validate_single_worker`, line 114) refuses to start if `WEB_CONCURRENCY` asks for more; `tests/test_single_worker.py` pins all three. The reason is that some state lives in the process or in one SQLite file, and a second worker would silently break it:

| State | Where it lives today | What goes wrong with N workers |
|---|---|---|
| Rate limit per caller | an in-process sliding window, `app/ratelimit.py` | each worker counts alone, so the real limit is N times higher |
| Revoked token ids (`jti`) | a SQLite file, `app/revocation.py` | the file survives a restart and could be shared on one host, but it is read and pruned by one process, and a shared volume across hosts is not a design |
| JWKS key cache | in the process, `app/jwks.py` | each worker fetches the keys on its own (a read-only cache, so harmless but N times the fetches) |
| Conversation memory | one SQLite checkpointer file, `app/memory.py` | several writers would contend for one file |
| Handoff queue, ticket approvals, traces | their own SQLite files, `app/handoff_store.py`, `app/approvals.py`, `app/observability.py` | the single-statement transitions are exactly-once inside SQLite, but not across hosts |
| Operator webhook | a bounded in-process queue and a thread, `app/webhook.py` | events queued in one worker are lost if it dies, and every worker has its own queue |
| The 30-second sweep (SLA, approval expiry) | a loop in each process, `app/main.py` | every worker would run it |
| Prometheus metrics | in the process, `app/telemetry.py` | each worker exposes only its own counters |

Until the rest of this page is done the way to grow is **up, not out**.

## The path (design, in this order)

1. **Conversation checkpointer: SQLite -> Postgres.** LangGraph has a Postgres checkpointer. It would replace `SqliteSaver` in `app/memory.py`, and the `threads` table (owner and last activity per thread) and the retention purge would move with it. The package is **a dependency this project has not approved**, so adding it is a decision to take first. Not known: the behavior of the retention purge and of `prune_thread` on Postgres, and the cost of the checkpoint size per turn, which is today kept small by keeping only the latest checkpoint.
2. **The queues' SQL.** The handoff and approval transitions are single `UPDATE ... WHERE status = ...` statements whose changed rows are counted. That is standard SQL, so the same statements are the natural candidate to move to Postgres with the same exactly-once reasoning. **That has not been tested**: the tests today race separate SQLite connections.
3. **Webhook: in-process queue -> an outbox and a broker.** Write the event in the same transaction as the handoff (a transactional outbox table), and let a delivery worker read the outbox and post (or publish to a broker). That removes the loss on a crash and the per-worker queue. It would need an acknowledgement and a retry policy that today do not exist (the README says the receiver's acknowledgement is not tracked).
4. **Rate limit and `jti` denylist -> Redis.** `app/ratelimit.py` already says where the seam is: the class has one method, `hit`, and a Redis-backed class with the same method replaces it. The denylist (`app/revocation.py`) would become a Redis set with a time-to-live equal to the token's maximum life. A new dependency, again to be approved.
5. **The sweep.** Either one elected process runs it, or an external scheduler calls it. The SLA escalation is one guarded statement and the approval reconcile is idempotent, so a duplicate run should be harmless, but running it N times is wasteful and the claim was not tested with several processes.
6. **Metrics.** Prometheus multiprocess mode, or scraping each worker as its own target.
7. **The knowledge base.** Chroma is used as an embedded library over a local directory (`app/rag/store.py`). Whether several processes can read the same directory safely was **not checked**; the alternatives (a read-only copy per worker, or a vector store that is a service) are not evaluated.

## What is not claimed

No throughput figure, no latency under load and no concurrency test exists for this project: the evaluation runs one request at a time. Nothing on this page says how many workers or users the system would hold after these changes.
