# A real replan, step by step: `q04_pix_bank_account`

What the `observe` step does when it is switched on (`REPLAN_MAX=2`), shown on the one case where `add_support` fired. Everything below comes from saved files of the repository, copied as they are; nothing was re-run for this page.
**All three repetitions on this page were scored by the judge. This page says what the code did and what it cost; it does not claim the answers are better.** The scripted comparison of the three judged repetitions with the five base repetitions is in `eval/reports/REPLAN-AB-20261116-234030.md` (`eval/compare_runs.py`). Its headlines: Repetition 1: **Headline:** candidate 18 pass of 27 regular rows; baseline repetitions 16-19 pass of 27; the candidate count is inside that range; replan fired in 1 of 27 rows. Repetition 2: **Headline:** candidate 20 pass of 27 regular rows; baseline repetitions 16-19 pass of 27; the candidate count is outside that range; replan fired in 2 of 27 rows. Repetition 3: **Headline:** candidate 20 pass of 27 regular rows; baseline repetitions 16-19 pass of 27; the candidate count is outside that range; replan fired in 2 of 27 rows. Each headline says whether the repetition's count is inside or outside the range of the baselines, and it is not the same for the three repetitions; with three repetitions with the replan on against five base repetitions and a judge that is not deterministic, **no statistical inference is made**. The rows whose verdict differs from the baselines are mostly rows where no replan action ran, and a difference there is not evidence about the replan; the report does not establish its cause. `q04` is the only case where `add_support` ran: its verdict in each repetition is in the report's table (it is not the same in all three), the rewrite in repetition 1 lost the profile data (see the replan demo) and repetitions 2 and 3 include it. `q05` and `q08`, where the replan offered a ticket, are in the same table. **A quality benefit is NOT demonstrated.** The step is off by default (`REPLAN_MAX=0`); see [REVIEW_RESPONSE.md](REVIEW_RESPONSE.md) for why.

**This page has two parts.** The first is one case, `q04`, with the step on. The second, from "The replan probe (2026-11-18)", is a preregistered probe of 12 cases with the step off and on, whose numbers are lines copied from a generated report. Neither part claims the answers are better.

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

In repetitions 2 and 3 the answer carries data from the customer's profile (the demo data), which the knowledge agent alone did not have. In repetition 1 it does not, **although the raw audit of the merged answer in that row (`verify_raw`) lists that profile-based statement as supported, with its quote found in the tool output**: the rewrite after the audit dropped a statement the audit had found backed. That is a defect of the rewrite step, seen in one of three runs, and it is not fixed. The smoke without the step also ended with the answer "There are no documented steps for this, and I’m happy to help further if you need more assistance." Whether any of these answers is better than another is **not measured**: the judge scored all three repetitions, and its verdict on this row in each of them is in the comparison report, next to the five base repetitions of the same case; the three verdicts are not the same, and no inference is made from three repetitions.

## Rows that differ without a replan: observations from the saved files

The comparison report lists rows whose verdict differs from the baselines although no replan action ran. These are observations read from the saved JSON files; the cause is not established.

- **m08:** the synthesizer's answer was longer in the replan-on runs, which let the language detector trigger the translation step, so m08's known language failure depends on the length of the answer and is not fixed.
- **q02:** the web search results depend on the run date, and the evidence the answers were built on was not the same in the base runs and the replan-on runs.
- **x01:** the date-relative tool data shifted by a day and the text changed with it; the judge scored that variant as less grounded although its own stated reason says the answer is supported.
- In these cases no replan action ran in the replan-on runs. The cause of each difference is not established.

## The limitation

- **`add_support` fired three times in the whole branch (81 case-runs), all three on this case**, once per repetition. The sample is one case, so nothing about how often it helps, or how often it fires on other questions, follows from it.
- **`is_account_claim` is a plain lexicon (a possessive or a "you have" form with an account noun or a figure), and it is not validated against labeled data.** Its verdict on the first claim of `q04` is a **debatable false positive**: "You must have a bank account linked to your Getnet profile..." is a general requirement of the product, not a fact about this customer, and the knowledge base had already answered the question. The support agent was run on a question that did not need it. The same point is in the final report (section 4). The lexicon was not changed to fit this case: that would be tuning on the one case that fired.
- In the record above `retry_kb` went from "1 unsupported" to "3 still unsupported" after the extra passages were added: the audit lists the claims anew on each call, and the cause of the larger count is not established.
- The branch ran 3 repetitions of the 27 regular cases only (no holdout), and all three repetitions were judged.

