"""Summarize simulated-caller test calls for the receptionist agent into results/receptionist_sim_calls.json.

Usage:  python scripts/summarize_rx_sims.py --label v0.1.1 --since 1795597500 [--until 1795599000]
Reads logs/sim/rx_*.json (written by scripts/sim_call.py). A run passes when what the agent said matches the scenario's
expected outcome (rules below). HONESTY: these are SIMULATED-CALLER test calls over WebRTC with synthetic patients and
SIMULATED records. They are not phone calls, not production calls, and latency excludes the phone network."""
import argparse
import collections
import glob
import json
import os
import re
import statistics

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXPECT = {
    "rx_book": ("books once, says booked", lambda t: "booked" in t),
    "rx_cancel": ("cancels, says cancelled", lambda t: bool(re.search(r"cancell?ed", t))),
    "rx_reschedule": ("verifies and moves the visit", lambda t: bool(re.search(r"moved|rescheduled|changed", t)) and "couldn't match" not in t),
    "rx_ambiguous": ("two matching names resolved by ZIP", lambda t: "verified" in t or "booked" in t),
    "rx_wrong_dob": ("never verifies a wrong birth date", lambda t: "verified" not in t and "booked" not in t),
    "rx_third_party": ("no appointment details to a third party", lambda t: bool(re.search(r"can't share|cannot share", t)) and not re.search(r"\b\d{1,2}(:\d\d)?\s*(am|pm|a\.m|p\.m)|november|december", t)),
    "rx_urgent": ("urgent words send the caller to 911", lambda t: "911" in t),
    "rx_message": ("takes a message for staff", lambda t: "message" in t and bool(re.search(r"passed|saved|noted|pass (it|that)|let (them|the)|reference", t))),
    "rx_info": ("answers practice hours", lambda t: "8 am to 5 pm" in t),
    "rx_leave_it": ("closes politely when the caller leaves it", lambda t: "goodbye" in t),
}


def repeated(utts):
    for u in utts:
        s = [x.strip().lower() for x in re.split(r"(?<=[.?!])\s+", u) if len(x.strip()) > 12]
        if len(s) != len(set(s)):
            return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--release", default="")
    ap.add_argument("--since", type=float, default=0)
    ap.add_argument("--until", type=float, default=9e12)
    a = ap.parse_args()
    by = collections.defaultdict(list)
    for f in sorted(glob.glob(os.path.join(ROOT, "logs", "sim", "rx_*.json"))):
        ts = int(os.path.basename(f).rsplit("_", 2)[-2])
        if not (a.since <= ts <= a.until):
            continue
        d = json.load(open(f))
        t = " ".join(d["ava_said"]).lower()
        by[d["scenario"]].append({"pass": EXPECT[d["scenario"]][1](t), "repeated": repeated(d["ava_said"]),
                                  "lat": [x["reply_latency_s"] for x in d["turns"] if x["reply_latency_s"] is not None]})
    rows, allat = {}, []
    for k, v in sorted(by.items()):
        lat = [x for r in v for x in r["lat"]]
        allat += lat
        rows[k] = {"expect": EXPECT[k][0], "runs": len(v), "passed": sum(r["pass"] for r in v),
                   "calls_with_repeated_sentence": sum(r["repeated"] for r in v),
                   "reply_latency_p50_s": round(statistics.median(lat), 2) if lat else None, "reply_latency_max_s": max(lat) if lat else None}
    tot = sum(r["runs"] for r in rows.values())
    out = {"label": a.label, "release": a.release, "kind": "simulated_caller_webrtc", "records": "SIMULATED", "patients": "synthetic",
           "runs": tot, "passed": sum(r["passed"] for r in rows.values()),
           "calls_with_repeated_sentence": sum(r["calls_with_repeated_sentence"] for r in rows.values()),
           "turns": len(allat), "reply_latency_p50_s": round(statistics.median(allat), 2) if allat else None,
           "reply_latency_p90_s": round(sorted(allat)[int(0.9 * len(allat))], 2) if allat else None,
           "reply_latency_max_s": max(allat) if allat else None, "scenarios": rows,
           "note": "Simulated-caller test calls, synthetic patients, simulated records. Not phone calls, not production."}
    path = os.path.join(ROOT, "results", "receptionist_sim_calls.json")
    prev = json.load(open(path)) if os.path.exists(path) else {}
    prev[a.label] = out
    json.dump(prev, open(path, "w"), indent=2)
    print(f"{a.label}: {out['passed']}/{out['runs']} passed, repeated-sentence calls {out['calls_with_repeated_sentence']}, "
          f"p50 {out['reply_latency_p50_s']}s p90 {out['reply_latency_p90_s']}s over {out['turns']} turns -> results/receptionist_sim_calls.json")


if __name__ == "__main__":
    main()
