"""Config and prompts as code, versioned, with an eval gate and rollback (failure 11).

Layout:  releases/<version>/{agent.json, system.md, MANIFEST.json}   immutable snapshots
         releases/POINTER.json  {"stable": "1.0.0", "canary": null, "canary_percent": 0, "history": [...]}

Dashboard edits cannot change a call: the worker loads ONLY a snapshot from the repo, selected here.

CLI (run from the repo root, using python -c):
  python -c "from ava.release import cli; cli(['publish'])"                  snapshot working config, run evals, set as stable
  python -c "from ava.release import cli; cli(['canary','1.1.0','10'])"      send 10 percent of calls to 1.1.0 (gated)
  python -c "from ava.release import cli; cli(['promote'])"                  canary becomes stable
  python -c "from ava.release import cli; cli(['rollback'])"                 previous stable (or drop canary)
  python -c "from ava.release import cli; cli(['status'])"
"""
import hashlib
import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REL = os.path.join(ROOT, "releases")
POINTER = os.path.join(REL, "POINTER.json")


def _sha(*paths):
    h = hashlib.sha256()
    for p in paths:
        with open(p, "rb") as f:
            h.update(f.read())
    return h.hexdigest()[:16]


def _read_pointer():
    if not os.path.exists(POINTER):
        return {"stable": None, "canary": None, "canary_percent": 0, "history": []}
    return json.load(open(POINTER))


def _write_pointer(p):
    os.makedirs(REL, exist_ok=True)
    json.dump(p, open(POINTER, "w"), indent=2)


def snapshot():
    cfg = json.load(open(os.path.join(ROOT, "config", "agent.json")))
    v = cfg["version"]
    d = os.path.join(REL, v)
    if os.path.exists(d):
        raise SystemExit(f"release {v} already exists and is immutable. Bump 'version' in config/agent.json.")
    os.makedirs(d)
    prompt_src = os.path.join(ROOT, cfg["prompt_file"])
    shutil.copy(os.path.join(ROOT, "config", "agent.json"), os.path.join(d, "agent.json"))
    shutil.copy(prompt_src, os.path.join(d, "system.md"))
    json.dump({"version": v, "hash": _sha(os.path.join(d, "agent.json"), os.path.join(d, "system.md"))}, open(os.path.join(d, "MANIFEST.json"), "w"))
    return v


def gate():
    """Run the eval suite. Returns True only if every eval passes."""
    from evals.run import run_all
    results = run_all(verbose=False)
    ok = all(r["passed"] for r in results)
    print(f"eval gate: {sum(r['passed'] for r in results)}/{len(results)} passed -> {'OPEN' if ok else 'BLOCKED'}")
    return ok


def load_release(version):
    d = os.path.join(REL, version)
    m = json.load(open(os.path.join(d, "MANIFEST.json")))
    actual = _sha(os.path.join(d, "agent.json"), os.path.join(d, "system.md"))
    if actual != m["hash"]:
        raise RuntimeError(f"release {version} was modified after publish (hash mismatch). Refusing to load.")
    return json.load(open(os.path.join(d, "agent.json"))), open(os.path.join(d, "system.md")).read(), m


def pick_version(call_id, pointer=None):
    """Staged rollout: stable by default; canary_percent of calls (by stable hash of call_id) get the canary."""
    p = pointer or _read_pointer()
    if p.get("canary") and p.get("canary_percent", 0) > 0:
        bucket = int(hashlib.sha256(call_id.encode()).hexdigest(), 16) % 100
        if bucket < p["canary_percent"]:
            return p["canary"]
    return p["stable"]


def cli(argv):
    cmd = argv[0] if argv else "status"
    p = _read_pointer()
    if cmd == "publish":
        if not gate():
            raise SystemExit("blocked: evals failed, nothing published")
        v = snapshot()
        if p["stable"]:
            p["history"].append(p["stable"])
        p["stable"], p["canary"], p["canary_percent"] = v, None, 0
        _write_pointer(p)
        print(f"published {v} as stable")
    elif cmd == "canary":
        v, pct = argv[1], int(argv[2])
        if not os.path.exists(os.path.join(REL, v)):
            raise SystemExit("unknown version; publish it as a snapshot first")
        if not gate():
            raise SystemExit("blocked: evals failed")
        p["canary"], p["canary_percent"] = v, pct
        _write_pointer(p)
        print(f"canary {v} at {pct}%")
    elif cmd == "promote":
        if not p["canary"]:
            raise SystemExit("no canary to promote")
        p["history"].append(p["stable"])
        p["stable"], p["canary"], p["canary_percent"] = p["canary"], None, 0
        _write_pointer(p)
        print("promoted", p["stable"])
    elif cmd == "rollback":
        if p["canary"]:
            print("dropped canary", p["canary"])
            p["canary"], p["canary_percent"] = None, 0
        elif p["history"]:
            bad = p["stable"]
            p["stable"] = p["history"].pop()
            print(f"rolled back {bad} -> {p['stable']}")
        else:
            raise SystemExit("nothing to roll back to")
        _write_pointer(p)
    elif cmd == "status":
        print(json.dumps(p, indent=2))
    else:
        raise SystemExit("unknown command")
