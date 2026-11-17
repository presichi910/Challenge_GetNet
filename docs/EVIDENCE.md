# Where to verify each claim

A map from a claim to the code that does it and to the test or report that backs it. Line numbers are those of this checkout and move when a file is edited; the file and the test name are the stable part.
A test named here is in the file next to it. Reports are in `eval/reports/` and are versioned. **No judge rate is typed here**: for those, read the generated block in the [README](../README.md#results). The answer to each point of the external review is in [REVIEW_RESPONSE.md](REVIEW_RESPONSE.md).

To run the tests without keys or network: `pytest -m "not eval"`.

## Security

| Claim | Where in the code | Test | Other evidence |
|---|---|---|---|
| Tokens are RS256 only, checked against the identity provider's JWKS; no shared secret in the app | `app/auth.py:51` (the algorithm list, fixed in code), `app/auth.py:185` (header check), `app/auth.py:207` (decode) | `tests/test_auth_rs256.py:61` (`test_a_valid_rs256_token_is_accepted`) | `infra/keycloak/realm-merchants.json`, `infra/keycloak/realm-operators.json`; `tests/test_keycloak_live.py` runs the same checks against a real Keycloak when `RUN_KEYCLOAK_TESTS=1` (skipped otherwise) |
| `kid` is required; a token cannot bring its own key (`jku`, `jwk`, `x5u`, `x5c` are refused) | `app/auth.py:53` | `tests/test_auth_rs256.py:66` and `:75` | |
| Merchants and operators have separate issuers, audiences and keys; the role comes from the issuer | `app/auth.py:207` (issuer and audience per realm), `app/config.py:22-27` (the six settings) | `tests/test_auth_rs256.py:86` and `:96` | `tests/test_roles.py`, `tests/test_operator_api.py` |
| The key cache refreshes on an unknown `kid` at most once per interval and keeps serving if the provider is down | `app/jwks.py:39` (`JwksCache`), `app/jwks.py:72` | `tests/test_auth_rs256.py:117`, `:136`, `:149`, `:160` | |
| A token cannot live longer than the configured maximum | `app/auth.py:216` | `tests/test_auth_rs256.py:190` | |
| Token revocation by `jti`, kept in SQLite so it survives a restart | `app/revocation.py:20` (`Denylist`), `app/auth.py:152` and `:223`, `app/main.py:458` (the operator endpoint) | `tests/test_auth_rs256.py:199`, `:300`, `:324` | |
| Rate limit per authenticated caller (`sub`), with its own limit for operators | `app/ratelimit.py:21` (`RateLimiter`), `app/auth.py:143` | `tests/test_auth_rs256.py:226`, `:236`, `:251` | The limiter lives in the process: see [SCALING.md](SCALING.md) |
| Refusals are logged with a fixed reason and never with the token, the claims or the `sub` | `app/auth.py:49` (logger `auth.audit`) | `tests/test_auth_rs256.py:263`, `:275`, `:287` | |
| The app does not start with an incomplete auth setup | `app/auth.py:92-108` | `tests/test_auth.py:233`, `:254` | |

## The customer's identity is injected, never chosen by the model

| Claim | Where in the code | Test | Other evidence |
|---|---|---|---|
| The customer is the token's `sub`; a body `user_id` that differs is refused | `app/main.py:250-261` | `tests/test_api.py`, `tests/test_auth.py` | |
| Support tools are created per request, tied to one customer, and take no user argument | `app/tools/customer_tools.py:43` (`make_support_tools`), `app/agents/support.py:23` | `tests/test_tool_schemas.py:67` (no tool exposes a parameter that selects a user, found automatically), `tests/test_customer_tools.py` | |
| A hostile call that names another customer is refused; injected text does not change whose data is read | `app/tools/customer_tools.py:24` (arguments not declared are an error) | `tests/test_injection.py:37`, `:51`, `:63` | Eval cases `x04_cross_customer_attempt` (`eval/dataset.jsonl:14`), `inj01_override_and_prompt_leak`, `inj02_claims_to_be_an_operator` |

## Human in the loop

| Claim | Where in the code | Test | Other evidence |
|---|---|---|---|
| A ticket the agent asks for waits for a person (LangGraph `interrupt()`, one thread per approval, apart from the conversation's graph) | `app/approvals.py:36` (the import), `app/approvals.py:354` (the `interrupt()` call), `app/graph.py:63-71` | `tests/test_approvals.py:126` (two decisions, one winner), `:160` (expiry), `:208` | |
| Silence never approves: an expired request opens no ticket and goes to the human queue | `app/approvals.py` (reason `approval_expired`) | `tests/test_approvals.py:160` | |
| An escalation in the same turn beats a ticket proposal | `app/handoff.py:67-82` | `tests/test_escalate_vs_ticket.py:79`, `:90` | |
| Handoff queue with claim, reply, lease and an audit trail | `app/handoff_store.py`, `app/main.py` (the `/operator/*` endpoints) | `tests/test_handoff_store.py`, `tests/test_handoff_lease.py`, `tests/test_operator_api.py`, `tests/test_handoff_privacy.py` | |
| Signed webhook to the operators, with no customer text, that never slows a turn | `app/webhook.py:34` (`sign`), `app/webhook.py:38` (`verify`), `app/webhook.py:59` (`WebhookNotifier`) | `tests/test_webhook.py:66`, `:87`, `:107`, `:118` | |
| A handoff nobody claims within the SLA is escalated once | `app/handoff_store.py:457` (`escalate_overdue`), `app/main.py:72` (`sweep_once`) | `tests/test_handoff_sla.py:30`, `:41`, `:53` | |

## Observe, replan and `add_support`

| Claim | Where in the code | Test | Other evidence |
|---|---|---|---|
| A node after the synthesizer audits the answer and can replan; with `REPLAN_MAX=0` (the default) it does nothing | `app/agents/observe.py:121` (`make_observe_node`), `app/graph.py:53-61` (the wiring), `app/config.py:142` | `tests/test_observe.py:83`, `:89`, `:323`, `:647` | `.env.example` (`REPLAN_MAX=0`) |
| Replan 1: one knowledge-base search with the unsupported claims grouped, then only what is still unsupported is deleted | `app/agents/observe.py:144-159` | `tests/test_observe.py:143`, `:162`, `:169` | `eval/reports/REPLAN_AB-20261115-151730.md` (the mechanical measurement) |
| Replan 2: `add_support` (the support agent runs with the authenticated user id, the answers are merged and audited again), or a ticket offer, or an escalation | `app/agents/observe.py:91` (`can_add_support`), `app/agents/observe.py:164` and `:199` (`_add_support`) | `tests/test_observe.py:441`, `:456`, `:475`, `:490`, `:523` | [REPLAN_DEMO.md](REPLAN_DEMO.md) |
| The three judged repetitions of the step on, compared with the five base repetitions by a script, without any interpretation | `eval/compare_runs.py` | `tests/test_compare_runs.py` (synthetic files, the headline line, the same bytes twice, and the regeneration of the committed report) | `eval/reports/REPLAN-AB-20261116-234030.md` (inputs: `eval/reports/20261116-234030.judged.json`, `eval/reports/20261116-234325.judged.json`, `eval/reports/20261116-234617.judged.json` and the five judged files of the final run, `eval/reports/20261115-202155.judged.json` and four more) |
| With the step off, the new code changes nothing | `app/agents/observe.py:129` (the early return) | `tests/test_observe.py:647`, `:661` | `eval/reports/SMOKE-add-support.md` (identity: no replans in any row, agent sets already seen) |
| The step on: how often it fired, and what it cost | | `tests/test_replan_stats.py` | `eval/reports/20261116-234030.json`, `eval/reports/20261116-234325.json`, `eval/reports/20261116-234617.json`, `eval/reports/ADDSUPPORT-CAPTURE-20261116-233738.jsonl`, `eval/reports/REPEATS-20261116-234617.md`, `eval/reports/REPLAN_ON-20261115.md`. The three repetitions of the `add_support` branch were scored by the judge (see the comparison report below); the earlier branch was not |

## The verifier

| Claim | Where in the code | Test | Other evidence |
|---|---|---|---|
| The model lists each claim with a quote and the **code** checks the quote against the evidence; one rewrite deletes what has no support | `app/agents/verifier.py:94` (`quote_in`), `app/agents/verifier.py:107`, `app/agents/verifier.py:226` (`make_verify_node`) | `tests/test_verify.py:55`, `:69`, `:82`, `:88` | |
| An answer that repeats 10 or more consecutive words of a system prompt is replaced by a refusal | `app/guard.py:53` (`leaked_run`) | `tests/test_guard.py:21`, `:27`, `:34` | |
| A segment-by-segment verifier was built, measured and **not adopted** | `app/segments.py` | `tests/test_verify_segments.py`, `tests/test_verifier_ab.py` | `eval/verifier_ab.py`; the comparison table is in the README ("Verifier alternatives") |

## Memory

| Claim | Where in the code | Test | Other evidence |
|---|---|---|---|
| Conversations are checkpointed in SQLite, per customer, with a rolling summary, retention and a limit per customer | `app/memory.py:48` (`Memory`), `app/memory.py:63` (`SqliteSaver`), `app/memory.py:119` (`prune_thread`), `app/main.py:190` | `tests/test_memory.py:51` (survives rebuilding the graph on the same file), `tests/test_memory_threads.py`, `tests/test_summary.py`, `tests/test_retention.py` | |
| The server generates the conversation id; one that is not the caller's is the same 404 as a missing one | `app/main.py:250-261` | `tests/test_conversation_ids.py`, `tests/test_conversations_api.py` | |
| The eval's multi-turn cases survive a restart of the checkpointer | `eval/persistent.py` | `tests/test_eval_persistent.py:38`, `:47` | The memory row of the generated block in the [README](../README.md#results) |

## Failover

| Claim | Where in the code | Test | Other evidence |
|---|---|---|---|
| With `FALLBACK_PROVIDER` set, an outage (5xx, 429, no connection, timeout) is answered by the same model at the other provider; a 400 is not | `app/llm.py:45` (`outage_errors`), `app/llm.py:58` (`fallback_model_name`), `app/llm.py:68` (`get_llm`), `app/llm.py:87` | `tests/test_failover.py:69`, `:94`, `:103` | `app/config.py:44` (the default is empty), `docker-compose.yml:59` (the API) and `:84` (the eval, which has it off) |
| The eval turns failover off, so a report is measured on one provider | `eval/run_eval.py:550` | `tests/test_run_config.py` | |

## Observability

| Claim | Where in the code | Test | Other evidence |
|---|---|---|---|
| `/metrics` exists only with a token, and carries no text or customer id | `app/telemetry.py:30` (`Metrics`), `app/main.py:466-468` | `tests/test_telemetry.py:82`, `:201`, `:209`, `:214`, `:222` | |
| One OTLP trace per turn, a child span per model or tool call, with no text | `app/telemetry.py:86` (`Tracing`), `app/config.py:146` | `tests/test_telemetry.py:102`, `:114`, `:128`, `:142` | `docker-compose.yml:93` (Jaeger), the `observability` profile |
| Prometheus, Grafana and the dashboard are provisioned and bound to localhost | `docker-compose.yml:102` (Prometheus) and `:116` (Grafana), `infra/observability/prometheus.yml`, `infra/observability/grafana/dashboards/getnet.json` | `tests/test_observability_infra.py:43`, `:54`, `:70`, `:78` | The checks against a real Jaeger and Grafana were made by hand while building it; they are not in the repository |
| 9 alert rules, each with a unit test that fires it and one that keeps it quiet | `infra/observability/alerts.yml` | `tests/test_observability_infra.py:36`, `:59`, `infra/observability/alerts_test.yml` (run by `promtool`) | `.github/workflows/ci.yml:80` (job `alerts`) |

## CI

| Claim | Where in the code | Test | Other evidence |
|---|---|---|---|
| Four jobs: `quality` (ruff, mypy, bandit, pip-audit), `test`, `alerts` (promtool), `docker` (build, non-root check, compose, trivy) | `.github/workflows/ci.yml:19`, `:56`, `:80`, `:91` | `tests/test_ci_gates.py:74`, `:80`, `:84` | The result of the runs on GitHub is the project's own account (final report, section 7, item 13) and cannot be checked from a checkout |
| The vulnerabilities pip-audit ignores are only the ones documented | `.github/workflows/ci.yml:48` | `tests/test_ci_gates.py` (the ignored ids equal the documented ones) | The README, "Accepted vulnerabilities" |
| Pinned dependencies for the image | `constraints.txt`, `scripts/lock_deps.py`, `Dockerfile` | `tests/test_constraints.py` | |

## Calibration of the judge (V1, V2, V3)

| Claim | Where | Test | Other evidence |
|---|---|---|---|
| Three human checks of the judge, each with its own sample and its own version of the judge | `eval/calibration/CALIBRATION_V1.md`, `eval/calibration/CALIBRATION_V2.md`, `eval/calibration/CALIBRATION_V3.md` | `tests/test_calibration.py` | `eval/calibration/agreement.py` computes agreement, Cohen's kappa and the mean absolute error |
| V3 is the only independent one: 15 rows fixed before any judge note existed | `eval/calibration/sample.py`, `eval/calibration/sample_v3.json`, `eval/calibration/labels_v3_blind.csv` | `tests/test_calibration_sample.py` | [FINAL_REPORT.md](FINAL_REPORT.md#3-does-the-judge-agree-with-a-human-v3) |
| V2 re-scored with the current judge on 19 rows drawn with a seed fixed beforehand; it is not independent | `eval/calibration/v2_rescore.py`, `eval/calibration/V2_RESCORE_REPORT.md` | `tests/test_v2_rescore.py` | `eval/reports/V2RESCORE19-20261114-192208.judged.json` |

## Repetitions

| Claim | Where | Test | Other evidence |
|---|---|---|---|
| The final run is five full repetitions of the 40 cases on one commit with a clean tree, scored by the judge afterwards | `eval/run_eval.py`, `eval/rejudge.py`, `eval/judge_budget.py` | `tests/test_rejudge.py`, `tests/test_repeats.py`, `tests/test_provenance.py` | `eval/reports/REPEATS-20261115-204526.md` (the spread over the five), `eval/reports/20261115-202155.judged.json` (and four more), [FINAL_REPORT.md](FINAL_REPORT.md) |
| The results in the README and the final report are generated from the saved runs, with the number of rows each rate is over | `eval/results_block.py` | `tests/test_results_block.py`, `tests/test_final_report.py` | |
| The later code behaves like the final run on the checks that need no judge | `eval/same_behavior.py` | `tests/test_same_behavior.py` | `eval/reports/SMOKE-0835e91.md`, `eval/reports/SMOKE-add-support.md` |
