# V3b: a second human check of the judge, preregistered

This page was written, and committed, **before the 15 rows were drawn**. The commit that holds it holds the sampler (`eval/calibration/sample_v3b.py`) and its tests, and holds no manifest and no sheet: the draw came in the next commit.

## Why

V3 measured the agreement between the judge and a human on 15 answers. With 15 rows, one disagreement moves the agreement by 7 points and the confidence interval is wide. V3b adds 15 answers of 15 **different cases**, so that V3 and V3b together are 30 rows of 30 cases. It does not change the judge, `JUDGE_PROMPT`, `eval/judge.py` or any score.

## The rule (the module docstring of `eval/calibration/sample_v3b.py` says the same, and a test pins it)

- **Source of the answers:** repetition 2 of the final run, `eval/reports/20261115-202732.judged.json` (commit `22423e2`, `REPLAN_MAX=0`). V3 used repetition 1. Every row of that file has a verdict of the judge (40 rows, none a judge error), so the replan setting is the same for all 15 rows and is not a variable.
- **Excluded:** the 15 cases of V3 (`eval/calibration/sample_v3.json`), in any repetition.
- **Strata:** the four of V3 (`eval.calibration.sample.strata`): blind holdout (`ho06` and later), regular with tools, regular multi-turn, regular other. The cases `ho01` to `ho05` were seen during development and are in no stratum, as in V3.
- **Quotas:** 3 blind holdout, 4 regular with tools, 4 regular multi-turn, 4 regular other. What V3 left in the strata was 4, 6, 5 and 5 cases; the quotas are their 15/20 share by the largest remainder. **No case is forced in** (V3 put `ho06` and `ho12` in on purpose; V3b does not).
- **Seed:** `20261118`, the date of the decision. In each stratum the candidates are sorted, the V3 cases removed, and shuffled with `random.Random(f"20261118:{stratum}")`; the first `k` with a usable row are taken. The rest of the shuffled order is the reserve: if a chosen case has no usable row, the replacement is the next one in that order.
- **Usable row:** it exists and its verdict is `pass` or `fail`. **Which of the two it is never enters the choice.** The draw is a function of the dataset's structure (the kind of each case), of V3's ids and of which rows exist. It reads no verdict, score, note or key, and a test checks the source of the functions that choose.

## What the human sees, and does not

The blind sheet (`eval/calibration/labels_v3b_blind.csv`) has one row per answer with: an opaque id (`v3b-01` to `v3b-15`, in a seeded shuffle, so the order does not group the strata), the question (with the earlier turns of a multi-turn case), the case's expected behavior (the `criteria` field: the judge was given it and it takes precedence in the judge's prompt), the evidence and the answer, and the empty columns `human_grounded` (1-5), `human_complete` (1-5), `human_pass` (yes/no) and `notes`. **It has no verdict, no score, no reason of the judge, no case id, no repetition, no replan setting and no holdout flag**, so the human sees what the judge was given (question, evidence, answer and the case's expected behavior) and nothing else. A test checks that none of those strings is in the file.

The key (`eval/calibration/labels_v3b_key.csv`) has the same opaque ids, the real case id, the source file and the judge's columns. It was written by the script and **not opened by the person who prepared the sheet**; its SHA-256 is in the manifest (`key_sha256` in `eval/calibration/sample_v3b.json`), so a later edit of it shows. It is opened only after the owner returns the labels.

**How to label (the same columns and scales as V3).** The text of V3's instructions is not in the repository, so this is the judge's own rubric, copied in meaning from `JUDGE_PROMPT`: list the claims of the answer that the evidence does not support (a step, a menu path, a figure, a fee, a date, a deadline, a capability, an eligibility condition; one stated as a general rule when the evidence ties it to a case is unsupported). `human_grounded`: no unsupported claim 5, one minor 4 at most, any claim that the customer would act on 3 at most, no evidence and specific factual claims 2 at most (unless the expected behavior says no tools are needed). `human_complete`: does the answer give the customer what they need to act. `human_pass`: yes when the answer is relevant, grounded at 4 or more, complete at 3 or more and in the language of the customer's last message.

## What will be computed, and said whatever it shows

When the owner returns the labels: the pass/fail agreement, Cohen's kappa, the 2 x 2 table and the mean absolute error of groundedness and completeness (`eval/calibration/agreement.py`), **for the 15 new rows alone and for the 30 rows of V3 and V3b together**, each with its n, plus the **Wilson 95% interval** of the agreement proportion for each of the two. The kappa is given as a point value with no interval: with 15 or 30 rows it is too unstable to read as more than a direction. The result goes in `eval/calibration/CALIBRATION_V3b.md` and in a generated block of the final report; the number of rows is stated next to every figure. A result lower than V3's is reported as it is.

## Declarations

- **The rows already had a verdict of the judge** (all 15 come from a judged run). The human does not see it. V3b therefore measures the agreement of the human with a verdict that already exists; it is not a measurement made before the judge ran.
- **The 15 rows are a seeded draw of one repetition of one run**, over 25 of the 40 cases. They are not a random sample of the 40 cases, and not a random sample of the answers the system gives.
- **Who prepared and who labels.** The sampler, the sheet and the key were prepared by Claude Code under the owner's direction. The labels are the owner's.
- The judge is not changed by this, and no number about the judge is typed on this page.
