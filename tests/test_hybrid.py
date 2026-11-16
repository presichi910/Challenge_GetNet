"""Phase 5: hybrid search (dense + BM25 + RRF + rerank) and the disk cache. A synthetic corpus, no network, no Voyage."""
import sqlite3

import numpy as np
import pytest
from langchain_core.documents import Document

from app.rag import hybrid
from app.rag.cache import DiskCache, digest
from app.rag.hybrid import (bm25_ranking, build_corpus, cap_pdfs, dense_ranking, embed_cached, hybrid_search, rerank_cached, rrf,
                            tokenize)

# Six chunks. The vectors are 2-D on purpose: the dense search sees only "direction". `d4` is the page that mentions the exact error
# code, but its vector points away from the query, so dense alone puts it last; BM25 finds it by the word.
TEXTS = [
    "Como habilitar o Pix no aplicativo Getnet passo a passo",          # d0  page
    "Pix recebimento taxas e prazos",                                    # d1  page
    "Manual da maquininha Get Smart bateria e carregador",               # d2  PDF
    "Contrato de adesao Getnet condicoes gerais",                        # d3  PDF
    "Erro NET_TIMEOUT: reinicie o terminal e verifique o sinal",         # d4  page (the one a person needs)
    "Antecipacao de vendas no aplicativo",                               # d5  page
]
VECTORS = [[1.0, 0.1], [0.9, 0.3], [0.8, 0.5], [0.7, 0.6], [-1.0, 0.2], [0.6, 0.8]]
IS_PDF = [False, False, True, True, False, False]
QUERY_VEC = [1.0, 0.0]


@pytest.fixture
def corpus():
    metas = [{"url": f"https://x/{i}", "is_pdf": IS_PDF[i], "title": f"t{i}"} for i in range(6)]
    return build_corpus([f"id{i}" for i in range(6)], TEXTS, metas, VECTORS)


def urls(result):
    return [d.metadata["url"].rsplit("/", 1)[1] for d, _ in result.hits]


def test_tokenize_drops_accents_case_stopwords_and_one_letter_words():
    assert tokenize("Como habilitar o Pix? É rápido, e a ANTECIPAÇÃO!") == ["habilitar", "pix", "rapido", "antecipacao"]
    assert tokenize("What is the NET_TIMEOUT error?") == ["net", "timeout", "error"]


def test_rrf_scores_by_rank_and_rewards_agreement():
    # 1 and 3 are first in one list and last in the other (1/61 + 1/63 each, a tie: the lower index goes first); that beats 2,
    # which is second in both (2/62): a very high rank counts for more than a middling one twice
    assert rrf([[1, 2, 3], [3, 2, 1]], k=60) == [1, 3, 2]
    assert rrf([[1, 2], [2, 3]], k=60)[0] == 2  # in both lists beats first in one
    assert rrf([[5, 6], [5]], k=60)[0] == 5  # first in both beats anything
    assert rrf([[7], []]) == [7] and rrf([]) == []
    top_dense_only, top_both = 1 / 61, 1 / 61 + 1 / 62
    assert top_both > top_dense_only


def test_the_dense_ranking_is_the_exact_cosine_order(corpus):
    assert dense_ranking(corpus, QUERY_VEC) == [0, 1, 2, 3, 5, 4]


def test_bm25_finds_the_exact_code_that_the_dense_search_ranks_last(corpus):
    assert bm25_ranking(corpus, "NET_TIMEOUT") == [4]
    assert bm25_ranking(corpus, "???") == [] and bm25_ranking(corpus, "") == []  # nothing to match is no ranking, not an error


def test_the_pdf_cap_keeps_two_pdf_chunks_at_most_and_the_rest_pages_in_order():
    is_pdf = np.array(IS_PDF)
    assert cap_pdfs([2, 3, 0, 1, 4, 5], is_pdf, k=5) == [2, 3, 0, 1, 4]
    assert cap_pdfs([2, 3, 0, 1, 4, 5], is_pdf, k=4) == [2, 3, 0, 1]
    assert cap_pdfs([2, 3, 2, 3], is_pdf, k=5, pdf_slots=1) == [2]
    assert cap_pdfs([0, 1, 4, 5], is_pdf, k=5) == [0, 1, 4]  # only 3 page slots: the 4th page does not take a PDF's place


