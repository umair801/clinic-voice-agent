"""Turn real call logs into results/live_calls.json (the demo page and README read this).
Run after your 20+ test calls:  python scripts/summarize_calls.py
Only files in logs/calls/ named call_*.jsonl are counted. Numbers here are TEST-CALL results (synthetic patients)."""
import glob, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ava.telemetry import read_call, summarize, _pct, LOG_DIR

files = sorted(glob.glob(os.path.join(LOG_DIR, "call_*.jsonl")))
sums, lats, costs, mins, false_claims, dup = [], [], [], 0.0, 0, 0
for f in files:
    rows = read_call(f)
    s = summarize(rows)
    if not s or s["turns"] == 0:
        continue
    sums.append(s)
    lats += [r["response_latency_s"] for r in rows if r.get("kind") == "turn" and r.get("response_latency_s")]
    costs.append(s["llm_cost_usd"]); mins += s["duration_min"]
    false_claims += sum(1 for r in rows if r.get("event") == "false_booking_claim")
out = {"label": "test-call results, synthetic patients", "available": bool(sums), "calls": len(sums),
       "total_minutes": round(mins, 2), "latency_p50_s": _pct(lats, .5), "latency_p95_s": _pct(lats, .95),
       "latency_note": "end of caller turn committed to agent reply finished, measured in the worker; see docs for exact definition",
       "llm_cost_total_usd": round(sum(costs), 5), "cost_per_min_usd": round(sum(costs) / mins, 5) if mins else None,
       "false_booking_claims_flagged": false_claims,
       "tier_counts": {t: sum(s["tier_counts"].get(t, 0) for s in sums) for t in ("small", "large")}}
os.makedirs("results", exist_ok=True)
json.dump(out, open("results/live_calls.json", "w"), indent=2)
print(json.dumps(out, indent=2))
if len(sums) < 20:
    print(f"\nWARNING: only {len(sums)} calls. The project standard is 20+ before quoting any number.")
