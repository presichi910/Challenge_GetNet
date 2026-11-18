"""V3b: 15 more judged rows for a human to label. The rule and the seed are fixed in code; the draw depends on the dataset's structure, on V3's ids
and on which rows exist, never on a verdict, a score, a note or a key. The sheet a human fills in carries nothing of the judge."""
import csv
import inspect

from eval.calibration import sample_v3b as mod
from eval.calibration.sample_v3b import QUOTAS, SEED, build_sheets, choose, order, sheet_order


def synthetic_cases() -> list[dict]:
    cases = [{"id": f"ho{n:02d}_x", "holdout": True} for n in range(1, 14)]  # ho01-ho05 are seen holdout, ho06-ho13 are blind
    cases += [{"id": f"q{n:02d}_t", "expected_tools": ["t"]} for n in range(1, 13)]
    cases += [{"id": f"m{n:02d}_h", "history": [{"role": "user", "content": "x"}]} for n in range(1, 9)]
    cases += [{"id": f"o{n:02d}_o"} for n in range(1, 8)]
    return cases


V3 = {"ho06_x", "ho12_x", "ho09_x", "ho11_x", "q01_t", "q02_t", "q03_t", "q04_t", "q05_t", "q06_t", "m01_h", "m04_h", "m08_h", "o01_o", "o02_o"}


def test_the_quotas_are_three_four_four_four_and_they_are_the_largest_remainder_share_of_what_v3_left():
    assert QUOTAS == [("blind_holdout", 3), ("regular_with_tools", 4), ("regular_multi_turn", 4), ("regular_other", 4)] and SEED == 20261118
    s = choose(synthetic_cases(), V3)
    assert {n: v["candidates"] for n, v in s["strata"].items()} == {"blind_holdout": 4, "regular_with_tools": 6, "regular_multi_turn": 5, "regular_other": 5}
    left = [v["candidates"] for v in s["strata"].values()]
    quotas = [k for _, k in QUOTAS]
    exact = [n * 15 / sum(left) for n in left]
    assert sum(quotas) == 15 and all(int(e) <= k <= int(e) + 1 for e, k in zip(exact, quotas))  # the floor or the floor + 1 of the exact share


def test_it_picks_fifteen_different_cases_none_of_them_in_v3_and_forces_no_case_in():
    s = choose(synthetic_cases(), V3)
    assert s["total"] == 15 == len(s["ids"]) == len(set(s["ids"]))
    assert not set(s["ids"]) & V3 and not any(i.startswith(("ho01", "ho02", "ho03", "ho04", "ho05")) for i in s["ids"])  # the seen holdout is in no stratum
    assert {n: len(v["chosen"]) for n, v in s["strata"].items()} == dict(QUOTAS)
    for v in s["strata"].values():  # chosen + reserve is exactly the candidates, in one shuffled order
        assert len(v["chosen"]) + len(v["reserve_in_order"]) == v["candidates"]


def test_it_is_reproducible_the_seed_decides_and_the_order_of_the_file_does_not():
    cases = synthetic_cases()
    assert choose(cases, V3) == choose(cases, V3) == choose(list(reversed(cases)), V3)
    assert choose(cases, V3, seed=1)["ids"] != choose(cases, V3)["ids"]
    assert order("blind_holdout", ["a", "b", "c", "d", "e"], set()) == order("blind_holdout", ["e", "d", "c", "b", "a"], set())


def test_a_case_without_a_usable_row_is_replaced_by_the_next_in_the_same_shuffle_and_nothing_else_moves():
    cases = synthetic_cases()
    s = choose(cases, V3)
    gone = s["strata"]["regular_with_tools"]["chosen"][0]
    every_id = {c["id"] for c in cases}
    res = choose(cases, V3, available=every_id - {gone})
    nxt = s["strata"]["regular_with_tools"]["reserve_in_order"][0]
    assert gone not in res["ids"] and nxt in res["ids"] and res["total"] == 15
    assert [i for i in s["ids"] if i != gone and i in res["ids"]] == [i for i in s["ids"] if i != gone]  # the rest of the draw stays


def test_the_choice_cannot_see_a_verdict_a_score_a_note_or_a_key():
    code = inspect.getsource(mod).split('"""', 2)[2]  # after the module docstring, which says what it does not read
    for fn in (mod.choose, mod.order, mod.sheet_order, mod.build_sheets):
        src = inspect.getsource(fn)
        for forbidden in ("verdict", "judge_reason", "judge_grounded", "judge_complete", "labels_template", "labels_v2_key", "labels_v3_key"):
            assert forbidden not in src, (fn.__name__, forbidden)
    assert code.count('"verdict"') == 0 and 'in ("pass", "fail")' in code  # only the existence of a verdict is looked at, never which one