def test_dense_alone_misses_the_chunk_hybrid_brings_up(corpus):
    assert 4 not in dense_ranking(corpus, QUERY_VEC)[:5]  # by direction alone the page with the error code is last
    fused = hybrid_search("NET_TIMEOUT", 5, QUERY_VEC, "hybrid", corpus, rrf_k=1)
    assert urls(fused)[0] == "4"  # the word match is first once the two rankings are fused


def test_hybrid_keeps_the_dense_order_when_the_query_has_no_word_to_match(corpus):
    result = hybrid_search("???", 5, QUERY_VEC, "hybrid", corpus)
    assert urls(result) == ["0", "1", "2", "3", "5"] and result.degraded == []


def test_the_distance_returned_is_the_true_cosine_distance_to_the_query(corpus):
    result = hybrid_search("pix", 3, QUERY_VEC, "hybrid", corpus)
    for doc, dist in result.hits:
        i = int(doc.metadata["url"].rsplit("/", 1)[1])
        v = np.array(VECTORS[i]) / np.linalg.norm(VECTORS[i])
        assert dist == pytest.approx(1 - float(v @ np.array([1.0, 0.0])), abs=1e-5)
    assert all(isinstance(d, Document) for d, _ in result.hits)


def test_the_result_never_holds_more_than_two_pdf_chunks(corpus):
    result = hybrid_search("manual contrato maquininha", 5, QUERY_VEC, "hybrid", corpus)
    assert sum(d.metadata["is_pdf"] for d, _ in result.hits) <= 2


def fake_rerank(order_of_ids):
    """A reranker that prefers the ids in the given order; counts its calls."""
    calls = []

    def rerank(query, texts, model, attempts, wait_s):
        calls.append((query, list(texts)))
        # the score of each text: higher the earlier its chunk is in `order_of_ids`
        return [float(len(order_of_ids) - next(i for i, t in enumerate(order_of_ids) if TEXTS[t] == x)) for x in texts]

    rerank.calls = calls
    return rerank


def test_the_reranker_puts_what_it_prefers_first_and_reads_only_the_pool(corpus):
    rerank = fake_rerank([5, 1, 0, 2, 3, 4])
    result = hybrid_search("pix", 3, QUERY_VEC, "hybrid_rerank", corpus, pool=4, rerank=rerank)
    assert len(rerank.calls) == 1 and len(rerank.calls[0][1]) == 4  # it read 4 candidates, not the whole corpus
    assert urls(result)[0] in ("5", "1")  # a candidate it prefers moved up
    assert result.degraded == []


def test_the_pdf_cap_is_applied_after_the_reranker(corpus):
    result = hybrid_search("manual", 5, QUERY_VEC, "hybrid_rerank", corpus, pool=6, rerank=fake_rerank([2, 3, 0, 1, 4, 5]))
    assert sum(d.metadata["is_pdf"] for d, _ in result.hits) <= 2 and len(result.hits) == 5


def test_if_the_reranker_fails_the_fused_order_stands_and_the_search_says_so(corpus):
    def boom(*a, **k):
        raise RuntimeError("503")

    fused = hybrid_search("pix", 4, QUERY_VEC, "hybrid", corpus)
    result = hybrid_search("pix", 4, QUERY_VEC, "hybrid_rerank", corpus, rerank=boom)
    assert urls(result) == urls(fused) and result.degraded == ["rerank"]


