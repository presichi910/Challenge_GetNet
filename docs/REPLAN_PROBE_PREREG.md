# Replan probe: preregistration

Written and committed **before any run of the probe**. The commit that adds this file and `eval/probe/replan_probe.jsonl` is the seal: nothing in them was written after seeing a row of the probe. Later changes are amendments, listed at the end with their own commit.

**What this is, in one sentence.** A mechanism diagnostic: 12 cases written by reading the conditions in `app/agents/observe.py`, run with the replan step off and on, to see whether the answer gets better *when the step acts*. **It does not measure generalization, and `REPLAN_MAX` stays 0 whatever it shows.**

## Why

On the 27 regular cases the replan step acted in 1 to 2 rows per repetition, so the comparison of the final run cannot show a benefit even if there is one (`docs/FINAL_REPORT.md`, section 4; `eval/reports/REPLAN-AB-20261116-234030.md`). The probe makes the step act more often, with cases that have a reason to make it act, and has controls that should not make it act.

## The conditions that make each branch act, by file and line

All of `app/agents/observe.py` at `98ea315` (`app/` is the same on this branch). `REPLAN_MAX` is read at `app/config.py:142` (default 0).

| Branch | What has to be true | Where |
|---|---|---|
| **Gate** (all branches) | `REPLAN_MAX >= 1`; the final answer is not empty, not the fallback message, has no synthesizer error, no handoff and no prompt leak | `observe.py:129-131` |
| | the turn has evidence, and it used the support agent or a Getnet knowledge-base search (a web answer is left alone) | `observe.py:132-134`, `getnet_context` `:98-102` |
| **Audit** | the checker (the verifier's model, with the quote check done by code) lists at least one claim with no support | `observe.py:143` (`audit_claims`, `app/agents/verifier.py:194`) |
| **B1, replan 1 `retry_kb`** | at least one unsupported claim and `REPLAN_MAX >= 1`: ONE knowledge-base search with the claims joined (600 characters), the same answer audited again against the old evidence plus the new passages | `observe.py:144-156`; query `:147-150`; failure path `:157-159` |
| **B2, replan 2 `add_support`** | after the retry, claims still unsupported; `REPLAN_MAX >= 2`; the router did NOT send the support agent (it is not in the plan or the outputs); there is a user id; at least one still-unsupported claim is "about the customer's account" | `observe.py:164` and `can_add_support` `:91-95` |
| | "about the account" = not a step, a possessive or "you have" form (`POSSESSIVE` `:68-69`) together with a noun of the account or a figure (`ACCOUNT_NOUNS` `:70-76`, `FIGURE` `:77`); a plain lexicon, no model | `is_account_claim` `:84-88` |
| | what it does: the support agent runs with the authenticated user id, the two answers are merged and the merge is audited again; if the support agent cannot resolve it, a human takes the case; if anything fails, nothing was done and the turn goes on to the next two rows | `_add_support` `:199-247` (support `:213`, handoff `:220-226`, merge `:227`, audit `:232`, failure `:235-238`) |
| **B3a, replan 2 `offer_ticket`** | claims still unsupported; `REPLAN_MAX >= 2`; the original answer had steps (an action verb first or a menu path) and the rewritten answer has none: an offer to open a ticket is appended in the customer's language | `observe.py:179-185`; `is_step` `:58-62`, `has_steps` `:105-106` |
| **B3b, replan 2 `escalate`** | claims still unsupported; `REPLAN_MAX >= 2`; NOTHING in the answer is backed (`supported == 0`); the original had steps: the draft is dropped and the case goes to a human (`agent_unresolved`) | `observe.py:171-178` |
| **Precedence** | `add_support` is tried before `escalate` and `offer_ticket` (`:164` before `:171`); one action per turn | `observe.py:164-168` |

Two things the table says that shape the design. First, **no branch can be made to act by the message alone**: every one needs the checker to list an unsupported claim, which depends on what the agents write. A case can only be given a *reason* to make a branch act. Second, B2 needs the **router to leave the support agent out** and the claim to match the lexicon; the lexicon also matches a general rule written to the customer ("you need a bank account", the case `q04` of the regular set, which the README already calls a debatable false positive).

## The cases

`eval/probe/replan_probe.jsonl`: 12 cases, the schema of the regular cases (`id`, `user_id`, `message`, `expected_agents`, `optional_agents`, `expected_tools`, `expect_handoff`, `criteria`), no `holdout` field. They are in their own file and never enter `eval/dataset.jsonl` or the generated results block (it would change every n).

| id | Branch it has a reason to reach | User | Message | What the merchant needs (the criterion the judge gets) |
|---|---|---|---|---|
| `p01_reprint_receipt` | B1 | cliente1988 | How do I reprint the receipt of a sale on my Get Smart? | The steps Getnet documents, no made-up menus or screens; if they cannot be found, say so plainly and give a concrete way to get help |
| `p02_close_day_sales` | B1 | cliente1988 | How do I do the end-of-day sales closing on the Get Smart? | The same, for closing the day's sales |
| `p03_antecipar_na_maquininha` | B1 | cliente1988 | Como faço para antecipar minhas vendas direto na maquininha? | The same, for requesting an advance on the terminal, in Portuguese |
| `p04_pix_fee` | B2 | cliente1988 | How much does Getnet charge me when a customer pays with Pix? | A fee only if the documentation or the merchant's own account data supports it; otherwise say plainly it cannot confirm it and where to check it or whom to ask |
| `p05_pix_savings_account` | B2 | cliente2024 | Can I receive my Pix sales in a savings account, or does it have to be a checking account? | The rule only if the documentation or the account data supports it; otherwise say plainly it cannot confirm it and how to find out |
| `p06_payment_link_cost` | B2 | cliente1988 | Do I have to pay anything to use the Payment Link? | A cost, or its absence, only if supported; otherwise say plainly it cannot confirm it and where to check it or whom to ask |
| `p07_printer_not_printing` | B3 | cliente1988 | I loaded a new paper roll in my Get Smart but it still won't print receipts. What should I do? | Help with what is documented or in the terminal data; if it cannot, say so plainly and give a concrete way forward (a contact channel, a support ticket, or a person taking the case); no invented repair steps |
| `p08_software_update` | B3 | cliente1988 | How do I update the software of my Get Smart? | The documented way, or say plainly it has none and give a concrete way forward; no invented menu steps |
| `p09_reinicia_sozinha` | B3 | cliente2024 | Minha maquininha fica reiniciando sozinha, o que eu faço? | In Portuguese; help with what is documented or in the terminal data, or say plainly it cannot and give a concrete way forward; no invented repair steps |
| `c01_payout_status` | control | cliente2024 | What is the status of my last payouts? | The payouts from the merchant's own account data, nothing the data does not show |
| `c02_biggest_sale_yesterday` | control | cliente1988 | What was my biggest approved sale yesterday? | The largest approved sale of yesterday from the transaction data, with amount and method, nothing more |
| `c03_box_contents` | control | cliente1988 | What comes in the box with the Get Smart, and does it include mobile data? | What the documentation says comes with the terminal and whether mobile data is included, nothing it does not state |

The full text of each criterion is in the file; `tests/test_replan_probe_cases.py` pins the schema, the 3 + 3 + 3 + 3 split, that no message is a copy of a dataset message, that none asks for a ticket, a person or any write, and that **no criterion names a mechanism** (an agent, a tool, a retry, a replan, a search).

**How the criteria were checked for neutrality.** Each one is written from what the merchant needs: *solve it with documented facts or with the merchant's own data, or say clearly that it cannot and give a concrete way forward*. For each case the question asked was: **would a system with no replan step that answered well pass this criterion?** Yes for all 12: a correct documented answer, or an honest "I cannot confirm this, here is whom to ask" with a real channel, passes. **A caveat that is the other way round** (declared here so nobody finds it later): the B3 criteria and the B2 ones reward "a concrete way forward". The replan step adds one by construction (the ticket offer), and the step off may end with "no documented steps" and no way forward. A better pass count for the step on in B3 therefore partly measures that the offer exists, which is its purpose; it is not evidence that the facts in the answers are more correct.

**Style and language.** Chosen by reading the 27 regular cases (the holdout was not opened for this): single-turn questions of a merchant, English with a few in Portuguese (2 of 12 here, 2 of 27 there), almost all as `cliente1988`, one or two as `cliente2024`. No message exists to touch the lexicon of `is_account_claim`. **If a branch can only be reached with a forced phrase, that is a finding and the phrase is not forced.**

**Controls** are three questions a healthy system answers well the first time (two from the customer's own data, one from the documentation that holds the answer), to count false triggers and the extra cost of the step on.

## The runs

- **Arms:** OFF = `REPLAN_MAX=0`, ON = `REPLAN_MAX=2`; same commit, same code, same models (`RAG_MODE=dense`, `VERIFIER_MODE=statements`, failover and approvals off in the eval), same judge.
- **Repetitions:** 2 per arm, so 12 x 2 x 2 = **48 rows**. Order of the agent runs: OFF 1, ON 1, OFF 2, ON 2. Order of the judge: the same, one process at a time.
- **Judge budget:** the daily cap is 391,000 with a stop line at 381,000 (`rejudge --daily-limit 391000`); the estimate and the margin at the time of the run are reported before any judge token is spent.

## What is measured

**Primary (the only one the decision uses): the judge's `pass` in paired rows.** A *pair* is a case and a repetition number: the ON row and the OFF row of the same case with the same repetition number. A pair counts only if **both** rows have a verdict of `pass` or `fail` (a judge error drops the pair; the dropped pairs are counted and listed). A `pass` is `verdict == "pass"` of the unchanged judge.

**Descriptive only; none of these enters the decision rule:** the rate of each branch acting (from the `replans` of each ON row: `retry_kb`, `add_support`, `offer_ticket`, `escalate`, and the `*_failed` kinds); false triggers on the controls; the structural expectations of the schema (route, tools, handoff); the judge's groundedness and completeness; agent cost and latency (p50, p95) per arm; the number of tickets and handoffs written; the pairs where the step acted (ON row with at least one replan entry) shown apart; and, as a noise reference, how many cases get a different verdict between OFF 1 and OFF 2.

## The decision rule

*(Amended before any datum: read Amendment 0 at the end of this page; where they differ, the amendment is the rule.)*

Let **N** be the number of valid pairs (at most 24) and **Net** = (ON passes) minus (OFF passes) over those pairs. A **false trigger** is an ON row of a control case with any replan entry.

- **Benefit suggested:** N >= 20 **and** Net >= +3 **and** no false trigger on any of the 6 ON control rows.
- **Harm suggested:** N >= 20 and Net <= -3.
- **In any other case** (|Net| <= 2; or Net >= +3 with a false trigger; or N < 20) the result is reported as **null** (or **inconclusive** if N < 20), not as a benefit.

"Suggested" is on purpose: 24 pairs, a judge that does not give the same verdict twice, and one run per arm cannot show an effect, only a direction. The report lists the discordant pairs (ON pass with OFF fail, OFF pass with ON fail) and the OFF 1 against OFF 2 noise reference next to the rule, and says which of the four outcomes it is, **whatever it is**. It is written in `docs/REPLAN_DEMO.md`, in row *a* of `docs/REVIEW_RESPONSE.md` and in `docs/EVIDENCE.md` with the generated lines.

## Before the judge: the run that counts firing (no judge, `REPLAN_MAX=2`)

A first run with no judge and the step on (2 repetitions of the 12 cases, about 24 case-runs, a few cents of agent cost) shows whether each branch acts at all. A case **fires** when at least one of its 2 rows has an entry of its branch (`retry_kb` for B1, `add_support` for B2, `offer_ticket` or `escalate` for B3). A branch is **under-firing** when fewer than half of its cases fire (so, with 3 cases, when 1 or none fires).

- If a branch under-fires, **one** revision of **that branch's cases** is allowed (message and criterion), declared below as an amendment with its own commit **before anything is judged**, under the same rules as above (neutral criterion, no forced phrase, no holdout), and the firing run is repeated once for those cases.
- If it still under-fires, **stop and report it**: that is already a finding (the branch does not act on natural questions of this kind).
- A revision may not touch the decision rule, the controls or the other branches, and it looks at firing only, never at the quality of an answer. It is declared that choosing cases because they make the step act favors showing the mechanism; that is what a mechanism probe is for, and it is why the result cannot be read as a rate.

## Side effects of the runs, and how they are cleaned up

- **Tickets.** In the eval no approval is configured (`build_default_graph` without `approvals`, `app/graph.py:79`, `app/service.py:37`): `interrupt()` is never reached and `create_support_ticket` would open a ticket at once. None of the 12 messages asks for a ticket, and the support agent's prompt says to offer one and open it only if the customer asks or accepts, so none is expected from the tool. The ticket offer of `offer_ticket` is a sentence in the answer, not a record.
- **Handoffs.** `escalate`, the support agent's own escalation and `add_support` when the added agent cannot resolve the case lead to the handoff node (`app/handoff.py:172-218`), which writes **a ticket of category `human_handoff`** (`:188`) and **one audit line** (`HANDOFFS_FILE`). That can happen in both arms.
- **Cleanup.** Every run uses its own empty folder for `TICKETS_FILE`, `HANDOFFS_FILE` and `HANDOFFS_DB_PATH` (settings of `app/config.py`, so no code change), so the project's `data/` is not touched, a ticket of one run cannot make another run's ticket an `already_existed` duplicate or show up as an open ticket to the support agent, and the arms start from the same state. The number of tickets and handoffs each run wrote is counted from those files and reported; the folders are then deleted. The runs add to `data/` the hashed query cache of the retrieval (`rag_cache.sqlite`, hashes of queries, never text), and the retrieval opens the Chroma store (`data/chroma/chroma.sqlite3`) to read it, which changes the file's modification date; no ticket, handoff or approval file of `data/` is written. (Corrected by amendment 1: this sentence said the cache was the only change.)

## What has to exist before the runs (4d to 4f), found while preparing this

1. **`eval/run_eval.py` reads only `eval/dataset.jsonl`** (`load_cases`, line 45), and **the judge's batch tool `eval/rejudge.py`, which must not change, takes each row's criterion from that same `load_cases`** and skips a row whose id is "not in the dataset". So the probe cannot be run or judged as it is. The change planned, in `eval/` and with tests: `load_cases` reads a path from the environment (`EVAL_DATASET`) when it is set, and `run_eval` gets a flag `--dataset` that sets it. With it unset nothing changes, and `rejudge` works on the probe without being touched. It is done at the start of 4d, committed with its tests, before any run.
2. **`eval/compare_runs.py` does not pair rows** (it compares one candidate with baseline repetitions and counts passes). A generated paired section (valid pairs, Net, discordant pairs, false triggers, firing by branch, cost and latency, the OFF 1 against OFF 2 reference) is added to it with tests, for the report `eval/reports/REPLAN-PROBE-<stamp>.md`. It is never passed to `eval/results_block.py`.

## Declarations

- **Who wrote what.** The cases, the criteria and this page were written by Claude Code under the owner's direction. No human labelled anything here; the judge scores the rows.
- **What was read to write them:** `app/agents/observe.py`; the 27 regular cases (for style; the holdout was filtered out and not read); the fake customer data (`app/data/customers.json`); the crawled pages of the knowledge base (`data/raw/pages.jsonl`) by keyword, offline, to see which topics have documented steps and which do not. **No answer, verdict or report of any earlier run was read for this**, but the documents say that `q04` (a "do I need a bank account" question) is the one regular case where `add_support` ran, and the B2 cases were written knowing that.
- **The cases are not independent of the mechanism**: they were designed from its conditions. A benefit seen here would say the step can help when it acts; it would not say how often it acts on real traffic, and a null would not say the step is useless.
- **The default stays at `REPLAN_MAX=0`** whatever the result.

## Record of the firing run (4d), 2026-11-18

A record, not an amendment: it changes no rule. Two runs of the 12 cases, **no judge**, `REPLAN_MAX=2`, on commit `b9a4902` with a clean tree (command: `python -m eval.run_eval --dataset eval/probe/replan_probe.jsonl --replan-max 2 --no-judge --pause 3 --retries 3 --require-clean`, with `TICKETS_FILE`, `HANDOFFS_FILE` and `HANDOFFS_DB_PATH` pointing at an empty folder per run). The raw files are `eval/reports/20261118-131849.json` and `eval/reports/20261118-132136.json` (diagnostic only: they are the input of no report and are not the ON arm). Nothing below says anything about the quality of an answer.

| Branch (the entry that shows it acted) | Cases that fire, repetition 1 | repetition 2 | Cases that fire in at least one | Under-firing? (fewer than half) |
|---|---|---|---|---|
| B1 `retry_kb` | 1 of 3 (`p01`) | 1 of 3 (`p01`) | 1 of 3 | yes |
| B2 `add_support` | 0 of 3 | 0 of 3 | 0 of 3 | yes |
| B3 `offer_ticket` or `escalate` | 0 of 3 | 0 of 3 | 0 of 3 | yes |

- **Entries seen:** only `retry_kb`, 10 in all (5 cases in each repetition: `p01`, `p04`, `p06`, `p07`, `p09`). No `add_support`, `offer_ticket`, `escalate` or `*_failed` entry in any of the 24 rows.
- **After the retry,** 7 of the 10 rows had no unsupported claim left (so a second action had nothing to act on); the 3 other kinds of leftovers were `p04` (1 claim, both repetitions) and `p09` (1 and 2 claims). `p04` had a router plan with the knowledge agent only, so the router condition of `add_support` held; its leftover claim does not match `is_account_claim` (no possessive). `p09` had the support agent in the plan, so `add_support` could not apply (`observe.py:164`, `can_add_support` `:95`).
- **`p08_software_update`** ended in a handoff (`agent_unresolved`) in both repetitions before the step ran: a turn with a handoff is left alone by `observe` (`observe.py:129`).
- **False triggers on the controls:** none (0 of 3 in each repetition, no entry of any kind).
- **Agent cost:** 0.0233 USD per run (0.0467 USD for both); about 116,500 tokens per run; latency p50 11.2 s and 8.5 s, p95 18.1 s and 17.8 s. Mean cost per row: B1 0.0021, B2 0.0019, B3 0.0026, controls 0.0011 USD.
- **Written, per run:** 1 handoff and 1 ticket (`human_handoff`, for `p08`) in each run's own folder, 2 and 2 in all; no ticket was opened by a tool. The project's `data/tickets.jsonl` and `data/handoffs.jsonl` were not touched (their modification dates are 15 and 16 November). `data/rag_cache.sqlite` grew (hashed queries) and the modification time of `data/chroma/chroma.sqlite3` changed (the store was opened for reading by the retrieval); nothing else under `data/`.
- **What the prereg says to do next:** all three branches under-fire, so **one** revision of the cases of those branches is allowed, as an amendment with its own commit before anything is judged, followed by one more firing run of the revised cases; if a branch still under-fires after that, stop and report it as a finding.

## Amendments

### Amendment 0 (written before any row of the probe was run, judged or read; commit of its own)

It changes the decision rule and the reports, and it is not a post hoc change: there are no data yet.

**1. A guard against noise.** "Benefit suggested" now needs, besides N >= 20, Net >= +3 and no false trigger, that **acted-net >= +3**, where *acted-net* = (the pairs OFF `fail` -> ON `pass` in which the ON row acted) minus (the pairs OFF `pass` -> ON `fail` in which the ON row acted). "The ON row acted" = it has at least one replan entry of any kind (`retry_kb`, `add_support`, `offer_ticket`, `escalate`, or a `*_failed`). A pair in which the step did not act is the same system in both rows: whatever differs there is noise. So:

- Net >= +3 **and** acted-net >= +3 **and** N >= 20 **and** no false trigger -> **benefit suggested**.
- Net >= +3 but acted-net < +3 -> **noise** (the global difference comes mostly from pairs where the step did not act); it is reported as a null result and says so.
- Net <= -3 with N >= 20 -> **harm suggested** (not guarded, as nobody asked for it: the report prints the acted-net beside it, and says when the harm does not come from pairs where the step acted).
- Everything else -> **null**, or **inconclusive** if N < 20.

**Noise baseline, always reported:** the discordance of OFF 1 against OFF 2 (the same case, the same configuration): among the cases that have a `pass`/`fail` verdict in both, how many get a different one. **An exact sign test**, two-sided, on the discordant pairs in which ON acted (OFF `fail` -> ON `pass` against OFF `pass` -> ON `fail`, each side equally likely under the null): reported as a number, **descriptive, with no threshold**; with no discordant acted pair it says "not applicable".

**2. Reports by branch.** For B1, B2, B3 and the controls, and for the **subtotal B1 + B2**: valid pairs, ON passes, OFF passes, Net, discordant pairs and pairs where ON acted. These are preregistered as descriptive: the decision rule is the one above, on all the valid pairs. **B3 favors ON by construction**: its criterion rewards a concrete way forward and the ticket offer of the step supplies one. **The B1 + B2 subtotal is the reading without that bias**, and any sentence that quotes Net quotes that subtotal next to it.

**3. Execution.**
- The no-judge firing run of 4d is **only diagnostic** (it shows whether each branch acts at all) and is **not reused as the ON arm**: its files are the input of no report.
- In 4e the four runs go **interleaved, OFF 1, ON 1, OFF 2, ON 2, on the same day** (`c02` depends on "yesterday", which the fake data resolves from the day of the run), each with its own empty folders for tickets and handoffs.
- If the cases are amended after 4d (the one revision the page allows), the amendment has its own commit and 4e runs after it.
- **Preflight before any judge token** (`eval/probe_preflight.py`): the dry-run of `rejudge` must count **48 rows to judge**, and every one of those rows must find its case (and its criterion) in the dataset that `EVAL_DATASET` names. `rejudge` skips a row whose id it does not find without stopping, so a count of 48 alone would not show a missing variable. If the count is not 48 or an id is not found, the preflight stops and nothing is judged.

**4. Infrastructure for 4d, with tests** (all in `eval/`; no change to `eval/judge.py` or `eval/rejudge.py`): `EVAL_DATASET` and `run_eval --dataset` (unset, `load_cases` returns exactly what it returns today); the preflight; the paired section of `eval/compare_runs.py` (`--paired`, `--groups eval/probe/replan_probe_groups.json`) with the guard, the by-branch tables, the noise baseline and the sign test, generated.

### Amendment 1 (after the firing run of 4d, before the dry-run and before any judge token; commit of its own)

**Decision of the owner: the cases are NOT revised.** The firing rule of this page was checked against the design branches and was not met by any of them (the record of the firing run above). The one revision it allows is not used, for two reasons:

- (a) `retry_kb` acted in 10 of the 24 ON rows of the firing run, in 5 different cases, whatever branch the case was designed for. That is enough to measure the mechanism: there are rows where the step acts, and the probe can compare them with the OFF rows.
- (b) B2 and B3 could only be made to act with messages written to mislead the router (to leave the support agent out, or to leave a fix with steps and no backing). That breaks the rule of this page that no message is forced.

**Recorded as a finding:** the two replan-2 safety nets, `add_support` (B2) and the ticket offer and escalation (B3), **did not act in the 24 ON rows of the probe**, and on the 27 regular cases `add_support` acted only in `q04`. The probe, as designed from the conditions of `observe.py` with natural questions, does not reach them.

**What changes in the report (not in the rule).** Besides the groups by design branch and the B1 + B2 subtotal, which stay as preregistered, the paired section adds the groups by **what the ON row did in the real run (4e)**, not what it did in 4d: **ON acted** (a non-control pair whose ON row has at least one replan entry), **ON did not act** (a non-control pair whose ON row has none) and **controls** (the 6 pairs of `c01` to `c03`). With what 4d observed, **the main reading is the `ON acted` line**: it is the only place the step can make a difference, and the pairs where it did not act are the same system in both rows. The report says so in its own text. B3 still favors ON by construction, and the design-branch tables stay in the report, as preregistered.

**Unchanged:** the decision rule, the noise guard (acted-net), the number of repetitions (2 per arm, 48 rows) and the order of execution (OFF 1, ON 1, OFF 2, ON 2, interleaved, on the same day, each with its own folders for tickets and handoffs, agents only until the preflight and the owner's OK).

## Record of the judged run (4e) and decisions after it

A record, not an amendment: no rule changed. The four agent runs (OFF 1, ON 1, OFF 2, ON 2) were made interleaved on 2026-11-18 on the clean commit `4192349`, each with its own empty folders for tickets and handoffs, with no judge. After the preflight (48 rows to judge, each with its case and criterion) and the owner's go-ahead, one `rejudge` process scored them in that order (`--daily-limit 391000`, with `EVAL_DATASET` set); it finished with every row scored and none skipped. The report is `eval/reports/REPLAN-PROBE-20261118-133249.md`, generated by `python -m eval.compare_runs --paired --groups eval/probe/replan_probe_groups.json`; its headline is the one the rule above gives, and it is quoted, with the rest of the descriptive lines, in `docs/REPLAN_DEMO.md`.

- **The owner's instruction about the false trigger:** the rule does not change. A control (`c03`) made the step act in the ON arm, so the outcome cannot be "benefit suggested"; the outcome that comes out is reported.
- **A display added after the numbers were seen:** the section of the report that lists the rows where the ON row acted, one by one (verdicts, scores, verifier notes, cost and latency against the OFF mean). It changes no count and not the rule.
- **Phase 5 is skipped, by decision of the owner (2026-11-18).** Two more repetitions with the step on over the 27 regular cases were not run. The comparison on the regular cases stays as it is in `eval/reports/REPLAN-AB-20261116-234030.md`, and the probe adds nothing to it and replaces nothing in it.
