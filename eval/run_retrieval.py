"""Retrieval metrics of the knowledge base search, against the real retriever (Chroma + Voyage query embeddings).

    python -m eval.run_retrieval              # all questions of eval/retrieval.jsonl -> eval/reports/RETRIEVAL-<stamp>.md
    python -m eval.run_retrieval --pause 21   # Voyage free tier (~3 requests/min)
    python -m eval.run_retrieval --warm       # embed every question in ONE Voyage request and keep it in the disk cache
    python -m eval.run_retrieval --modes dense hybrid hybrid_rerank   # before / after in one report
    python -m eval.run_retrieval --grid       # choose RRF k, rerank pool and model on the TUNE half only (prints nothing else)

Each question has the page(s) that answer it (`expected_urls`, checked to exist in the KB). The ranking is the
list of distinct URLs, in order, of the chunks `app.tools.knowledge_tools.retrieve` returns (k = RETRIEVAL_K,
web pages and at most 2 PDF chunks, exactly what the Knowledge agent gets). Binary relevance: a URL is
relevant if it is one of the expected ones.

Metrics per question, then averaged: hit@1/3/5 (an expected URL in the top k), recall@5 (share of the
expected URLs in the top 5), MRR (1 / rank of the first expected URL, 0 if none) and nDCG@5. Questions marked
`"holdout": true` are averaged apart; they are for checking a retrieval change, never for tuning one. The regular questions are
split in two halves: `"split": "tune"` (the ones a retrieval parameter may be chosen on) and the rest (the "report" half, only
measured), so a number we publish was not the one we tuned on.
Only Voyage is paid (query embeddings, and the reranker in `hybrid_rerank`; both are kept on disk, a question is never paid for
twice); no LLM is called.
"""
from __future__ import annotations

import argparse
import json
import math
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent
DATASET = ROOT / "retrieval.jsonl"


def load(path: Path = DATASET) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def ranked_urls(results) -> list[str]:
    """Distinct URLs of [(doc, distance)] in order (they come sorted by distance)."""
    out = []
    for doc, _ in results:
        url = doc.metadata.get("url", "")
        if url and url not in out:
            out.append(url)
    return out


def hit_at(ranking: list[str], relevant: set[str], k: int) -> float:
    return 1.0 if any(u in relevant for u in ranking[:k]) else 0.0


def recall_at(ranking: list[str], relevant: set[str], k: int) -> float:
    return len(relevant & set(ranking[:k])) / len(relevant) if relevant else 0.0


def reciprocal_rank(ranking: list[str], relevant: set[str]) -> float:
    for i, u in enumerate(ranking, start=1):
        if u in relevant:
            return 1.0 / i
    return 0.0


def ndcg_at(ranking: list[str], relevant: set[str], k: int) -> float:
    dcg = sum(1.0 / math.log2(i + 1) for i, u in enumerate(ranking[:k], start=1) if u in relevant)
    ideal = sum(1.0 / math.log2(i + 1) for i in range(1, min(len(relevant), k) + 1))
    return dcg / ideal if ideal else 0.0


def score(ranking: list[str], expected: list[str]) -> dict:
    rel = set(expected)
    return {"hit@1": hit_at(ranking, rel, 1), "hit@3": hit_at(ranking, rel, 3), "hit@5": hit_at(ranking, rel, 5),
            "recall@5": recall_at(ranking, rel, 5), "mrr": reciprocal_rank(ranking, rel), "ndcg@5": ndcg_at(ranking, rel, 5)}


METRICS = ("hit@1", "hit@3", "hit@5", "recall@5", "mrr", "ndcg@5")


def group_of(case: dict) -> str:
    """holdout (never tuned on), tune (the half a parameter may be chosen on) or report (the other regular half)."""
    if case.get("holdout"):
        return "holdout"
    return "tune" if case.get("split") == "tune" else "report"


def average(rows: list[dict]) -> dict:
    ok = [r for r in rows if "error" not in r]
    return {m: (sum(r["scores"][m] for r in ok) / len(ok)) if ok else None for m in METRICS} | {"n": len(ok)}


def evaluate(cases: list[dict], retrieve, k: int, pause: float = 0.0, sleep=time.sleep) -> list[dict]:
    """`retrieve(question, k)` -> [(chunk, distance)]. If it also sets `retrieve.last_degraded` (the stages it had to skip), the
    row says so: a number measured with the reranker silently skipped would be the wrong number."""
    rows = []
    for i, c in enumerate(cases):
        try:
            ranking = ranked_urls(retrieve(c["question"], k))
            rows.append({**c, "ranking": ranking, "scores": score(ranking, c["expected_urls"]),
                         "degraded": list(getattr(retrieve, "last_degraded", []))})
        except Exception as e:  # noqa: BLE001 - a provider error is reported, not scored
            rows.append({**c, "error": f"{type(e).__name__}: {str(e)[:200]}"})
        if pause and i < len(cases) - 1:
            sleep(pause)
    return rows


