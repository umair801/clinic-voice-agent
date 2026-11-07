"""Round-trip check for scripts/import_railway_logs.py with JSON, CSV and plain-text exports.
Run: python -c "from tests.log_import import main; main()"
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    from scripts.import_railway_logs import parse
    rows = [{"kind": "event", "call_id": "call_x", "ts": 2.0, "event": "call_end", "level": "info", "note": 'quote " inside'},
            {"kind": "event", "call_id": "call_x", "ts": 1.0, "event": "call_start", "level": "info"},
            {"kind": "turn", "call_id": "call_y", "ts": 5.0, "turn": 1}]
    lines = ["CALLLOG " + json.dumps(r) for r in rows] + ["noise line", "CALLLOG " + json.dumps(rows[1])]
    plain = "\n".join(lines)
    as_json = json.dumps([{"timestamp": "t", "message": m} for m in lines])
    as_csv = "\n".join('t,"' + m.replace('"', '""') + '",info' for m in lines)
    ok = True
    for name, raw in (("plain", plain), ("json", as_json), ("csv", as_csv)):
        got = parse(raw)
        good = set(got) == {"call_x", "call_y"} and [r["event"] for r in got["call_x"]] == ["call_start", "call_end"] and len(got["call_x"]) == 2
        ok = ok and good
        print(("PASS" if good else "FAIL"), "railway log import:", name)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