## The replan probe (2026-11-18): the step on against the step off, on cases written to make it act

The page above is one case. This section is the preregistered probe ([`REPLAN_PROBE_PREREG.md`](REPLAN_PROBE_PREREG.md), committed with its 12 cases before any run, with two amendments written before the data they refer to). **It is a mechanism diagnostic, not a measure of generalization, and `REPLAN_MAX` stays 0 whatever it shows.** Every number below is a line **copied by a script** from the generated report `eval/reports/REPLAN-PROBE-20261118-133249.md` (`python -m eval.compare_runs --paired ...`); a test fails if a quoted line stops being a line of that report, and no judge rate is typed in this page.

What was run: 12 cases (3 for each of 3 branches, written from the conditions of `observe.py`, and 3 controls), two repetitions with the step off (`REPLAN_MAX=0`) and two with it on (`REPLAN_MAX=2`), agents run interleaved on one day on one clean commit (`4192349`), each run with its own empty folders for tickets and handoffs; then the four runs were scored by the unchanged judge in one process (`eval/reports/20261118-133249.judged.json`, `20261118-133453.judged.json`, `20261118-133646.judged.json`, `20261118-133846.judged.json`). The preflight checked that all rows were to be judged and each found its case before any judge token was spent.

### What the preregistered rule says

<!-- probe:outcome:start -->
> **Outcome of the preregistered rule: null.** Net +0, inside +/-2.
<!-- probe:outcome:end -->

The rule is the one fixed before the runs. A control question made the step act in the ON arm, and under the rule a false trigger on a control rules out "benefit suggested" whatever the pass counts are, so the headline cannot be a benefit. The pairs and what they hold:

<!-- probe:pairs:start -->
> - pairs: 24; valid: 24 (the rule needs at least 20); dropped: 0
> - ON passes 8, OFF passes 8, **Net +0** over the valid pairs
> - discordant pairs: OFF fail -> ON pass 2, OFF pass -> ON fail 2
> - pairs where the ON row acted: 10; among them OFF fail -> ON pass 0, OFF pass -> ON fail 2, **acted-net -2**
> - exact two-sided sign test on those discordant acted pairs (2): p = 0.5000 (descriptive, no threshold)
> - false triggers (a control row of the ON arm with a replan entry): 2
>   - `c03_box_contents` repetition 1: retry_kb
>   - `c03_box_contents` repetition 2: retry_kb
<!-- probe:pairs:end -->

### By group, and by what the ON row did

By design branch (the groups preregistered). B3 favors the ON arm by construction (its criterion rewards a concrete way forward and the ticket offer of the step supplies one); the line B1 + B2 is the reading without that bias. The rule above uses all the valid pairs.

<!-- probe:by_group:start -->
> | group | pairs | valid | ON pass | OFF pass | Net | OFF fail -> ON pass | OFF pass -> ON fail | pairs where ON acted |
> |---|---|---|---|---|---|---|---|---|
> | B1 | 6 | 6 | 1 | 0 | +1 | 1 | 0 | 1 |
> | B2 | 6 | 6 | 0 | 0 | +0 | 0 | 0 | 4 |
> | B1 + B2 (without the bias of B3) | 12 | 12 | 1 | 0 | +1 | 1 | 0 | 5 |
> | B3 | 6 | 6 | 3 | 2 | +1 | 1 | 0 | 3 |
> | control | 6 | 6 | 4 | 6 | -2 | 0 | 2 | 2 |
<!-- probe:by_group:end -->

By what the ON row did in this run (amendment 1 of the preregistration, which says the `ON acted` line is the main reading):