def groups(rows: list[dict]) -> list[tuple[str, list[dict]]]:
    main, held = [r for r in rows if not r.get("holdout")], [r for r in rows if r.get("holdout")]
    out = [("regular", main)]
    if any(r.get("split") for r in main):  # the halves only show when the dataset marks them
        out += [("regular, tune half", [r for r in main if group_of(r) == "tune"]),
                ("regular, report half", [r for r in main if group_of(r) == "report"])]
    return out + [("holdout", held), ("all", rows)]


def render(rows: list[dict], k: int, stamp: str, mode: str = "") -> str:
    lines = ["# Retrieval report", "", f"_Generated {stamp}. Retriever: `app.tools.knowledge_tools.search`"
             f"{f', mode `{mode}`' if mode else ''}, k = {k} "
             "chunks (at most 2 PDF chunks), ranking = distinct URLs in order. Binary relevance._", "",
             "| set | n | hit@1 | hit@3 | hit@5 | recall@5 | MRR | nDCG@5 |", "|---|---|---|---|---|---|---|---|"]
    for name, part in groups(rows):
        a = average(part)
        if a["n"]:
            lines.append(f"| {name} | {a['n']} | " + " | ".join(f"{a[m]:.2f}" for m in METRICS) + " |")
    errors = [r for r in rows if "error" in r]
    if errors:
        lines += ["", f"**{len(errors)} question(s) failed (provider error) and are not scored:** "
                  + ", ".join(r["id"] for r in errors)]
    degraded = [r for r in rows if r.get("degraded")]
    if degraded:
        lines += ["", f"**{len(degraded)} question(s) ran with a stage skipped (not the mode named above):** "
                  + ", ".join(f"{r['id']} ({'+'.join(r['degraded'])})" for r in degraded)]
    lines += ["", "## Questions", "", "| id | first expected rank | MRR | nDCG@5 | top URLs |", "|---|---|---|---|---|"]
    for r in rows:
        if "error" in r:
            lines.append(f"| {r['id']} | error | - | - | {r['error'][:80]} |")
            continue
        rank = next((i for i, u in enumerate(r["ranking"], 1) if u in set(r["expected_urls"])), None)
        top = ", ".join(u.split("//", 1)[-1][:60] for u in r["ranking"][:3])
        lines.append(f"| {r['id']}{' (holdout)' if r.get('holdout') else ' (tune)' if r.get('split') == 'tune' else ''} | {rank or 'not found'} "
                     f"| {r['scores']['mrr']:.2f} | {r['scores']['ndcg@5']:.2f} | {top} |")
    return "\n".join(lines) + "\n"


GRID_RRF_K = (10, 30, 60)
GRID_POOLS = (10, 20, 30)
GRID_RERANK_MODELS = ("rerank-2.5", "rerank-2.5-lite")


def tune_grid(cases: list[dict], search_fn, k: int, rrf_ks=GRID_RRF_K, pools=GRID_POOLS, models=GRID_RERANK_MODELS) -> list[dict]:
    """The parameters that may be chosen, measured on the TUNE half only (the report half and the holdout are not read here).
    `search_fn(question, k, mode, **tuning)` -> SearchResult. One row per setting, with its tune-half averages."""
    tune = [c for c in cases if group_of(c) == "tune"]
    settings = [("hybrid", {"rrf_k": r}) for r in rrf_ks]
    settings += [("hybrid_rerank", {"rrf_k": r, "pool": p, "rerank_model": m}) for r in rrf_ks for p in pools for m in models]
    out = []
    for mode, tuning in settings:
        def retrieve(q, kk, _m=mode, _t=tuning):
            result = search_fn(q, kk, _m, **_t)
            retrieve.last_degraded = result.degraded
            return result.hits

        rows = evaluate(tune, retrieve, k)
        out.append({"mode": mode, **tuning, **average(rows), "degraded": sum(bool(r.get("degraded")) for r in rows)})
    return out


def render_grid(rows: list[dict]) -> str:
    lines = ["| mode | rrf_k | pool | rerank model | n | hit@1 | hit@5 | MRR | nDCG@5 | degraded |", "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['mode']} | {r['rrf_k']} | {r.get('pool', '-')} | {r.get('rerank_model', '-')} | {r['n']} | {r['hit@1']:.2f} "
                     f"| {r['hit@5']:.2f} | {r['mrr']:.2f} | {r['ndcg@5']:.2f} | {r['degraded']} |")
    return "\n".join(lines)


