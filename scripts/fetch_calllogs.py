"""Download call logs from Supabase into logs/calls/<call_id>.jsonl, the same files the summary and replay tools read.

Usage:  python scripts/fetch_calllogs.py --since-minutes 60
        python scripts/fetch_calllogs.py --since 1794188000
Reads SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY from .env. Never prints them.
"""
import argparse
import json
import os
import sys
import time

import httpx
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def fetch(since, base, key):
    h = {"apikey": key, "Authorization": f"Bearer {key}"}
    out, offset = [], 0
    while True:
        r = httpx.get(f"{base}/rest/v1/cva_call_logs", headers=h, timeout=30,
                      params={"select": "call_id,ts,data", "ts": f"gte.{since}", "order": "ts.asc", "limit": "1000", "offset": str(offset)})
        r.raise_for_status()
        page = r.json()
        out += page
        if len(page) < 1000:
            return out
        offset += 1000


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", type=float, default=0)
    ap.add_argument("--since-minutes", type=float, default=0)
    a = ap.parse_args(argv)
    load_dotenv(os.path.join(ROOT, ".env"))
    base, key = os.environ.get("SUPABASE_URL", "").rstrip("/"), os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not base or not key:
        sys.exit("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be in .env")
    since = a.since or (time.time() - a.since_minutes * 60 if a.since_minutes else 0)
    rows = fetch(since, base, key)
    d = os.path.join(ROOT, "logs", "calls")
    os.makedirs(d, exist_ok=True)
    seen, files = set(), {}
    for r in rows:
        row = r["data"]
        k = (r["call_id"], json.dumps(row, sort_keys=True))
        if k in seen:
            continue            # a retried batch can arrive twice
        seen.add(k)
        files.setdefault(r["call_id"], []).append(row)
    for cid, rs in files.items():
        with open(os.path.join(d, f"{cid}.jsonl"), "w") as f:
            for row in sorted(rs, key=lambda x: x["ts"]):
                f.write(json.dumps(row) + "\n")
    print(f"{len(rows)} rows, {len(files)} calls written to logs/calls")


if __name__ == "__main__":
    main()