def judged_row(case_id: str, verdict: str) -> dict:
    return {"id": case_id, "holdout": "yes" if case_id.startswith("ho") else "", "question": "a question", "criteria": "expected behavior", "answer": "an answer",
            "evidence_summary": "no tools", "evidence": "the evidence", "judge_relevance": 5, "judge_grounded": 4, "judge_complete": 3, "judge_tone": 5, "judge_verdict": verdict,
            "judge_reason": "SECRET-REASON", "human_grounded": "", "human_complete": "", "human_pass": "", "notes": ""}


def test_the_sheet_a_human_fills_in_has_opaque_ids_and_nothing_of_the_judge_and_the_key_joins_back(tmp_path):
    ids = ["ho07_x", "q07_t", "m02_h", "o03_o", "ho13_x"]
    rows = {i: judged_row(i, "pass" if n % 2 else "fail") for n, i in enumerate(ids)}
    blind, key = build_sheets(ids, rows)
    assert [r["id"] for r in blind] == [f"v3b-{n:02d}" for n in range(1, 6)] == [r["id"] for r in key]
    assert [r["case_id"] for r in key] == sheet_order(ids) and sorted(r["case_id"] for r in key) == sorted(ids)
    path = tmp_path / "blind.csv"
    mod.write_csv(blind, path, ["id", "question", "criteria", "answer", "evidence", "human_grounded", "human_complete", "human_pass", "notes"])
    text = path.read_text(encoding="utf-8")
    header = text.splitlines()[0]
    assert header == "id,question,criteria,answer,evidence,human_grounded,human_complete,human_pass,notes"
    for leak in ("SECRET-REASON", "judge", "verdict", "holdout", "ho07", "q07_t", "m02_h", "ho13", "repetition", "replan", "20261115"):
        assert leak not in text, leak
    with path.open(newline="", encoding="utf-8") as f:
        labeled = list(csv.DictReader(f))
    assert all(r["human_pass"] == "" for r in labeled)  # the human columns come out empty
    from eval.calibration.agreement import agreement, merge_judge  # the key makes the agreement script work on the blind sheet

    for r in labeled:
        r["human_pass"] = "yes"
    merged = merge_judge(labeled, key)
    assert agreement(merged)["labeled"] == 5 and {r["judge_verdict"] for r in merged} == {"pass", "fail"}
    assert all(r["source"] == mod.SOURCE for r in key)


def test_the_v3_ids_are_the_fifteen_of_the_v3_manifest():
    ids = mod.v3_ids()
    assert len(ids) == 15 == len(set(ids))


# ---- the real manifest, the real sheet and the sealed key ----

CAL = mod.Path(__file__).resolve().parent.parent / "eval" / "calibration"


def test_the_committed_manifest_is_what_the_sampler_gives_for_the_real_dataset_and_the_real_source():
    from eval.calibration.sample import load_cases

    source = mod.ROOT.parent / mod.SOURCE
    if not source.exists():
        import pytest

        pytest.skip(f"{mod.SOURCE} is not in this checkout")
    saved = mod.json.loads((CAL / "sample_v3b.json").read_text(encoding="utf-8"))
    available = set(mod.available_rows(source, mod.load_criteria()))
    fresh = mod.choose(load_cases(), set(mod.v3_ids()), available)
    for key in ("seed", "source", "excluded_v3", "note", "total", "strata", "ids"):
        assert saved[key] == fresh[key], key
    assert saved["total"] == 15 and not set(saved["ids"]) & set(mod.v3_ids())


def test_the_key_is_sealed_its_hash_is_the_one_in_the_manifest_and_the_blind_sheet_has_no_judge_and_no_case_id():
    saved = mod.json.loads((CAL / "sample_v3b.json").read_text(encoding="utf-8"))
    assert mod.sha256(CAL / "labels_v3b_key.csv") == saved["key_sha256"], "the key was edited after the draw"
    rows = mod.read_csv(CAL / "labels_v3b_blind.csv")
    assert len(rows) == 15 == saved["sheet_rows"] and [r["id"] for r in rows] == [f"v3b-{n:02d}" for n in range(1, 16)]
    assert list(rows[0]) == ["id", "question", "criteria", "answer", "evidence", "human_grounded", "human_complete", "human_pass", "notes"]
    text = "\n".join(" ".join(r[c] for c in ("id", "question", "criteria", "answer", "evidence")) for r in rows).lower()  # what the owner reads, not what the owner writes
    for leak in [*saved["ids"], "verdict", "replan", "repetition", "holdout", "20261115"]:
        assert leak.lower() not in text, leak
