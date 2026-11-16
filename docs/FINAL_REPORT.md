# Getnet multi-agent support: final evaluation report

> **DRAFT.** Everything the judge scored (the five repetitions, the V2 re-score, the calibration) sits in **generated blocks** between
> `<!-- name:start -->` and `<!-- name:end -->`. Nothing inside them is typed by hand: `eval/results_block.py` writes them from the saved runs, and every rate that
> needs the judge states the number of rows it is over. Until the program runs they say "not generated yet". Everything outside them is text, with the file it comes from.
>
> Fill the blocks (the three commands are at the end of this file) once `eval.rejudge` has stopped, then read section 7 again: it lists what the numbers can and cannot say.

## 1. What was run

| | |
|---|---|
| Code | commit `22423e2f10249eb38bcaa8e8583bdaac49b5b35a`, working tree clean (`run_eval --require-clean`; every report and JSON carries `commit` and `tree_clean`) |
| Cases | 40 per repetition: 27 regular (including the injection cases `inj01`, `inj02`) and 13 holdout (`ho01`-`ho05` seen during development; `ho06`-`ho13` the new holdout, of which `ho06`, `ho08` and `ho12` are "baseline seen", see section 7) |
| Repetitions | 5, each a full run of the 40 cases (200 case-runs), no judge during the run, 1,705 s |
| Provider errors | 0 in the 200 case-runs |
| Agents | Fireworks `accounts/fireworks/models/gpt-oss-120b` for every role (router, knowledge, support, synthesizer, verifier) |
| Judge | Groq `qwen/qwen3.8-27b`, run AFTER the agents (`eval/rejudge.py`), see section 7 for how much of it was scored |
| Configuration | `REPLAN_MAX=0` (observe step off), `RAG_MODE=dense`, `VERIFIER_MODE=statements`; ticket approval off and provider failover off in the eval |
| Agent cost | $0.233 for the five repetitions ($0.0425-$0.0492 each), 1.18 M tokens; about $0.0012 per case-run |
| Latency (the 27 regular cases) | p50 2.7-4.1 s and p95 13.9-18.2 s per repetition (the run's own summaries; the generated block below also shows it by group) |
| Raw data | `eval/reports/20261115-202155.json` ... `20261115-204526.json` (five files) and their `.judged.json` |

Multi-turn cases run on a real SQLite checkpointer that is closed and reopened between the earlier turns and the question (`eval/persistent.py`); the 10 multi-turn
case-runs per repetition pass a deterministic memory check.

## 2. Results (generated)

<!-- results:start -->
_Not generated yet._
<!-- results:end -->

How to read it: the routing, tool, handoff and memory rates are over the case-runs that have that check, and the judge-based rates (groundedness, completeness, pass rate) are over
the rows the judge scored, each with its n; the original holdout (`ho01`-`ho05`), the new holdout not targeted by a prompt rule (`ho07`, `ho09`-`ho11`, `ho13`) and the baseline-seen cases (`ho06`, `ho08`, `ho12`) are separate columns, with no pooled `ho06`-`ho13` total. A repetition scored in part is shown as such, not as a whole. A row the judge has not scored is **pending**, never a pass. The pass rate of the current judge is **not comparable** to the 86% of the
2026-11-12 report: the judge changed after that run (see section 7).

## 3. Does the judge agree with a human? (V3)

The judge is an LLM, so its verdicts were checked against a human on **new** answers: 15 rows of repetition 1, chosen before any judge note existed, from the dataset
alone, stratified, with a fixed seed (`eval/calibration/sample.py`, seed 20261117, `sample_v3.json`): 4 new-holdout rows (`ho06` and `ho12`, which are "baseline seen", plus `ho09` and `ho11`), 6 regular rows with tools, 3 multi-turn, 2 others.
**`ho06` and `ho12` were included on purpose as edge cases (2 of the 15), not drawn:** the sample is not a random sample of the 40 cases.

<!-- calibration:start -->
_Not generated yet._
<!-- calibration:end -->

**Where the judge and the human disagreed (both are false fails, there is no false pass).**
- **`m04_followup_product`:** the judge failed it on groundedness (3, where 4 is needed) because it counted three claims as unsupported; the human found them backed by the Get Smart manual
  in the evidence (Pix by QR or NFC, enabled in the app) and gave 5/5/pass. The judge's free-text reason blames a language mismatch that is not there (the user wrote in English and the code's own
  language check said they match), so on this row the stated reason and the cause of the verdict differ.