def render_compare(by_mode: dict[str, list[dict]], k: int, stamp: str) -> str:
    """One table per metric group: the modes side by side (the before / after)."""
    lines = ["# Retrieval: modes compared", "", f"_Generated {stamp}. k = {k} chunks (at most 2 PDF chunks). The tune half is where a "
             "parameter may be chosen; the report half and the holdout are only measured._", ""]
    for name in ("regular, tune half", "regular, report half", "regular", "holdout", "all"):
        lines += [f"### {name}", "", "| mode | n | hit@1 | hit@3 | hit@5 | recall@5 | MRR | nDCG@5 |", "|---|---|---|---|---|---|---|---|"]
        for mode, rows in by_mode.items():
            part = dict(groups(rows)).get(name)
            a = average(part) if part else {"n": 0}
            if a["n"]:
                lines.append(f"| {mode} | {a['n']} | " + " | ".join(f"{a[m]:.2f}" for m in METRICS) + " |")
        lines.append("")
    return "\n".join(lines)


def warm_cache(questions: list[str]) -> int:
    """Embed the questions that are not in the disk cache in ONE Voyage request (the free tier counts requests, not texts)."""
    import voyageai

    from app.config import get_settings
    from app.rag.cache import digest
    from app.rag.hybrid import get_cache

    s, cache = get_settings(), get_cache()
    missing = [q for q in questions if cache.get("embedding", digest(s.embedding_model, q)) is None]
    if missing:
        res = voyageai.Client(api_key=s.voyage_api_key or None).embed(missing, model=s.embedding_model, input_type="query")
        for q, vec in zip(missing, res.embeddings):
            cache.put("embedding", digest(s.embedding_model, q), [float(x) for x in vec])
    return len(missing)


def main() -> None:
    from app.config import get_settings
    from app.tools.knowledge_tools import search

    ap = argparse.ArgumentParser()
    ap.add_argument("--pause", type=float, default=0.0, help="seconds between queries (Voyage rate limits)")
    ap.add_argument("--k", type=int, default=None, help="chunks to retrieve (default RETRIEVAL_K)")
    ap.add_argument("--modes", nargs="+", default=None, help="dense, hybrid, hybrid_rerank (default: RAG_MODE)")
    ap.add_argument("--only", nargs="+", default=None, help="question id prefixes, e.g. r01 r03")
    ap.add_argument("--warm", action="store_true", help="embed every question in one request into the disk cache, then go on")
    ap.add_argument("--grid", action="store_true", help="measure RRF k / rerank pool / rerank model on the tune half only")
    ap.add_argument("--rerank-attempts", type=int, default=4, help="tries per rerank call when Voyage rate limits (21 s apart)")
    args = ap.parse_args()
    s = get_settings()
    k = args.k or s.retrieval_k
    cases = [c for c in load() if not args.only or any(c["id"].startswith(p) for p in args.only)]
    if args.warm:
        print(f"Embedded {warm_cache([c['question'] for c in cases])} question(s) not yet in the cache (one request).")
    if args.grid:
        print("# Tuning grid, TUNE half only\n")
        print(render_grid(tune_grid(cases, lambda q, kk, m, **t: search(q, kk, mode=m, rerank_attempts=args.rerank_attempts, **t), k)))
        return
    modes = args.modes or [s.rag_mode]
    by_mode: dict[str, list[dict]] = {}
    for mode in modes:
        def retrieve(q, kk, _mode=mode):
            result = search(q, kk, mode=_mode, rerank_attempts=args.rerank_attempts)
            retrieve.last_degraded = result.degraded
            return result.hits

        by_mode[mode] = evaluate(cases, retrieve, k, pause=args.pause)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    reports = ROOT / "reports"
    reports.mkdir(exist_ok=True)
    text = "\n".join(render(rows, k, stamp, mode) for mode, rows in by_mode.items())
    if len(by_mode) > 1:
        text = render_compare(by_mode, k, stamp) + "\n" + text
    out = reports / f"RETRIEVAL-{stamp}.md"
    out.write_text(text, encoding="utf-8")
    (reports / f"RETRIEVAL-{stamp}.json").write_text(json.dumps(by_mode, indent=2, ensure_ascii=False), encoding="utf-8")
    print(text.split("## Questions")[0])
    print(f"Report: {out}")


if __name__ == "__main__":
    main()
