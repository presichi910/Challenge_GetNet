"""Hybrid search of the knowledge base: dense (Voyage embeddings) + BM25, fused with RRF, optionally reranked.

    dense         the original search: Chroma nearest neighbours of the query embedding (pages and PDFs searched apart)
    hybrid        dense ranking + BM25 ranking over the same chunks, fused with Reciprocal Rank Fusion
    hybrid_rerank hybrid, then a Voyage cross-encoder reorders the best `pool` candidates

Why: a dense search misses exact words (a product name, an error code such as "NET_TIMEOUT") and BM25 misses paraphrases;
RRF needs no score calibration between the two (only ranks), and the reranker reads the question and each chunk together.
The whole corpus (a few thousand chunks) is held in memory: BM25 needs every chunk's text, and with the stored embeddings the
dense ranking is an exact cosine search (no approximate-index misses). The final list keeps the rule of the original search:
at most PDF_SLOTS PDF chunks out of k, so PDFs (60% of the chunks) cannot crowd the pages out.

A search NEVER fails because of the extras: if BM25 or the reranker cannot run, the result is what the stage before it gave
(and the search reports it as `degraded`, so a measurement can tell).
"""
from __future__ import annotations

import logging
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any
from functools import lru_cache

import numpy as np
from langchain_core.documents import Document

from app.config import get_settings
from app.rag.cache import DiskCache, digest

log = logging.getLogger(__name__)

PDF_SLOTS = 2
MODES = ("dense", "hybrid", "hybrid_rerank")
# words that carry no topic in the three languages of the corpus (pt, en, es): without them BM25 ranks by "the", "de", "que"
STOPWORDS = frozenset("""a o os as um uma uns umas de do da dos das em no na nos nas por para com sem sobre e ou que se
the an of to in on for with without and or is are be do does how what can i my me you your it its at by from as this that
el la los las un una unos unas del al en con sin sobre y o que es son por para como mi tu su lo le""".split())


def tokenize(text: str) -> list[str]:
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    return [t for t in re.findall(r"[a-z0-9]+", plain) if len(t) > 1 and t not in STOPWORDS]


@dataclass
class Corpus:
    ids: list[str]
    texts: list[str]
    metadatas: list[dict]
    vectors: np.ndarray  # unit length, so a dot product is the cosine similarity
    bm25: Any = None  # a rank_bm25.BM25Okapi (no type stubs)
    is_pdf: np.ndarray | None = field(default=None)

    def document(self, i: int) -> Document:
        return Document(page_content=self.texts[i], metadata=self.metadatas[i])


def build_corpus(ids, texts, metadatas, vectors) -> Corpus:
    from rank_bm25 import BM25Okapi

    v = np.asarray(vectors, dtype=np.float32)
    v = v / np.clip(np.linalg.norm(v, axis=1, keepdims=True), 1e-12, None)
    tokens = [tokenize(t) or ["_"] for t in texts]  # BM25Okapi cannot hold an empty document
    return Corpus(list(ids), list(texts), list(metadatas), v, BM25Okapi(tokens),
                  np.array([bool(m.get("is_pdf")) for m in metadatas]))


_corpus_lock = threading.Lock()
_corpus: tuple[int, Corpus] | None = None


def get_corpus() -> Corpus:
    """Loaded from Chroma once per process and rebuilt if the number of chunks changes (a new ingest)."""
    global _corpus
    from app.rag.store import get_vector_store

    col = get_vector_store()._collection
    size = col.count()
    with _corpus_lock:
        if _corpus is None or _corpus[0] != size:
            data = col.get(include=["documents", "metadatas", "embeddings"])
            _corpus = (size, build_corpus(data["ids"], data["documents"], data["metadatas"], data["embeddings"]))
        return _corpus[1]


def dense_ranking(c: Corpus, qvec) -> list[int]:
    q = np.asarray(qvec, dtype=np.float32)
    q = q / max(float(np.linalg.norm(q)), 1e-12)
    return [int(i) for i in np.argsort(-(c.vectors @ q), kind="stable")]


def bm25_ranking(c: Corpus, query: str) -> list[int]:
    tokens = tokenize(query)
    if not tokens or c.bm25 is None:
        return []
    scores = np.asarray(c.bm25.get_scores(tokens))
    return [int(i) for i in np.argsort(-scores, kind="stable") if scores[i] > 0]


def rrf(rankings: list[list[int]], k: int = 60) -> list[int]:
    """Reciprocal Rank Fusion: a chunk's score is the sum of 1 / (k + rank) over the rankings that list it."""
    score: dict[int, float] = {}
    for ranking in rankings:
        for rank, i in enumerate(ranking, start=1):
            score[i] = score.get(i, 0.0) + 1.0 / (k + rank)
    return sorted(score, key=lambda i: (-score[i], i))