def test_if_bm25_fails_the_search_is_the_dense_one_and_says_so(corpus, monkeypatch):
    monkeypatch.setattr(hybrid, "bm25_ranking", lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    result = hybrid_search("pix", 5, QUERY_VEC, "hybrid", corpus)
    assert urls(result) == ["0", "1", "2", "3", "5"] and result.degraded == ["bm25"]


# ---- the disk cache ---------------------------------------------------------------------------------------------
def test_a_query_is_embedded_once_and_then_read_from_disk(tmp_path):
    cache, calls = DiskCache(tmp_path / "c.sqlite"), []

    def embed(q):
        calls.append(q)
        return [0.25, 0.5]

    assert embed_cached("how do I enable Pix?", cache, embed) == [0.25, 0.5]
    assert embed_cached("how do I enable Pix?", DiskCache(tmp_path / "c.sqlite"), embed) == [0.25, 0.5]  # a new process: same file
    assert calls == ["how do I enable Pix?"]
    embed_cached("another question", cache, embed)
    assert len(calls) == 2


def test_the_cache_file_never_holds_the_text_of_a_query(tmp_path):
    path = tmp_path / "c.sqlite"
    secret = "my card number is 4111 1111 1111 1111 and my name is Carlos"
    embed_cached(secret, DiskCache(path), lambda q: [1.0, 2.0])
    rerank_cached(secret, ["id1", "id2"], ["a", "b"], DiskCache(path), rerank=lambda *a: [0.1, 0.9])
    dump = b"".join(sqlite3.connect(str(path)).execute("SELECT kind || key || value FROM kv").fetchall()[i][0].encode() for i in range(2))
    assert b"4111" not in dump and b"Carlos" not in dump and path.read_bytes().count(b"Carlos") == 0


def test_a_cache_without_a_path_never_stores_and_never_fails(tmp_path):
    cache, calls = DiskCache(""), []
    for _ in range(2):
        embed_cached("q", cache, lambda q: (calls.append(q), [1.0])[1])
    assert len(calls) == 2


def test_a_damaged_cache_is_a_miss_not_an_error(tmp_path):
    path = tmp_path / "c.sqlite"
    path.write_bytes(b"this is not a sqlite file at all")
    cache = DiskCache.__new__(DiskCache)  # a file that was fine when the process started and got damaged
    import threading
    cache.path, cache._lock = path, threading.Lock()
    assert cache.get("embedding", "k") is None
    cache.put("embedding", "k", [1.0])  # does not raise


def test_the_rerank_scores_are_cached_per_query_and_candidate_set(tmp_path):
    cache, calls = DiskCache(tmp_path / "c.sqlite"), []

    def rerank(q, texts, model, attempts, wait_s):
        calls.append(1)
        return [0.2, 0.8]

    assert rerank_cached("q", ["a", "b"], ["ta", "tb"], cache, rerank) == [0.2, 0.8]
    assert rerank_cached("q", ["a", "b"], ["ta", "tb"], cache, rerank) == [0.2, 0.8] and len(calls) == 1
    rerank_cached("q", ["a", "c"], ["ta", "tc"], cache, rerank)  # another candidate set is another request
    assert len(calls) == 2
    assert digest("a", "b") != digest("ab")


# ---- the entry point -------------------------------------------------------------------------------------------
def test_an_unknown_mode_is_refused(monkeypatch):
    from app.tools import knowledge_tools

    monkeypatch.setattr("app.rag.hybrid.embed_cached", lambda *a, **k: [1.0, 0.0])
    monkeypatch.setattr("app.rag.store.get_embeddings", lambda: None)
    with pytest.raises(ValueError, match="RAG_MODE"):
        knowledge_tools.search("q", 5, mode="sparse")


def test_dense_mode_is_the_original_search_two_calls_and_sorted_by_distance(monkeypatch):
    from app.tools import knowledge_tools

    class FakeStore:
        def __init__(self):
            self.calls = []

        def similarity_search_by_vector_with_relevance_scores(self, vec, k, filter):
            self.calls.append((k, filter))
            return [(Document(page_content="p", metadata={"url": "pdf"}), 0.3)] if filter["is_pdf"] else \
                   [(Document(page_content="a", metadata={"url": "a"}), 0.5), (Document(page_content="b", metadata={"url": "b"}), 0.1)]

    store = FakeStore()
    monkeypatch.setattr("app.rag.hybrid.embed_cached", lambda *a, **k: [1.0, 0.0])
    monkeypatch.setattr("app.rag.store.get_embeddings", lambda: None)
    monkeypatch.setattr("app.rag.store.get_vector_store", lambda: store)
    result = knowledge_tools.search("q", 5, mode="dense")
    assert store.calls == [(3, {"is_pdf": False}), (2, {"is_pdf": True})]
    assert [d.metadata["url"] for d, _ in result.hits] == ["b", "pdf", "a"] and result.mode == "dense" and result.degraded == []


def test_the_tests_never_use_the_real_disk_cache():
    """The default path is ./data/rag_cache.sqlite; conftest turns it off for every test (a fake vector in it would poison real searches)."""
    from app.config import get_settings
    from app.rag.hybrid import get_cache

    assert get_settings().rag_cache_path == "" and get_cache().path is None
