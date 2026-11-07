"""Rebuild call log files from a downloaded Railway log export.

    python scripts/import_railway_logs.py railway_logs.json

On Railway the voice worker prints every call-log row as a line starting with CALLLOG. In the Railway
service, open Deploy Logs, click the download icon, save the file in the project folder, then run this.
It writes logs/calls/<call_id>.jsonl (so `replay` and scripts/summarize_calls.py work) and skips duplicates.
Works with JSON, CSV or plain text exports.
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "logs", "calls")
MARK = "CALLLOG "
DEC = json.JSONDecoder()


def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from _strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v)


def _candidates(raw):
    try:
        for s in _strings(json.loads(raw)):
            yield s
        return
    except ValueError:
        pass
    for line in raw.splitlines():
        yield line


def _row(text):
    i = text.find(MARK)
    if i < 0:
        return None
    body = text[i + len(MARK):]
    j = body.find("{")
    if j < 0:
        return None
    body = body[j:]
    for variant in (body, body.replace('""', '"'), body.replace('\\"', '"')):
        try:
            obj, _ = DEC.raw_decode(variant)
            if isinstance(obj, dict) and "call_id" in obj and "ts" in obj:
                return obj
        except ValueError:
            continue
    return None


def parse(raw):
    """Returns {call_id: [rows sorted by ts]} with exact duplicates removed."""
    calls, seen = {}, set()
    for text in _candidates(raw):
        for chunk in text.split(MARK)[1:]:
            row = _row(MARK + chunk)
            if not row:
                continue
            key = json.dumps(row, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            calls.setdefault(row["call_id"], []).append(row)
    return {k: sorted(v, key=lambda r: r["ts"]) for k, v in calls.items()}


def main(argv=None):
    argv = argv or sys.argv[1:]
    if not argv:
        sys.exit("usage: python scripts/import_railway_logs.py <downloaded log file>")
    raw = open(argv[0], encoding="utf-8", errors="ignore").read()
    calls = parse(raw)
    os.makedirs(OUT, exist_ok=True)
    for cid, rows in calls.items():
        with open(os.path.join(OUT, f"{cid}.jsonl"), "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
    print(f"imported {len(calls)} call(s): {', '.join(calls) or 'none found'}")
    if not calls:
        print("No CALLLOG lines found. Check the file is the worker's Deploy Logs and that the worker redeployed after this update.")


if __name__ == "__main__":
    main()