<!-- probe:by_on:start -->
> | what the ON row did | pairs | valid | ON pass | OFF pass | Net | OFF fail -> ON pass | OFF pass -> ON fail | replan entries of the ON rows |
> |---|---|---|---|---|---|---|---|---|
> | ON acted | 8 | 8 | 0 | 0 | +0 | 0 | 0 | offer_ticket 1, retry_kb 8 |
> | ON did not act | 10 | 10 | 4 | 2 | +2 | 2 | 0 | none |
> | controls | 6 | 6 | 4 | 6 | -2 | 0 | 2 | retry_kb 2 |
<!-- probe:by_on:end -->

### Noise, cost and latency

<!-- probe:noise:start -->
> The same cases with the same configuration (`20261118-133249.judged.json` and `20261118-133646.judged.json`): 12 cases have a verdict in both, **0 get a different one**.
<!-- probe:noise:end -->

<!-- probe:cost:start -->
> | arm | rows | mean agent cost | total agent cost | latency p50 (ms) | latency p95 (ms) | rows with a handoff |
> |---|---|---|---|---|---|---|
> | ON | 24 | $0.00198 | $0.04752 | 6,444 | 13,047 | 2 |
> | OFF | 24 | $0.00172 | $0.04132 | 6,043 | 21,253 | 1 |
<!-- probe:cost:end -->

<!-- probe:over:start -->
> Over the 10 ON rows that acted, against the mean of the same case in the OFF runs: extra agent cost +0.00065 USD per row on average (+0.00646 USD in all); extra latency -4,062 ms per row on average and +710 ms at the median (a large difference between the two means a few slow rows of the OFF runs weigh on the average).
<!-- probe:over:end -->

Cost and latency of the ON arm are above those of the OFF arm on average; the retry adds a knowledge-base search and a second audit to the rows where the step acted. The difference between the mean and the median of the latency is in the line above: a few slow rows of the OFF runs weigh on the mean.

### The control that changed verdict (`c03_box_contents`)

The question asked was whether the retry changed the verdict or only the cost. **It changed the answer, and with it the verdict**, in each ON repetition. The entries and scores of the pair, as saved:

<!-- probe:c03a:start -->
> - **`c03_box_contents`, repetition 1** (control): ON fail, OFF pass
>   - replan entries of the ON row: retry_kb: 1 unsupported -> 5 passages -> 0 still unsupported
>   - judge scores, groundedness / completeness: ON 3 / 4, OFF 4 / 5; claims the judge listed as unsupported: ON 1, OFF 1
>   - note of the verifier: ON ``, OFF `revised:1 [No additional mobile‑data subscription is required]`
>   - agent cost: ON $0.00179 against the OFF mean $0.00144; latency (ms): ON 5,015 against the OFF mean 5,643
>   - reason, ON: The answer is in English while the user asked in Portuguese, and it makes an actionable claim about no subscription being required that is not explicitly supported by the provided documentation.
>   - reason, OFF: The answer is in English while the user's question and the provided evidence are in Portuguese, violating the language match requirement.
<!-- probe:c03a:end -->

<!-- probe:c03b:start -->
> - **`c03_box_contents`, repetition 2** (control): ON fail, OFF pass
>   - replan entries of the ON row: retry_kb: 1 unsupported -> 5 passages -> 0 still unsupported
>   - judge scores, groundedness / completeness: ON 3 / 4, OFF 4 / 5; claims the judge listed as unsupported: ON 1, OFF 1
>   - note of the verifier: ON ``, OFF `revised:1 [No additional mobile‑data subscription is required]`
>   - agent cost: ON $0.00179 against the OFF mean $0.00144; latency (ms): ON 9,020 against the OFF mean 5,643
>   - reason, ON: The answer is in English while the user asked in Portuguese, and it makes an actionable claim about no subscription being required that is not explicitly supported by the provided documentation.
>   - reason, OFF: The answer is written in English while the user's question and the provided evidence are in Portuguese, failing the language match requirement.
<!-- probe:c03b:end -->

