"""Redacts one Jaeger trace for docs/evidence/jaeger_trace_2026-11-18.json (the same logic as the script that was run, tidied for the repository: the raw export was not kept, so this copy was not re-run on it).

    curl -s "http://127.0.0.1:16686/api/traces?service=getnet-agents&limit=5" > raw.json
    python docs/evidence/redact_jaeger_trace.py raw.json out.json

What it changes: absolute timestamps become offsets from the start of the turn, and the process tag service.instance.id becomes <redacted>.
It keeps every span tag as Jaeger returned it: the API puts no message text, token, header or customer id in a span (tests/test_telemetry.py),
and the capture was checked with a grep for those patterns (docs/EVIDENCE.md).
"""
import json
import sys

src, dst = sys.argv[1], sys.argv[2]
with open(src, encoding="utf-8") as f:
    raw = json.load(f)
assert len(raw["data"]) == 1, "expects exactly one trace"
t = raw["data"][0]
t0 = min(s["startTime"] for s in t["spans"])
spans = [
    {
        "spanID": s["spanID"],
        "parentSpanID": (s.get("references") or [{}])[0].get("spanID"),
        "operationName": s["operationName"],
        "startOffsetMicros": s["startTime"] - t0,
        "durationMicros": s["duration"],
        "tags": list(s["tags"]),
        "logs": s.get("logs", []),
    }
    for s in sorted(t["spans"], key=lambda s: s["startTime"])
]
procs = {
    k: {"serviceName": v["serviceName"], "tags": [{**x, "value": "<redacted>"} if x["key"] == "service.instance.id" else x for x in v["tags"]]}
    for k, v in t["processes"].items()
}
out = {
    "_capture": {
        "what": "one trace of the Getnet API as Jaeger 1.62.0 returned it (GET /api/traces?service=getnet-agents), exported by the owner's stack of docker-compose.yml, profile observability",
        "date": "2026-11-18",
        "code": "exp/b-plus, app/ identical to 98ea315",
        "request": "ONE POST /chat with a synthetic greeting and no customer data; the no-LLM requests tried first (GET /health, GET /conversations, GET /metrics) produced no trace of the API",
        "redaction": [
            "absolute timestamps replaced by offsets from the start of the turn (startOffsetMicros)",
            "process tag service.instance.id replaced by <redacted>",
            "no token, header, message text, customer id or conversation id is in the spans (checked by grep, see docs/EVIDENCE.md)",
        ],
    },
    "traceID": t["traceID"],
    "spans": spans,
    "processes": procs,
}
with open(dst, "w", encoding="utf-8", newline="\n") as f:
    json.dump(out, f, indent=2, ensure_ascii=False)
print("spans:", len(spans))
