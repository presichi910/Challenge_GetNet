# V2 rows re-scored with the current judge

_V2 is the sheet the judge was ADJUSTED on: its disagreements led to the rules now in the code. The agreement of the current judge with the V2 human labels is therefore NOT independent of the judge's design. The independent measure is V3 (new answers; the sample was fixed before any judge note existed)._

- Rows: **19 of the 27 V2 rows**, chosen with seed 20261116 from the ids alone (`random.Random(20261116).sample`). Scored by the current judge so far: **19 of 19**.

## Pass rate on the same rows

| who | rows | pass |
|---|---|---|
| old judge (the one V2 was measured with) | 19 | 8/19 = 42% |
| **current judge** | 19 | **8/19 = 42%** |
| human (V2 labels) | 19 | 9/19 = 47% |

_For the record, on all 19 selected rows (not only the 19 re-scored): old judge 8/19 = 42%, human 9/19 = 47%._

## Agreement with the human on those rows (pass/fail)

| judge | agrees with the human | Cohen's kappa |
|---|---|---|
| old | 12/19 = 63% | 0.26 |
| current | 16/19 = 84% | 0.68 |

_n = 19: a difference of one row is 5 points. Read the direction, not the decimals._

## About the draw

The 19 were drawn from the ids alone, with a seed fixed beforehand. By chance (about 6 draws in 100) the draw contains EVERY row on which the old judge and the human disagreed in V2: the old judge agreed with the human on **12/19 = 63%** of the selected rows and on **8/8 = 100%** of the 8 left out. So the old judge starts from a lower agreement on these 19 rows than on all 27, and the comparison below favors a change of judge more than a random draw would. The seed was not changed after seeing this: that would be choosing the rows by their results.

## Rows where the verdict changed with the judge

q09_installments (fail -> pass), q10_payment_link_whatsapp (fail -> pass), x04_cross_customer_attempt (pass -> fail), m02_recall_which_bigger (pass -> fail)