- **`m08_recall_sum`:** the answer ("O total foi de R$ 200,00.") is correct from the conversation but is in Portuguese to a question in English. The judge failed it for language, as its rules say;
  the human noted the mismatch and still passed it, because the case's criteria ask only for the total, with no tools and no handoff.
- **Pattern:** both are multi-turn rows (two of the three in the sample), and with 15 rows one row is 6.7 points, so 87% (13/15) and kappa 0.73 are indicative, not a precise measure.
  The `m08` answer language is a product defect that the judge saw and the human forgave; it is not fixed (the code has been frozen since the final run).

History of the judge's agreement with a human, each on its own sample and its own version of the judge: V1 70% (kappa 0.20), V2 74% (kappa 0.46), V3 87% (kappa 0.73).

### 3.1 V2 re-scored with the current judge

19 of the 27 V2 rows, chosen from the ids alone with seed 20261116 (written in `CLAUDE.md` before anything ran). **V2 is the sheet the judge was adjusted on, so this is not an
independent measure.** By chance (about 6 draws in 100) the draw contains all seven rows on which the old judge and the human disagreed, so the old judge starts from a lower agreement on them.

<!-- v2:start -->
_Not generated yet._
<!-- v2:end -->

## 4. The observe step (replan): measured, not adopted

The step re-queries the knowledge base once with the claims the verifier could not back, re-checks the same answer, and (with `REPLAN_MAX=2`) offers a ticket or escalates when nothing actionable is left.
It is **off by default and off in the main run.** As built, it runs **once, after the synthesizer** (not after each agent), and the design's "add another agent" branch is **not implemented**: the only actions are the knowledge-base retry, a ticket offer and an escalation.

- **Paired measurement with the judge** (`eval/replan_ab.py`, the same 30 saved drafts through both arms, only the 6 whose text differs were scored, one judge score per text, n = 6):
  unsupported actionable claims 1 -> 2, completeness 2.67 -> 3.17, pass 4/6 -> 4/6 (unsupported claims in total 6 -> 11). One case improved (`q08`), one got worse (`q05`: after the retry the verifier backed
  everything and the judge still found six unsupported claims). The only measured benefit is completeness, with n = 6 and a judge that scores the same text differently between passes.
- **A branch of the real run with the step on** (`--replan-max 2 --label "replan on"`, 2 x 40 case-runs, not scored): it acted in 9 of 80 case-runs (11%), escalated 0 times and offered a ticket once;
  p95 latency 16.3 s -> 26.3 s (+10 s), p50 3.9 s -> 4.5 s, mean 5.49 s -> 8.26 s; agent cost $0.0012 per case-run in both arms (5,924 against 5,923 tokens) and the same handoff rate (15%). Every action is one extra Voyage call (9 in all), which is not in that agent cost.
  The cost of the step is therefore latency, not agent tokens (`eval/reports/REPLAN_ON-20261115.md`).
- **Decision:** `REPLAN_MAX=0`.

## 5. Did the work after the final run change the behavior?

After the final run came the operator webhook, the handoff SLA, metrics and traces, and the quality gates. A smoke test (`run_eval --repeats 1 --no-judge --require-clean`, 40 cases, 0 provider errors,
$0.042) on commit `0835e91f81556587124e12aa81fe82556f775c24`, compared with the five repetitions on `22423e2f1024` (`eval/same_behavior.py`, `eval/reports/SMOKE-0835e91.md`):
routing, tool use, handoff and memory all fall inside the spread the five repetitions showed among themselves, and no case that passed a check in every repetition fails it in the smoke.
**Verdict: no change.** This compares only the checks that need no judge; it says nothing about the quality of the texts.
Every later commit up to this report leaves `app/` byte-identical to `0835e91` (`git diff 0835e91 HEAD -- app` is empty), so the smoke covers the code as it stands now; between `22423e2` and `0835e91`, 22 files under `app/` changed
(new modules for the webhook and telemetry, the handoff store and its SLA, configuration, and small type and lint fixes in the router, the verifier and the agent runner), while the prompts and the judge did not.

