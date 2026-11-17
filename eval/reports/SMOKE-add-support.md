# Did the later work change the behavior? (smoke test, no judge)

- **Final run** (`20261115-202155.json, 20261115-202732.json, 20261115-203324.json, 20261115-203912.json, 20261115-204526.json`): commit `22423e2f1024`, working tree clean: [True, True, True, True, True]
- **Smoke run** (`20261116-232753.json`): commit `3e7ad4302f66`, working tree clean: True

_Unchanged = the smoke's rate is inside the spread of the final run's own repetitions, and no case that passed a check in every repetition of the final run fails it here._

| check | final min | final max | final mean | smoke (n) | inside the spread |
|---|---|---|---|---|---|
| routing | 98% | 100% | 98% | 100% (40) | yes |
| tools | 98% | 98% | 98% | 100% (40) | **NO** |
| handoff | 100% | 100% | 100% | 100% (38) | yes |
| memory | 100% | 100% | 100% | 100% (10) | yes |

**Suspects (passed in every final repetition, fail in the smoke):** none
**Improved (failed in every final repetition, pass in the smoke):** ho13_printer_and_ticket_pt (tools)

## Verdict: CHANGED, look at the rows above

## Identity with REPLAN_MAX=0 (the observe step does nothing, and no agent is added)

- **(a) no replans in any row: PASS** (40 rows; the final repetitions had 0 rows with replans)
- **(b) agents of each case among the sets seen in the final repetitions: PASS** (40 cases compared)

**Identity verdict: IDENTICAL: the observe step took no action in any row and no case used an agent set the final run had not shown**
