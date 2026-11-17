# A real replan, step by step: `q04_pix_bank_account`

What the `observe` step does when it is switched on (`REPLAN_MAX=2`), shown on the one case where `add_support` fired. Everything below comes from saved files of the repository, copied as they are; nothing was re-run for this page.
**These answers were not scored by the judge, so this page says what the code did and what it cost, not whether the answers are better.** The step is off by default (`REPLAN_MAX=0`); see [REVIEW_RESPONSE.md](REVIEW_RESPONSE.md) for why.

Sources: `eval/reports/20261116-234030.json` (repetition 1 of the branch `replan on add_support`: code at commit `b800b8e`, tree clean, `replan_max=2`), `eval/reports/ADDSUPPORT-CAPTURE-20261116-233738.jsonl` (what the account check saw), and, for the cost without the step, `eval/reports/20261116-232753.json` (the smoke at commit `3e7ad43` with `REPLAN_MAX=0`, the same case, one run).

## The question

> Do I need a bank account to receive my sales via Pix?

## What the system did

1. The router sent **only the knowledge agent** (`plan: ["knowledge"]`). It searched the knowledge base and answered.
2. `observe` audited that answer against the evidence and found claims without support. It ran **replan 1** (`retry_kb`): one more search of the knowledge base with the unsupported claim as the query (the `observe` entry in `tool_calls` below), and audited again against the old evidence plus the 5 new passages.
3. Claims were **still** unsupported, so **replan 2** was considered. `can_add_support` was true: the router had not sent the support agent, there is a customer, and the first claim was marked as being about the customer's account by `is_account_claim` (the capture file below).
4. `observe` ran the **support agent** with the authenticated customer (one tool call, `get_customer_profile`), had the synthesizer merge both answers, audited the merge, and rewrote it once without the claims still unsupported.
5. The verify node kept the note and did not audit the answer a second time (`verify_note` ends with `added_support`).

## The record, verbatim

From `eval/reports/20261116-234030.json`, row `q04_pix_bank_account`, with the fields that matter (the rest of the row is the evidence and the raw audit):

```json
{
  "id": "q04_pix_bank_account",
  "message": "Do I need a bank account to receive my sales via Pix?",
  "plan": ["knowledge"],
  "tools_called": ["get_customer_profile", "search_getnet_kb", "search_getnet_kb"],
  "tool_calls": [
    {"agent": "support", "name": "get_customer_profile", "args": {}},
    {"agent": "knowledge", "name": "search_getnet_kb", "args": {"query": "receber vendas via Pix conta bancária necessária"}},
    {"agent": "observe", "name": "search_getnet_kb", "args": {"query": "You must have a bank account linked to your Getnet profile in order to receive sales made with Pix."}}
  ],
  "replans": [
    "retry_kb: 1 unsupported -> 5 passages -> 3 still unsupported",
    "add_support: 3 unsupported -> support ran (1 tool calls) -> merged -> 2 still unsupported"
  ],
  "handoff": null,
  "verify_note": "revised:2 [You need a bank account linked to your Getnet profile to receive sales made with | If you ever remove or need to change that bank account, Pix transfers will stop ]; added_support",
  "routing_ok": true,
  "tools_ok": true,
  "handoff_ok": true,
  "degraded": false,
  "latency_ms": 10585,
  "answer": "There are no documented steps for this, and we are happy to help further."
}
```

What the account check saw, from `eval/reports/ADDSUPPORT-CAPTURE-20261116-233738.jsonl` (the first call of that file, repetition 1):

```json
{
  "case": "q04_pix_bank_account",
  "repetition": 1,
  "call": 1,
  "question": "Do I need a bank account to receive my sales via Pix?",
  "claims": [
    {"text": "You must have a bank account linked to your Getnet profile in order to receive sales made with Pix.", "account": true},
    {"text": "The Pix settlement is transferred to the bank account you register in the app/portal.", "account": false},
    {"text": "If no account is linked, the Pix product cannot be enabled.", "account": false}
  ],
  "can_add_support": true,
  "has_user_id": true,
  "support_in_plan": false,
  "support_in_outputs": false,
  "plan": ["knowledge"]
}
```

(Pretty-printed. The file holds one such object per line and records no customer id.)

## What it cost

All the figures are from the `usage` and `latency_ms` of the rows. The three repetitions of the branch fired the same two replans on this case; the run without the step is a single run of the smoke, so it shows the size of the difference, not a spread.

| | latency | agent cost | tokens | model calls of the `observe` node |
|---|---|---|---|---|
| `REPLAN_MAX=0` (smoke `20261116-232753`, `3e7ad43`) | 4,247 ms | $0.00138 | 7,422 | none (the step is off; the verify node made 2 calls) |
| `REPLAN_MAX=2`, repetition 1 (`20261116-234030`) | 10,585 ms | $0.00339 | 18,234 | 7 ($0.00260 of the total) |
| `REPLAN_MAX=2`, repetition 2 (`20261116-234325`) | 8,779 ms | $0.00348 | 18,378 | 7 |
| `REPLAN_MAX=2`, repetition 3 (`20261116-234617`) | 8,680 ms | $0.00348 | 18,376 | 7 |

So on this case the step multiplied the latency by about 2 to 2.5 and the agent cost by about 2.5, and it also made one extra call to the knowledge base (a Voyage embedding, not in the agent cost). Routing, tool use and handoff checks are identical to the smoke in all 81 case-runs of the branch (the other 26 cases never ran `add_support`; two of them, `q05` and `q08`, ran the ticket offer instead).

## What the answers were

The same case ended with different text in the three repetitions (read from the `answer` field of each row):

- Repetition 1: "There are no documented steps for this, and we are happy to help further."
- Repetition 2: "Your account already has an Itaú bank account connected, so Pix payouts are enabled. Let me know if you’d like to update your banking details or need anything else."
- Repetition 3: "Your account already has an Itaú bank account connected, so Pix payouts are enabled. There are no documented steps for this, and we’re happy to help further."

In repetitions 2 and 3 the answer carries data from the customer's profile (the demo data), which the knowledge agent alone did not have. In repetition 1 it does not, **although the raw audit of the merged answer in that row (`verify_raw`) lists that profile-based statement as supported, with its quote found in the tool output**: the rewrite after the audit dropped a statement the audit had found backed. That is a defect of the rewrite step, seen in one of three runs, and it is not fixed. The smoke without the step also ended with the answer "There are no documented steps for this, and I’m happy to help further if you need more assistance." Whether any of these answers is better than another is **not measured**: no judge has scored them.

## The limitation

- **`add_support` fired three times in the whole branch (81 case-runs), all three on this case**, once per repetition. The sample is one case, so nothing about how often it helps, or how often it fires on other questions, follows from it.
- **`is_account_claim` is a plain lexicon (a possessive or a "you have" form with an account noun or a figure), and it is not validated against labeled data.** Its verdict on the first claim of `q04` is a **debatable false positive**: "You must have a bank account linked to your Getnet profile..." is a general requirement of the product, not a fact about this customer, and the knowledge base had already answered the question. The support agent was run on a question that did not need it. The same point is in the final report (section 4). The lexicon was not changed to fit this case: that would be tuning on the one case that fired.
- The re-audit is not stable between calls: `retry_kb` went from "1 unsupported" to "3 still unsupported" with more passages, because the audit lists claims again each time (the same variability the verifier showed on other drafts).
- The branch ran 3 repetitions of the 27 regular cases only (no holdout), with no judge.