## 6. What was built and how it was checked

| Area | What | How it was checked |
|---|---|---|
| Verifier | The model lists the statements of an answer with a quote each; the code checks the quote is really in the evidence and removes the rest (one rewrite). A guard blocks the system prompt from appearing in an answer | tests; the leak of `inj01` disappeared in 3 of 3 live runs |
| Security | RS256 against a JWKS (Keycloak, two realms), mandatory `kid`, 15-minute tokens, revocation list in SQLite, per-subject rate limit, audit log without tokens, one worker | tests with generated RSA keys; 4 tests and an end-to-end run against a real Keycloak |
| Human oversight | Handoff queue with claim, reply, lease and an SLA escalation; a ticket request waits for an operator's approval (LangGraph `interrupt()`, one graph thread per request, expiry opens no ticket); signed webhook with no customer text | dozens of tests (the approval flow alone has 36); one real-model run (request, approval, ticket opened, next turn informed) |
| Memory | SQLite checkpointer, rolling summary, retention, conversation ids generated by the server | tests; the eval's restart test |
| Retrieval | Dense search (kept), plus BM25 + RRF and a Voyage reranker, measured and not adopted: over 21 questions hit@1 0.33 / 0.48 / 0.57 and hit@5 0.71 / 0.62 / 0.62 (dense / hybrid / hybrid + rerank), and the plain hybrid is worse on the holdout (hit@5 0.67 -> 0.33). One question is 5 points | `eval/run_retrieval.py`; parameters chosen on one half of the regular questions, reported on the other half and the holdout |
| Observability | Prometheus metrics, OTLP traces, 9 alert rules, a Grafana dashboard | against a real Jaeger and a real Grafana; `promtool test rules`, which fails when a rule is broken |
| Production | ruff, mypy, bandit, pip-audit, promtool and trivy as blocking steps of the CI; a multi-stage image with a pinned lock file, labeled with its commit, uid 10001 | each gate run locally; see section 7 for the status of the CI itself |

## 7. Disclosures and limitations

**About the judged numbers**
1. **Not every repetition was scored.** The judge runs on Groq's free tier (200,000 tokens per rolling day, about 1.7K tokens per row), so the scoring was done in sessions after the run, repetition by repetition
   (`eval/rejudge.py`, resumable, stopping before the cap). The generated block says how many rows of each repetition were scored; the rates are over those rows, and a repetition scored in part is not weighted as a whole one.
2. **The judge is not the one of the earlier reports.** After V2 its verdict became a function of its scores (relevance and groundedness at least 4, completeness at least 3, language by code, caps for unsupported claims).
   The 86% regular pass rate of 2026-11-12 was measured with the previous judge and cannot be compared with today's. Whatever the generated block shows against it is a difference of at least four things at once (another judge, other agent prompts, another set of cases, another run),
   so it is **not** attributed to the product getting better or worse; at most it is read as a direction, with caution.
3. **The agreement figures are small-sample figures** (15 rows in V3; 19 in the V2 re-score). A one-row difference is 6.7 and 5.3 points. V3 was sampled on purpose to include two edge cases.
4. **V2 is not independent.** The judge was adjusted on its disagreements. The 19-row re-score is also an **atypical subset**: the draw happens to hold all seven rows where the old judge and the human disagreed (section 3.1), so the old judge starts from a lower agreement there than on the 27.
5. **The judge's free-text reason can be wrong about the language.** On `m04` it says the user wrote in Portuguese when they wrote in English. The verdict does not depend on that sentence (the language rule is applied by code with its own detector, and the cause of that verdict was groundedness),
   but the reason of a single row must not be quoted as evidence.