def cap_pdfs(order: list[int], is_pdf, k: int, pdf_slots: int = PDF_SLOTS) -> list[int]:
    """The first k of `order` with at most `pdf_slots` PDF chunks and at most k - pdf_slots pages (as the original search)."""
    pages = pdfs = 0
    out = []
    for i in order:
        if is_pdf[i]:
            if pdfs >= pdf_slots:
                continue
            pdfs += 1
        else:
            if pages >= k - pdf_slots:
                continue
            pages += 1
        out.append(i)
        if len(out) == k:
            break
    return out


@dataclass
class SearchResult:
    hits: list[tuple[Document, float]]  # (chunk, cosine distance to the query), in final order
    mode: str
    degraded: list[str] = field(default_factory=list)  # stages that could not run and were skipped


def embed_cached(query: str, cache: DiskCache, embed) -> list[float]:
    """The query's embedding: from the disk cache if it was paid for before, else `embed(query)` and remember it."""
    key = digest(get_settings().embedding_model, query)
    vec = cache.get("embedding", key)
    if vec is None:
        vec = embed(query)
        cache.put("embedding", key, [float(x) for x in vec])
    return vec


def voyage_rerank(query: str, texts: list[str], model: str, attempts: int, wait_s: float) -> list[float]:
    """One relevance score per text, from Voyage's reranker. Retries a rate limit `attempts` times in all."""
    import voyageai

    client = voyageai.Client(api_key=get_settings().voyage_api_key or None, max_retries=0)
    for attempt in range(1, attempts + 1):
        try:
            res = client.rerank(query, texts, model=model, top_k=len(texts))
            scores = [0.0] * len(texts)
            for r in res.results:
                scores[r.index] = float(r.relevance_score)
            return scores
        except Exception as e:  # noqa: BLE001
            limited = "ratelimit" in type(e).__name__.lower() or "rate limit" in str(e).lower()
            if not limited or attempt == attempts:
                raise
            log.warning("Rerank rate-limited; waiting %.0fs (attempt %d/%d)", wait_s, attempt, attempts)
            time.sleep(wait_s)
    raise AssertionError("unreachable")


def rerank_cached(query: str, ids: list[str], texts: list[str], cache: DiskCache, rerank=voyage_rerank,
                  attempts: int = 1, wait_s: float = 21.0, model: str | None = None) -> list[float]:
    model = model or get_settings().rerank_model
    key = digest(model, query, "|".join(ids))
    scores = cache.get("rerank", key)
    if scores is None:
        scores = rerank(query, texts, model, attempts, wait_s)
        cache.put("rerank", key, scores)
    return scores


@lru_cache
def get_cache() -> DiskCache:
    return DiskCache(get_settings().rag_cache_path)


def hybrid_search(query: str, k: int, qvec, mode: str, corpus: Corpus, cache: DiskCache | None = None, *, pool: int | None = None,
                  rrf_k: int | None = None, rerank=voyage_rerank, rerank_attempts: int = 1, wait_s: float = 21.0,
                  rerank_model: str | None = None) -> SearchResult:
    """`qvec` is the query's embedding (the caller pays for it once). Pure apart from the reranker call."""
    s = get_settings()
    pool = pool or s.rag_pool
    rrf_k = rrf_k or s.rag_rrf_k
    cache = cache or DiskCache("")
    dense = dense_ranking(corpus, qvec)
    degraded: list[str] = []
    try:
        lexical = bm25_ranking(corpus, query)
    except Exception:  # noqa: BLE001 - BM25 is an extra: without it the search is the dense one
        log.exception("BM25 failed; using the dense ranking alone")
        lexical, degraded = [], [*degraded, "bm25"]
    order = rrf([dense, lexical], rrf_k) if lexical else dense

    if mode == "hybrid_rerank":
        candidates = order[:pool]  # the best of the fused order; the PDF cap is applied to what the reranker puts first
        try:
            scores = rerank_cached(query, [corpus.ids[i] for i in candidates], [corpus.texts[i] for i in candidates], cache,
                                   rerank, rerank_attempts, wait_s, rerank_model)
            order = [i for _, i in sorted(zip(scores, candidates), key=lambda p: -p[0])] + [i for i in order if i not in set(candidates)]
        except Exception as e:  # noqa: BLE001 - rate limit, no key, network: the fused order stands
            log.warning("Rerank skipped (%s: %s); using the fused order", type(e).__name__, str(e)[:120])
            degraded = [*degraded, "rerank"]

    q = np.asarray(qvec, dtype=np.float32)
    q = q / max(float(np.linalg.norm(q)), 1e-12)
    top = cap_pdfs(order, corpus.is_pdf, k)
    return SearchResult([(corpus.document(i), float(1.0 - corpus.vectors[i] @ q)) for i in top], mode, degraded)
