"""The 15 rows a human labels: stratified, fixed seed, chosen from the dataset alone (never from a judge note or a key)."""
import inspect
import json

from eval.calibration import sample as sample_mod
from eval.calibration.sample import QUOTAS, SEED, check, choose, load_cases, strata


def test_the_sample_has_fifteen_rows_in_the_four_strata_with_the_two_required_holdout_cases():
    s = choose(load_cases())
    assert s["total"] == 15 == len(s["ids"]) == len(set(s["ids"])) and s["seed"] == SEED
    assert {n: len(v["chosen"]) for n, v in s["strata"].items()} == dict(QUOTAS)
    blind = s["strata"]["blind_holdout"]["chosen"]
    assert blind[0].startswith("ho06_") and blind[1].startswith("ho12_") and all(i.startswith("ho") for i in blind)
    cases = {c["id"]: c for c in load_cases()}
    assert all(cases[i].get("expected_tools") and not cases[i].get("history") and not cases[i].get("holdout") for i in s["strata"]["regular_with_tools"]["chosen"])
    assert all(cases[i].get("history") and not cases[i].get("holdout") for i in s["strata"]["regular_multi_turn"]["chosen"])
    assert all(not cases[i].get("expected_tools") and not cases[i].get("history") for i in s["strata"]["regular_other"]["chosen"])


def test_it_is_reproducible_and_the_seed_is_what_decides():
    assert choose(load_cases()) == choose(load_cases()) and choose(load_cases(), seed=1)["ids"] != choose(load_cases())["ids"]


def test_the_sample_is_a_function_of_the_dataset_only_it_cannot_see_a_judge_note_or_a_key():
    src = inspect.getsource(sample_mod)
    code = src.split('"""', 2)[2]  # after the module docstring (which says what it does NOT read)
    for forbidden in ("verdict", "judge_reason", "labels_template", "labels_v2_key", "key.csv", "\"judge\""):
        assert forbidden not in code
    assert strata(load_cases()) == strata(list(reversed(load_cases())))  # the order of the file does not matter either


def test_the_replacement_for_a_missing_row_is_decided_in_advance_from_the_same_shuffle(tmp_path):
    s = choose(load_cases())
    gone = s["strata"]["regular_with_tools"]["chosen"][0]
    run = tmp_path / "r1.json"
    run.write_text(json.dumps({"rows": [{"id": i, "answer": "a"} for i in {c["id"] for c in load_cases()} if i != gone]
                               + [{"id": gone, "provider_error": "429"}]}), encoding="utf-8")
    res = check(s, str(run))
    assert res["missing"] == [gone]
    nxt = s["strata"]["regular_with_tools"]["reserve_in_order"][0]  # the next one in the SAME shuffle
    assert nxt in res["replacement"] and gone not in res["replacement"] and len(res["replacement"]) == 15
    rest = [i for i in s["ids"] if i != gone]
    assert all(i in res["replacement"] for i in rest)  # nothing else moves