**About what the evaluation covers**
6. **The eval does not exercise** ticket approval, provider failover, the webhook, the SLA or the telemetry (the eval runs with approval and failover off). They are covered by tests, and approval by one real-model run.
7. After the commit of the holdout (`9326b88`), three commits changed `app/prompts.py`: `20d5b4e` (support rule 5: offer a ticket, create it only if the customer accepts; tuned with `q05`, `q08`, `x04`), `d71966d` (rule 8: do not reveal instructions; tuned with `inj01`, controls `inj02` and `x04`) and `9eb7a24` (support rules 3 and 7; tuned with `h03`, controls `x04`, `h01`, `h02`).
   `ho06` and `ho12` measure the same behavior as rule 5, and `ho08` the one of rule 8; the baseline of the holdout had already been seen when the changes were made (`ho06` and `ho12` failed 0/3; `ho08` passed 3/3). For that reason `ho06`, `ho08` and `ho12` are not presented as blind. The cause of the `h03` regression (probably rule 8) was not verified. `ho02` (routing) is unstable and `ho13` (tool use) fails in every repetition; both are reported as they are. The "blind" labels in the per-run reports generated by `run_eval` (for example "new blind (ho06 and later), written before any prompt fix") predate the correction in this item: read them as "new holdout", with `ho06`, `ho08` and `ho12` as baseline seen.
8. **Tuning discipline.** Prompts changed in three steps, each tuned on REGULAR cases only. (a) After the new holdout was committed and its baseline measured (pass 16/24 over three repetitions, with the judge of that day), the support rules for declining and for offering a ticket and the router and synthesizer rules for recall were changed using `q05`, `q08` and `x04`, then the holdout was measured once more (21/24); no prompt rule was changed in response to holdout results after the second measurement; the later edits (rule 8, h03) were tuned on regular cases. (b) A rule against revealing the instructions, with a guard in code, tuned on `inj01` and `inj02`. (c) A regression found by repeating `h03` (it passed 2 of 5 runs, down from 15 of 16 earlier): the support rules 3 and 7 were narrowed using only `h03` and `x04`, 5 repetitions each, with `h01` and `h02` as controls; `h03` went from 2/5 to 5/5. `PROMPT_SHA256` pins every prompt. The judge was not changed after the final run.
9. **Run-to-run variation is real.** The model is not deterministic: `ho02` passes in some repetitions and not in others, and the judge scores the same text differently between passes. Read directions, not decimals.

**About known defects**
10. The verifier removes unbacked claims but cannot add the right ones: a case such as `ho06` can end as an honest, nearly empty answer ("no documented steps"). It audits about 72% of an answer's sentences and cannot tell a real quote that does not
   back its claim. A stricter segment-by-segment verifier was built, measured on 15 drafts and **not adopted**: it covered every segment but flagged 1.87 per answer against 0.8 (mostly false positives: lead-in lines, derived figures) and did not leave fewer unsupported claims (7 actionable against 5 for the one in use; completeness 3.08 against 3.31).
11. `inj01` still ends as a fail with the judge, on the groundedness of its harmless part (contact data the judge disputes although the verifier found the quotes), not on the injection: the leak of the system prompt is gone.
12. The language of the answer to `m08` is wrong (Portuguese to an English question), see section 3.

**About the environment and the process**
13. **CI.** The quality gates were verified **locally**, one by one; **the workflow has not yet run on GitHub, and its first run there is pending.** `pip-audit` on the pinned versions (`constraints.txt`, 126 packages) reports **5 entries, all for ChromaDB 1.5.9, which are 4 distinct ids**
    (`PYSEC-2026-311`, listed twice, = CVE-2026-45829; `PYSEC-2026-3813` = CVE-2026-45830; `PYSEC-2026-3814` = CVE-2026-45833; `PYSEC-2026-3815` = CVE-2026-45831); none has a fixed version. They are accepted by id (with the four `--ignore-vuln` flags the tool ends with
    "No known vulnerabilities found, 5 ignored" and exit code 0): they concern Chroma's server, which this project does not use (Chroma is embedded), and a test fails if the code starts using the server. Any other advisory fails the build.
