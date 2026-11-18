"""Redacts one Jaeger trace for docs/evidence/jaeger_trace_2026-11-18.json and docs/evidence/jaeger_trace_tools_2026-11-18.json.

Who ran what: the FIRST file (the greeting) was made by a scratch version of this logic whose raw export was not kept, so this copy was not re-run on that one.
The SECOND file (the read-only turn with tools) was made by THIS script, run on its raw export, which was then deleted.

    curl -s "http://127.0.0.1:16686/api/traces?service=getnet-agents&limit=5" > raw.json
    python docs/evidence/redact_jaeger_trace.py raw.json out.json "<what the one request was>"

What it changes: absolute timestamps become offsets from the start of the turn, the process tag service.instance.id becomes <redacted>, and float tags
(the cost) are rounded to 8 decimals (floating-point noise such as 0.0029363999999999996 is not data and looks like a long number).
It keeps every span tag as Jaeger returned it: the API puts no message text, token, header or customer id in a span (tests/test_telemetry.py),
and the capture was checked with a grep for those patterns (docs/EVIDENCE.md).
"""
import json
import sys

src, dst, request = sys.argv[1], sys.argv[2], sys.argv[3]
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
        "tags": [{**x, "value": round(x["value"], 8)} if isinstance(x["value"], float) else x for x in s["tags"]],
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
        "request": request,
        "redaction": [
            "absolute timestamps replaced by offsets from the start of the turn (startOffsetMicros)",
            "process tag service.instance.id replaced by <redacted>",
            "float tags (cost_usd) rounded to 8 decimals: floating-point noise, not data",
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
