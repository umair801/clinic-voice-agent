"""Config and prompts as code, versioned, with an eval gate and rollback (failure 11).

Layout:  releases/<version>/{agent.json, system.md, MANIFEST.json}   immutable snapshots
         releases/POINTER.json  {"stable": "1.0.0", "canary": null, "canary_percent": 0, "history": [...]}

Dashboard edits cannot change a call: the worker loads ONLY a snapshot from the repo, selected here.

Every command takes an optional "--agent <id>" (default agent: see agents/registry.json). Other agents keep their
snapshots and pointer in their own releases folder, so a bad release of one agent never touches another.

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

from ava import profiles

ROOT = profiles.ROOT
REL = os.path.join(ROOT, "releases")            # default agent's folder, kept for older callers
POINTER = os.path.join(REL, "POINTER.json")


def _prof(agent_id=None):
    return profiles.get(agent_id)


def _sha(*paths):
    h = hashlib.sha256()
    for p in paths:
        with open(p, "rb") as f:
            h.update(f.read())
    return h.hexdigest()[:16]


def _read_pointer(agent_id=None):
    path = _prof(agent_id)["pointer_path"]
    if not os.path.exists(path):
        return {"stable": None, "canary": None, "canary_percent": 0, "history": []}
    return json.load(open(path))


def _write_pointer(p, agent_id=None):
    prof = _prof(agent_id)
    os.makedirs(prof["releases_dir"], exist_ok=True)
    json.dump(p, open(prof["pointer_path"], "w"), indent=2)


def snapshot(agent_id=None):
    prof = _prof(agent_id)
    cfg = json.load(open(prof["config_path"]))
    v = cfg["version"]
    d = os.path.join(prof["releases_dir"], v)
    if os.path.exists(d):
        raise SystemExit(f"release {v} already exists and is immutable. Bump 'version' in {os.path.relpath(prof['config_path'], ROOT)}.")
    os.makedirs(d)
    prompt_src = os.path.join(ROOT, cfg["prompt_file"])
    shutil.copy(prof["config_path"], os.path.join(d, "agent.json"))
    shutil.copy(prompt_src, os.path.join(d, "system.md"))
    json.dump({"version": v, "hash": _sha(os.path.join(d, "agent.json"), os.path.join(d, "system.md"))}, open(os.path.join(d, "MANIFEST.json"), "w"))
    return v


def gate(agent_id=None):
    """Run THIS agent's eval suite (agents/registry.json "evals"). Returns True only if every eval passes."""
    import importlib
    run_all = importlib.import_module(_prof(agent_id)["evals"]).run_all
    results = run_all(verbose=False)
    ok = all(r["passed"] for r in results)
    print(f"eval gate: {sum(r['passed'] for r in results)}/{len(results)} passed -> {'OPEN' if ok else 'BLOCKED'}")
    return ok


def load_release(version, agent_id=None):
    d = os.path.join(_prof(agent_id)["releases_dir"], version)
    m = json.load(open(os.path.join(d, "MANIFEST.json")))
    actual = _sha(os.path.join(d, "agent.json"), os.path.join(d, "system.md"))
    if actual != m["hash"]:
        raise RuntimeError(f"release {version} was modified after publish (hash mismatch). Refusing to load.")
    return json.load(open(os.path.join(d, "agent.json"))), open(os.path.join(d, "system.md")).read(), m


def pick_version(call_id, pointer=None, agent_id=None):
    """Staged rollout: stable by default; canary_percent of calls (by stable hash of call_id) get the canary."""
    p = pointer or _read_pointer(agent_id)
    if p.get("canary") and p.get("canary_percent", 0) > 0:
        bucket = int(hashlib.sha256(call_id.encode()).hexdigest(), 16) % 100
        if bucket < p["canary_percent"]:
            return p["canary"]
    return p["stable"]


def cli(argv):
    argv = list(argv)
    agent_id = None
    if "--agent" in argv:
        i = argv.index("--agent")
        agent_id = argv[i + 1]
        del argv[i:i + 2]
    prof = _prof(agent_id)        # rejects an unknown agent before anything is written
    agent_id = prof["id"]
    cmd = argv[0] if argv else "status"
    p = _read_pointer(agent_id)
    if cmd == "publish":
        if not gate(agent_id):
            raise SystemExit("blocked: evals failed, nothing published")
        v = snapshot(agent_id)
        if p["stable"]:
            p["history"].append(p["stable"])
        p["stable"], p["canary"], p["canary_percent"] = v, None, 0
        _write_pointer(p, agent_id)
        print(f"published {v} as stable")
    elif cmd == "canary":
        v, pct = argv[1], int(argv[2])
        if not os.path.exists(os.path.join(prof["releases_dir"], v)):
            raise SystemExit("unknown version; publish it as a snapshot first")
        if not gate(agent_id):
            raise SystemExit("blocked: evals failed")
        p["canary"], p["canary_percent"] = v, pct
        _write_pointer(p, agent_id)
        print(f"canary {v} at {pct}%")
    elif cmd == "promote":
        if not p["canary"]:
            raise SystemExit("no canary to promote")
        p["history"].append(p["stable"])
        p["stable"], p["canary"], p["canary_percent"] = p["canary"], None, 0
        _write_pointer(p, agent_id)
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
        _write_pointer(p, agent_id)
    elif cmd == "status":
        print(json.dumps(p, indent=2))
    else:
        raise SystemExit("unknown command")
