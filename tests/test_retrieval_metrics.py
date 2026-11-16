"""Retrieval metrics on synthetic rankings with known values. No network."""
import math
from types import SimpleNamespace

import pytest

from eval.run_retrieval import (average, evaluate, hit_at, load, ndcg_at, ranked_urls, recall_at, reciprocal_rank,
                                render, score)

R = ["a", "b", "c", "d", "e"]


def test_mrr_is_one_over_the_rank_of_the_first_relevant():
    assert reciprocal_rank(R, {"a"}) == 1.0
    assert reciprocal_rank(R, {"c"}) == pytest.approx(1 / 3)
    assert reciprocal_rank(R, {"e", "d"}) == pytest.approx(1 / 4)  # the first relevant one counts
    assert reciprocal_rank(R, {"z"}) == 0.0
    # mean over three queries with first relevant at ranks 1, 2 and none -> (1 + 0.5 + 0) / 3 = 0.5
    rows = [{"scores": score(R, ["a"])}, {"scores": score(R, ["b"])}, {"scores": score(R, ["z"])}]
    assert average(rows)["mrr"] == pytest.approx(0.5)


def test_hit_and_recall_at_k():
    assert (hit_at(R, {"c"}, 1), hit_at(R, {"c"}, 3), hit_at(R, {"c"}, 5)) == (0.0, 1.0, 1.0)
    assert recall_at(R, {"a", "e", "z"}, 5) == pytest.approx(2 / 3)
    assert recall_at(R, {"a", "e"}, 3) == pytest.approx(0.5)


def test_ndcg_with_known_values():
    assert ndcg_at(R, {"a"}, 5) == pytest.approx(1.0)
    assert ndcg_at(R, {"b"}, 5) == pytest.approx(1 / math.log2(3))  # 0.6309
    # relevant at ranks 2 and 4, two relevant: (1/log2 3 + 1/log2 5) / (1 + 1/log2 3)
    assert ndcg_at(R, {"b", "d"}, 5) == pytest.approx((1 / math.log2(3) + 1 / math.log2(5)) / (1 + 1 / math.log2(3)))
    assert ndcg_at(R, {"z"}, 5) == 0.0
    assert ndcg_at(R, {"a", "b"}, 5) == pytest.approx(1.0)


def doc(url):
    return SimpleNamespace(metadata={"url": url})


def test_ranking_is_the_distinct_urls_in_order_and_evaluate_scores_and_isolates_errors():
    results = [(doc("u1"), 0.1), (doc("u1"), 0.2), (doc("u2"), 0.3), (doc(""), 0.4), (doc("u3"), 0.5)]
    assert ranked_urls(results) == ["u1", "u2", "u3"]

    def retrieve(q, k):
        if q == "boom":
            raise RuntimeError("RateLimitError 429")
        return results

    cases = [{"id": "q1", "question": "x", "expected_urls": ["u2"]},
             {"id": "q2", "question": "y", "expected_urls": ["u9"], "holdout": True},
             {"id": "q3", "question": "boom", "expected_urls": ["u1"]}]
    rows = evaluate(cases, retrieve, 5)
    assert rows[0]["scores"]["mrr"] == 0.5 and rows[1]["scores"]["hit@5"] == 0.0 and "error" in rows[2]
    text = render(rows, 5, "stamp")
    assert "| regular | 1 | 0.00 | 1.00 | 1.00 | 1.00 | 0.50 |" in text
    assert "| holdout | 1 |" in text and "1 question(s) failed" in text


def test_the_dataset_has_about_twenty_questions_and_a_third_holdout():
    cases = load()
    held = [c for c in cases if c.get("holdout")]
    assert 18 <= len(cases) <= 25 and 0.25 <= len(held) / len(cases) <= 0.35
    assert all(c["expected_urls"] and c["question"] for c in cases)
    assert len({c["id"] for c in cases}) == len(cases)


# ---- Phase 5: halves, modes, the cache warm-up ---------------------------------------------------------------------
def test_the_regular_questions_are_split_in_two_halves_and_the_holdout_is_never_tuned_on():
    from eval.run_retrieval import group_of

    cases = load()
    regular = [c for c in cases if not c.get("holdout")]
    tune = [c for c in regular if group_of(c) == "tune"]
    report = [c for c in regular if group_of(c) == "report"]
    assert tune and report and abs(len(tune) - len(report)) <= 1  # halves
    assert not any(c.get("split") for c in cases if c.get("holdout"))  # a holdout question has no tune mark
    assert all(group_of(c) == "holdout" for c in cases if c.get("holdout"))
    assert {c["id"] for c in tune}.isdisjoint({c["id"] for c in report})


def test_the_report_has_a_row_per_half_only_when_the_questions_are_marked():
    def row(i, **extra):
        return {"id": i, "ranking": R, "expected_urls": [i], "scores": score(R, [i]), **extra}

    text = render([row("a", split="tune"), row("b"), row("c", holdout=True)], 5, "s", "hybrid")
    assert "mode `hybrid`" in text and "| regular, tune half | 1 |" in text and "| regular, report half | 1 |" in text
    assert "tune half" not in render([row("b")], 5, "s")


def test_a_question_that_ran_with_the_reranker_skipped_is_flagged_not_hidden():
    def retrieve(q, k):
        retrieve.last_degraded = ["rerank"]
        return [(doc("u1"), 0.1)]

    rows = evaluate([{"id": "q1", "question": "x", "expected_urls": ["u1"]}], retrieve, 5)
    assert rows[0]["degraded"] == ["rerank"]
    assert "ran with a stage skipped" in render(rows, 5, "s", "hybrid_rerank") and "q1 (rerank)" in render(rows, 5, "s")


def test_the_modes_side_by_side_are_one_table_per_group():
    from eval.run_retrieval import render_compare

    good = [{"id": "a", "split": "tune", "scores": score(R, ["a"])}]
    bad = [{"id": "a", "split": "tune", "scores": score(R, ["e"])}]
    text = render_compare({"dense": bad, "hybrid": good}, 5, "s")
    assert "### regular, tune half" in text and "| dense | 1 | 0.00 |" in text and "| hybrid | 1 | 1.00 |" in text


def test_warming_the_cache_embeds_all_missing_questions_in_one_request(tmp_path, monkeypatch):
    import voyageai

    from app.rag import hybrid
    from app.rag.cache import DiskCache
    from eval.run_retrieval import warm_cache

    requests = []

    class FakeClient:
        def __init__(self, **kw):
            pass

        def embed(self, texts, model, input_type):
            requests.append((list(texts), input_type))
            return SimpleNamespace(embeddings=[[float(len(t)), 1.0] for t in texts])

    monkeypatch.setattr(voyageai, "Client", FakeClient)
    monkeypatch.setattr(hybrid, "get_cache", lambda: DiskCache(tmp_path / "w.sqlite"))
    assert warm_cache(["one", "three", "five"]) == 3 and len(requests) == 1 and requests[0][1] == "query"
    assert warm_cache(["one", "three", "five", "seven"]) == 1 and requests[1][0] == ["seven"]  # only what was missing
    assert warm_cache(["one", "seven"]) == 0 and len(requests) == 2  # all cached: no request at all
