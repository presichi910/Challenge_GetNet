# Evaluation report

_Generated 20261114-115916_

_Models: agent provider = `fireworks`, knowledge = `accounts/fireworks/models/gpt-oss-120b`, router / synthesizer = `accounts/fireworks/models/gpt-oss-120b`, support = `accounts/fireworks/models/gpt-oss-120b`, judge = `qwen/qwen3.8-27b (groq)`_

## Summary

_Judge scores are shown as a percentage of the maximum (5)._

_The summary covers the 22 regular cases; the 5 holdout cases are summarized apart, below. `provider_errors` counts both._

- **provider_errors**: 0
- **routing_accuracy**: 91%
- **tool_use_accuracy**: 100%
- **degraded_rate**: 0%
- **latency_ms_p50**: 3278
- **latency_ms_p95**: 17780
- **handoff_accuracy**: 95%
- **handoff_rate**: 23%
- **avg_relevance**: 96%
- **avg_groundedness**: 93%
- **avg_completeness**: 89%
- **avg_tone**: 98%
- **judge_pass_rate**: 95%
- **agent_tokens**: 139276
- **judge_tokens**: 45706
- **agent_tokens_per_case_mean**: 5158
- **agent_cost_usd**: $0.0253
- **judge_cost_usd**: $0.0439
- **total_cost_usd**: $0.0691

## Holdout (blind cases, never used to tune prompts)

- **cases**: 5
- **provider_errors**: 0
- **routing_accuracy**: 80%
- **tool_use_accuracy**: 100%
- **degraded_rate**: 0%
- **latency_ms_p50**: 3609
- **latency_ms_p95**: 9527
- **handoff_accuracy**: 100%
- **handoff_rate**: 20%
- **avg_relevance**: 84%
- **avg_groundedness**: 92%
- **avg_completeness**: 80%
- **avg_tone**: 96%
- **judge_pass_rate**: 60%
- **agent_tokens**: 38248
- **judge_tokens**: 11610
- **agent_tokens_per_case_mean**: 7650
- **agent_cost_usd**: $0.0066
- **judge_cost_usd**: $0.0107
- **total_cost_usd**: $0.0173

## Tokens and estimated cost per model

_Prices: app/observability.py (USD per 1M tokens, list prices of 2026-11-12); a model without a price shows `n/a`, not 0._

| role | model | calls | input | output | cost USD |
|---|---|---|---|---|---|
| agents | `accounts/fireworks/models/gpt-oss-120b` | 98 | 129559 | 9717 | $0.0253 |
| judge | `qwen/qwen3.8-27b` | 27 | 43428 | 2278 | $0.0439 |

## Cases

_tokens / cost: the agents of the case (the judge is apart, above)._