Read from those lines: in the OFF arm the verifier removed one claim as unsupported and put a sentence of its own in its place; in the ON arm the retry searched again, the second audit found the same claim backed, nothing was removed, and the answer kept it. The judge, which is another model with its own reading of the evidence, listed that claim as unsupported and scored the ON answer lower, and the verdict went from pass to fail. So the difference is not cost: it is the retry and the judge disagreeing about whether the passages back the claim. **A human check after the fact settled that disagreement against the retry: the claim is NOT in the retrieved passages** ([`c03_post_hoc_check_2026-11-18.md`](evidence/c03_post_hoc_check_2026-11-18.md) is the file that was read; the owner made the check, on this one row). The re-audit after `retry_kb` accepted as backed a claim that does not appear in the passages it had just retrieved, and the judge was right to mark it. It does not change the preregistered result. **A limitation of the judge that shows in these lines (one case observed; not generalized):** its `reason` for this case says the question was in Portuguese and the answer in English, while the message of the case is in English and `language_match` was true; the sentence does not agree with the fields next to it, so the reason text of this judge is not evidence of what drove the verdict. It is recorded in the README's known limits and in section 7, item 21, of the final report.

### The one ticket offer (`p07_printer_not_printing`, repetition 1)

<!-- probe:p07:start -->
> - **`p07_printer_not_printing`, repetition 1** (B3): ON fail, OFF fail
>   - replan entries of the ON row: retry_kb: 8 unsupported -> 5 passages -> 8 still unsupported; offer_ticket: no step is left
>   - judge scores, groundedness / completeness: ON 3 / 3, OFF 2 / 3; claims the judge listed as unsupported: ON 4, OFF 8
>   - note of the verifier: ON `revised:8 [Remove the roll and re‑insert it, feeding the paper from the bottom so the roll | From the Get Smart home ...`, OFF ``
>   - agent cost: ON $0.00338 against the OFF mean $0.00268; latency (ms): ON 11,228 against the OFF mean 9,204
>   - reason, ON: The answer contains actionable unsupported claims, specifically the diagnostic logic that a successful printer test combined with offline status indicates a network issue, which is not documented in the provided evide...
>   - reason, OFF: The answer invents specific repair steps (cleaning the print head, paper loading details, and specific menu paths) that are not supported by the provided evidence, violating the case-specific requirement not to invent...
<!-- probe:p07:end -->

The step appended its one-sentence offer to open a ticket. The verdict was fail in both arms in both repetitions of this case, with or without the offer; the offer is a sentence in the answer, not a ticket (no tool opened one).

### What the probe found, and how far it goes

- **The step acts, and mostly in one way.** The entries of the ON rows are in the table by what the ON row did: they are almost all `retry_kb`, which acted on cases of all three design branches and on one control. `add_support` and the escalation did not act in any ON row, and the ticket offer acted once.
- **Recorded as a finding (amendment 1):** the two replan-2 safety nets, `add_support` and the ticket offer with the escalation, **did not act in the ON rows of the firing run nor in those of the run that was judged**, apart from the one offer above; on the 27 regular cases `add_support` acted only in `q04`. The cases were not revised to make them act, because that would have meant messages written to mislead the router.
- **In the pairs where the step acted on the cases designed for it, no verdict changed** (see the line `ON acted` above); the only pairs where it acted and the verdict changed are the control's. The pairs whose verdict differs although the ON row has no replan entry are the same system in both rows, which is what the noise guard of the rule is for.
- **One observed case in which the verifier was more lenient than the judge (`c03`, human check after the fact, one row; not generalized).** The re-audit after the retry accepted a claim that the retrieved passages do not contain, and the judge flagged it. Nothing is claimed about other rows or about how often this happens.
- **The number of passes is low in both arms** (the table by group shows it), which leaves the probe little room to show a difference in either direction. The criteria ask for documented steps or a plain "I cannot confirm"; whether the cases, the criteria or the judge's strictness explain the low counts is not established, and it is not a finding about the step.
- **What it does not show:** how often the step acts on real traffic, whether the answers are better, or anything about the 27 regular cases. 24 pairs and a judge that does not give the same verdict twice are a direction at best. **`REPLAN_MAX=0` stays the default.**

### Decision on Phase 5

Phase 5 of the plan of the day (two more repetitions with the step on over the 27 regular cases, to add to the three judged ones) was **skipped, by decision of the owner on 2026-11-18**. It is a decision, not an omission: the headline numbers of the comparison on the regular cases remain those of `eval/reports/REPLAN-AB-20261116-234030.md` (three judged repetitions), and nothing in this probe adds to or replaces them.
