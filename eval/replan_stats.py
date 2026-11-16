"""What the observe step did in real runs, with no judge: how often it acted, what it cost in time and money, and how often it escalated.

    python -m eval.replan_stats --off A.json B.json ... --on C.json D.json ... [-o REPLAN_ON-<stamp>.md]

`--off` are runs of the main branch (REPLAN_MAX=0), `--on` the ones labeled "replan on" (REPLAN_MAX=2). The source is `routing.replans` as the eval
stored it in each row (`replans`), the row's latency and its token usage/cost. Nothing here says whether the answers got BETTER (that needs the
judge: see `eval.replan_ab`); it says how often the step fires, what it adds and what it sends to a human.
"""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

KINDS = ("retry_kb:", "retry_kb_failed", "offer_ticket", "escalate")


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    v = sorted(values)
    return v[min(len(v) - 1, int(p * len(v)))]


def rows_of(paths: list[str]) -> list[dict]:
    return [r for p in paths for r in json.loads(Path(p).read_text(encoding="utf-8"))["rows"] if not r.get("provider_error") and not r.get("error")]


def stats(rows: list[dict]) -> dict:
    lat = [r["latency_ms"] for r in rows if r.get("latency_ms")]
    cost = [r["usage"]["cost_usd"] for r in rows if (r.get("usage") or {}).get("cost_usd") is not None]
    toks = [r["usage"]["total_tokens"] for r in rows if (r.get("usage") or {}).get("total_tokens")]
    acted = [r for r in rows if r.get("replans")]
    return {
        "rows": len(rows), "acted": len(acted), "acted_ids": sorted({r["id"] for r in acted}),
        "kinds": {k: sum(any(x.startswith(k) for x in r["replans"]) for r in rows) for k in KINDS},
        "escalated_by_observe": sum(any(x.startswith("escalate") for x in r.get("replans", [])) for r in rows),
        "handoffs": sum(bool(r.get("handoff")) for r in rows),
        "latency_p50": pct(lat, 0.5), "latency_p95": pct(lat, 0.95), "latency_mean": statistics.mean(lat) if lat else 0.0,
        "cost_mean": statistics.mean(cost) if cost else None, "cost_total": sum(cost) if cost else None,
        "tokens_mean": statistics.mean(toks) if toks else None,
        "voyage_extra_calls": sum(any(x.startswith("retry_kb") for x in r.get("replans", [])) for r in rows),
    }


def render(off: dict, on: dict, off_files: list[str], on_files: list[str]) -> str:
    def usd(v):
        return "n/a" if v is None else f"${v:.4f}"

    def delta(a, b, fmt):
        return "n/a" if a is None or b is None else fmt(b - a)

    L = ["# Observe step on vs off (no judge)", "",
         f"_Off (REPLAN_MAX=0): {', '.join(Path(f).name for f in off_files)}. On (REPLAN_MAX=2): {', '.join(Path(f).name for f in on_files)}._", "",
         "_This says how often the step fires and what it adds. It does not say whether the answers are better: that needs the judge._", "",
         "| | off | on | difference |", "|---|---|---|---|",
         f"| case-runs | {off['rows']} | {on['rows']} | |",
         f"| latency p50 | {off['latency_p50'] / 1000:.1f} s | {on['latency_p50'] / 1000:.1f} s | {delta(off['latency_p50'], on['latency_p50'], lambda d: f'{d / 1000:+.1f} s')} |",
         f"| latency p95 | {off['latency_p95'] / 1000:.1f} s | {on['latency_p95'] / 1000:.1f} s | {delta(off['latency_p95'], on['latency_p95'], lambda d: f'{d / 1000:+.1f} s')} |",
         f"| latency mean | {off['latency_mean'] / 1000:.2f} s | {on['latency_mean'] / 1000:.2f} s | {delta(off['latency_mean'], on['latency_mean'], lambda d: f'{d / 1000:+.2f} s')} |",
         f"| agent cost per case-run | {usd(off['cost_mean'])} | {usd(on['cost_mean'])} | {delta(off['cost_mean'], on['cost_mean'], lambda d: f'{d:+.4f}')} |",
         f"| agent tokens per case-run | {off['tokens_mean'] and round(off['tokens_mean'])} | {on['tokens_mean'] and round(on['tokens_mean'])} | |",
         # a RATE: the two branches ran a different number of case-runs, so raw counts would compare 200 with 80
         f"| handoff rate | {off['handoffs']}/{off['rows']} = {off['handoffs'] / off['rows'] * 100:.1f}% | {on['handoffs']}/{on['rows']} = {on['handoffs'] / on['rows'] * 100:.1f}% "
         f"| {(on['handoffs'] / on['rows'] - off['handoffs'] / off['rows']) * 100:+.1f} points |", "",
         "## What the step did (runs with REPLAN_MAX=2)", "",
         f"- Acted in **{on['acted']} of {on['rows']}** case-runs ({on['acted'] / on['rows'] * 100:.0f}%)" + (": " + ", ".join(on["acted_ids"]) if on["acted_ids"] else "") if on["rows"] else "- no rows",
         f"- Retried the knowledge base: {on['kinds']['retry_kb:']} (failed: {on['kinds']['retry_kb_failed']}); each one is one extra Voyage call: {on['voyage_extra_calls']} in all",
         f"- Offered a ticket because no step was left: {on['kinds']['offer_ticket']}",
         f"- **Escalated to a human instead of answering: {on['escalated_by_observe']}**",
         f"- In the off runs it acted in {off['acted']} case-runs (it must be 0: the step is off)", ""]
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--off", nargs="+", required=True)
    ap.add_argument("--on", nargs="+", required=True)
    ap.add_argument("-o", "--out")
    args = ap.parse_args(argv)
    text = render(stats(rows_of(args.off)), stats(rows_of(args.on)), args.off, args.on)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"written: {args.out}")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
