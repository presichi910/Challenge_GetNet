"""The replan probe's cases (eval/probe/replan_probe.jsonl, preregistered in docs/REPLAN_PROBE_PREREG.md): the schema of the regular cases, three cases for each of
three branches and three controls, read-only messages, and criteria that say what the merchant needs and never name a mechanism."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROBE = ROOT / "eval" / "probe" / "replan_probe.jsonl"
CASES = [json.loads(line) for line in PROBE.read_text(encoding="utf-8").splitlines() if line.strip()]
DATASET = [json.loads(line) for line in (ROOT / "eval" / "dataset.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
BRANCH = {"p01": "B1 retry_kb", "p02": "B1 retry_kb", "p03": "B1 retry_kb", "p04": "B2 add_support", "p05": "B2 add_support", "p06": "B2 add_support",
          "p07": "B3 ticket_or_escalate", "p08": "B3 ticket_or_escalate", "p09": "B3 ticket_or_escalate", "c01": "control", "c02": "control", "c03": "control"}
MECHANISM = re.compile(r"add_support|replan|retry|re-?try|observe|escalat|knowledge base|knowledge agent|support agent|router|verif|\btools?\b|second search|search again|REPLAN", re.I)
WRITE_OR_HUMAN = re.compile(r"ticket|human|operator|escalat|atendente|chamado|falar com|talk to|open a|create a|cancel|refund|estorn", re.I)


def test_it_has_twelve_cases_three_for_each_of_three_branches_and_three_controls():
    ids = [c["id"] for c in CASES]
    assert len(ids) == 12 == len(set(ids))
    assert {i[:3]: BRANCH[i[:3]] for i in ids} == BRANCH
    from collections import Counter

    assert Counter(BRANCH[i[:3]] for i in ids) == {"B1 retry_kb": 3, "B2 add_support": 3, "B3 ticket_or_escalate": 3, "control": 3}


def test_the_schema_is_the_one_of_the_regular_cases_and_the_probe_never_enters_the_holdout_or_the_dataset():
    schema = {k for c in DATASET for k in c}
    assert all(set(c) <= schema and "holdout" not in c for c in CASES)
    assert all({"id", "user_id", "message", "expected_agents", "optional_agents", "expected_tools", "expect_handoff", "criteria"} == set(c) for c in CASES)
    assert not {c["id"] for c in CASES} & {c["id"] for c in DATASET}
    assert not {c["message"].strip().lower() for c in CASES} & {c["message"].strip().lower() for c in DATASET}  # no case is a copy of a dataset message
    assert not any(c["id"].startswith("ho") for c in CASES)  # compare_runs treats an id that starts with "ho" as holdout
    customers = json.loads((ROOT / "app" / "data" / "customers.json").read_text(encoding="utf-8"))
    assert all(c["user_id"] in customers for c in CASES)


def test_the_messages_are_read_only_none_asks_for_a_ticket_a_person_or_any_write():
    for c in CASES:
        assert not WRITE_OR_HUMAN.search(c["message"]), c["id"]


def test_the_criteria_say_what_the_merchant_needs_and_never_name_a_mechanism():
    for c in CASES:
        assert c["criteria"].strip() and not MECHANISM.search(c["criteria"]), (c["id"], MECHANISM.search(c["criteria"]))


def test_the_structural_expectations_are_descriptive_the_controls_expect_no_handoff_and_the_rest_do_not_care():
    for c in CASES:
        assert c["expect_handoff"] is (False if c["id"].startswith("c") else None), c["id"]


def test_the_mix_of_languages_is_close_to_the_regular_cases_mostly_english_a_few_portuguese():
    pt = [c["id"] for c in CASES if re.search(r"\b(como|minha|faço|maquininha|eu)\b", c["message"], re.I)]
    assert pt == ["p03_antecipar_na_maquininha", "p09_reinicia_sozinha"]
    assert all("Portuguese" in c["criteria"] for c in CASES if c["id"] in pt)  # the criterion asks for the language, as a merchant would expect