| id | route | tools | handoff | rel | grounded | compl | tone | verdict | ms | tokens | cost USD |
|---|---|---|---|---|---|---|---|---|---|---|---|
| q01_compare_plans | OK ['knowledge'] | OK | OK  | 100% | 80% | 80% | 100% | pass | 12683 | 4101 | $0.0007 |
| q02_weather | OK ['knowledge'] | OK | OK  | 100% | 80% | 80% | 100% | pass | 6036 | 4864 | $0.0008 |
| q03_payout_timing | OK ['support'] | OK | OK  | 100% | 100% | 100% | 100% | pass | 1928 | 3556 | $0.0006 |
| q04_pix_bank_account | OK ['knowledge'] | OK | OK  | 100% | 80% | 80% | 100% | pass | 6918 | 8494 | $0.0014 |
| q05_terminal_offline | OK ['support', 'knowledge'] | OK | OK  | 100% | 100% | 80% | 100% | pass | 17780 | 8983 | $0.0019 |
| q06_receivables_advance | OK ['knowledge'] | OK | OK  | 100% | 100% | 100% | 100% | pass | 4836 | 4725 | $0.0010 |
| q07_fx_euro | OK ['knowledge'] | OK | OK  | 100% | 100% | 80% | 100% | pass | 5634 | 3787 | $0.0007 |
| q08_decline_error | OK ['support', 'knowledge'] | OK | OK  | 100% | 100% | 100% | 100% | pass | 22577 | 12999 | $0.0028 |
| q09_installments | OK ['knowledge'] | OK | OK  | 100% | 100% | 80% | 100% | pass | 2709 | 4539 | $0.0008 |
| q10_payment_link_whatsapp | OK ['knowledge'] | OK | OK  | 100% | 100% | 100% | 100% | pass | 3710 | 4097 | $0.0007 |
| x01_portuguese | OK ['support'] | OK | OK  | 100% | 100% | 100% | 100% | pass | 3278 | 3640 | $0.0007 |
| x02_greeting | OK [] | OK | OK  | 100% | 100% | 100% | 100% | pass | 903 | 1180 | $0.0002 |
| x03_unknown_customer | OK ['support'] | OK | - agent_unresolved | 100% | 100% | 80% | 100% | pass | 1914 | 4481 | $0.0007 |
| x04_cross_customer_attempt | OK ['support'] | OK | OK  | 100% | 100% | 100% | 100% | pass | 1897 | 2116 | $0.0004 |
| h01_human_request | OK [] | OK | OK user_requested | 100% | 100% | 100% | 100% | pass | 636 | 969 | $0.0002 |
| h02_human_request_pt | OK [] | OK | OK user_requested | 100% | 100% | 100% | 100% | pass | 845 | 977 | $0.0002 |
| h03_unanswerable | OK ['support', 'knowledge'] | OK | OK agent_unresolved | 100% | 100% | 100% | 100% | pass | 4463 | 5343 | $0.0009 |
| m01_recall_color | FAIL ['knowledge'] | OK | FAIL agent_unresolved | 20% | 20% | 20% | 60% | fail | 3248 | 3923 | $0.0007 |
| m02_recall_which_bigger | OK [] | OK | OK  | 100% | 100% | 100% | 100% | pass | 2253 | 1302 | $0.0003 |
| m03_rephrase_previous | FAIL ['knowledge'] | OK | OK  | 100% | 100% | 100% | 100% | pass | 4875 | 5054 | $0.0009 |
| m04_followup_product | OK ['knowledge'] | OK | OK  | 100% | 80% | 100% | 100% | pass | 3155 | 4327 | $0.0007 |
| m05_followup_payout | OK ['support', 'knowledge'] | OK | OK  | 100% | 100% | 80% | 100% | pass | 6106 | 7571 | $0.0013 |
| ho01_get_tap (holdout) | OK ['knowledge'] | OK | OK  | 100% | 100% | 100% | 100% | pass | 3609 | 4088 | $0.0007 |
| ho02_profile_plan_bank (holdout) | FAIL ['support', 'knowledge'] | OK | OK  | 100% | 100% | 100% | 100% | pass | 9527 | 23731 | $0.0038 |
| ho03_advance_enabled_and_how (holdout) | OK ['support', 'knowledge'] | OK | OK  | 100% | 60% | 80% | 100% | fail | 8374 | 8205 | $0.0016 |
| ho04_human_request_es (holdout) | OK [] | OK | OK user_requested | 100% | 100% | 100% | 100% | pass | 795 | 1002 | $0.0002 |
| ho05_out_of_scope_poem (holdout) | OK [] | OK | OK  | 20% | 100% | 20% | 80% | fail | 1485 | 1222 | $0.0002 |

## Judge failures

- **m01_recall_color**: The assistant failed to recall the user's favorite color from the conversation history and incorrectly escalated a simple conversational query to a human agent.
- **ho03_advance_enabled_and_how**: The answer invents specific details not present in the evidence, such as the '1 business day' deposit time and the option to select a specific sales period (e.g., 'last 30 days'), whereas the knowledge base indicates a simulation process and a minimum value without specifying a fixed deposit timeframe or period selection method.
- **ho05_out_of_scope_poem**: The assistant failed to address the user's request for a poem, instead providing a generic refusal that did not follow the expected behavior of politely declining or providing a tiny poem.