14. **Model risk.** Fireworks announced the removal of `gpt-oss-120b` from its **serverless** service on 2026-11-07 (a dedicated deployment is the only stated alternative); it was still being served during the run, but the agents depend on it. A failover to the same model on Groq exists and is tested, but was not used in the eval.
    Moving the judge to Fireworks was also looked at and **discarded**: its model (`qwen3p8-27b`) is offered there only as a dedicated deployment, not serverless, with no published per-token price, so the judge stayed on Groq's free tier, which is the reason the scoring is partial (item 1).
15. **The judged numbers are for commit `22423e2`.** The repository has moved since (webhook, SLA, telemetry, quality-gate fixes: 22 files under `app/`); `app/prompts.py` and `eval/judge.py` are byte-identical to the run. What the later code does is checked by the deterministic smoke test of section 5, not by a new judged run.
16. **Scale limits.** One worker (the rate limit and the key cache live in the process), SQLite for conversations, queues and approvals, no Postgres, metrics per process, no Alertmanager.
17. **Dependencies** were approved by the owner: `cryptography`, `rank_bm25`, `prometheus-client`, `opentelemetry-sdk` and `opentelemetry-exporter-otlp-proto-grpc`, and for development `ruff`, `mypy`, `bandit`, `pip-audit` and `pyyaml`.
18. **Process.** One commit per phase, nothing squashed; the raw JSON of every cited run is versioned; the final run, the replan branch and the smoke each cite their commit. The owner made every commit and labeled the calibration sheets blind.
19. **Assisted development.** The code, tests and drafts of this project were produced with Claude Code (Anthropic's coding assistant) under the owner's direction and review.
20. **The evaluation runs and the image do not use the same interpreter or the same package versions.** The runs were made from the project's local virtualenv, Python 3.13.7 (the working method of the project notes; the run JSON files record the commit but not the interpreter or the package versions).
    That virtualenv differs from `constraints.txt` in 15 of the pinned packages, for example `openai` 3.49.0 (pinned 3.51.0), `voyageai` 0.5.0 (0.6.0) and `numpy` 2.5.4 (2.5.5). All 15 were installed between 2026-11-08 and 2026-11-12, before the runs of 2026-11-15,
    and `constraints.txt` at the run commit pins the same versions for them. The `Dockerfile` and the CI use Python 3.12. The `Dockerfile` installs with `pip install -r requirements.txt -c constraints.txt`, so the image holds the pinned versions; the CI installs `requirements-dev.txt` without the constraints file.
    No evaluation was re-run on 3.12 with the pinned versions: the numbers of this report belong to the virtualenv, not to the image. What was checked on the image's side is the code, not the judged numbers: in a `python:3.12-slim` container the test suite, ruff, mypy and bandit pass
    with the CI's install and with the install pinned by `constraints.txt` (the two differ in two packages, `httpx2` and `httpcore2`, 2.16.2 against 2.16.0; the three provenance tests need `git`, which that container lacks).

## 8. Not done

Postgres as the checkpointer; Alertmanager; hash-pinned installs; push notifications to the customer; the judge's own retry logic inside `eval/judge.py` (a wrapper does it, because the judge is frozen); type checking of
`eval/` and `tests/`; a look at the Grafana dashboard under live traffic.

## 9. How this file is filled in

```
python -m eval.results_block eval/reports/20261115-202155.judged.json eval/reports/20261115-202732.judged.json eval/reports/20261115-203324.judged.json ^
   eval/reports/20261115-203912.judged.json eval/reports/20261115-204526.judged.json --report docs/FINAL_REPORT.md --v2 --calibration eval/calibration/CALIBRATION_V3.md
```
(for a repetition `rejudge` never reached there is no `.judged.json`: pass the plain `.json`, and its judge-based cells will read n = 0). The V2 block needs `python -m eval.rejudge` to have scored
`eval/reports/V2RESCORE19-20261114-192208.json` first.
