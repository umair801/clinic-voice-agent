"""Offline checks for agent profiles (one engine, many agents). Usage: python -c "from tests.profiles import main; main()"

Proves: the clinic agent resolves to its original paths and its published release still loads (no behavior change),
a second agent has its own releases and pointer, unknown ids are rejected, and the browser token carries the agent_id.
"""
import json
import os
import shutil
import tempfile


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f": {detail}" if detail and not cond else ""))
    if not cond:
        raise SystemExit(1)


def main():
    from ava import profiles, release

    # 1. Default agent and normalization.
    check("default agent is clinic", profiles.default_agent_id() == "clinic")
    check("none and empty mean the default", profiles.normalize(None) == "clinic" and profiles.normalize("  ") == "clinic")
    try:
        profiles.normalize("nope")
        check("unknown agent is rejected", False)
    except profiles.UnknownAgent:
        check("unknown agent is rejected", True)

    # 2. The clinic profile keeps its original paths, and its published release loads with a matching hash.
    p = profiles.get("clinic")
    check("clinic config path unchanged", p["config_path"] == os.path.join(profiles.ROOT, "config", "agent.json"))
    check("clinic pointer path unchanged", p["pointer_path"] == release.POINTER)
    ptr = json.load(open(p["pointer_path"]))
    cfg, prompt, manifest = release.load_release(ptr["stable"])
    cfg2, prompt2, manifest2 = release.load_release(ptr["stable"], agent_id="clinic")
    check("stable clinic release loads and is the same through the profile",
          (cfg, prompt, manifest) == (cfg2, prompt2, manifest2) and prompt.strip() != "")
    check("pick_version for the default agent follows the pointer", release.pick_version("call_x") == ptr["stable"])

    # 3. Worker metadata parsing.
    a = profiles.agent_id_from_metadata
    check("metadata with agent_id", a('{"agent_id": "medical"}') == "medical")
    check("metadata empty, bad or wrong type gives None", a("") is None and a(None) is None and a("not json") is None and a("[1]") is None)

    # 4. A second agent in a temporary repo: own config, own releases, own pointer. The real repo is not touched.
    tmp = tempfile.mkdtemp(prefix="profiles_test_")
    saved = (profiles.ROOT, profiles.REGISTRY, release.ROOT)
    try:
        os.makedirs(os.path.join(tmp, "agents", "medical"))
        os.makedirs(os.path.join(tmp, "config"))
        os.makedirs(os.path.join(tmp, "releases"))
        os.makedirs(os.path.join(tmp, "prompts"))
        json.dump({"default": "clinic", "agents": {
            "clinic": {"title": "Clinic", "status": "live", "config": "config/agent.json", "releases": "releases"},
            "medical": {"title": "Medical", "status": "draft", "config": "agents/medical/agent.json",
                        "releases": "agents/medical/releases"}}}, open(os.path.join(tmp, "agents", "registry.json"), "w"))
        json.dump({"version": "9.0.0", "prompt_file": "prompts/system.md"}, open(os.path.join(tmp, "config", "agent.json"), "w"))
        open(os.path.join(tmp, "prompts", "system.md"), "w").write("clinic prompt")
        json.dump({"version": "0.1.0", "prompt_file": "agents/medical/system.md"}, open(os.path.join(tmp, "agents", "medical", "agent.json"), "w"))
        open(os.path.join(tmp, "agents", "medical", "system.md"), "w").write("medical prompt")
        profiles.ROOT, profiles.REGISTRY, release.ROOT = tmp, os.path.join(tmp, "agents", "registry.json"), tmp

        check("drafts are hidden from the public list", [x["id"] for x in profiles.list_agents()] == ["clinic"])
        check("drafts show when asked", [x["id"] for x in profiles.list_agents(public=False)] == ["clinic", "medical"])
        v = release.snapshot("medical")
        release._write_pointer({"stable": v, "canary": None, "canary_percent": 0, "history": []}, "medical")
        check("medical snapshot lives in its own folder",
              os.path.exists(os.path.join(tmp, "agents", "medical", "releases", "0.1.0", "MANIFEST.json"))
              and not os.path.exists(os.path.join(tmp, "releases", "0.1.0")))
        check("medical pointer is separate from clinic", release.pick_version("c1", agent_id="medical") == "0.1.0"
              and release.pick_version("c1", agent_id="clinic") is None)
        mcfg, mprompt, _ = release.load_release("0.1.0", agent_id="medical")
        check("medical release loads its own prompt", mprompt == "medical prompt")
        try:
            release.snapshot("medical")
            check("a published release is immutable", False)
        except SystemExit:
            check("a published release is immutable", True)
        try:
            release.cli(["status", "--agent", "nope"])
            check("release tool rejects an unknown agent", False)
        except profiles.UnknownAgent:
            check("release tool rejects an unknown agent", True)

        # 5. Web endpoint: bad id is 404 and uses no call slot, draft agent is 404, default still works.
        from fastapi.testclient import TestClient
        os.environ.update({"WEB_CALL_ENABLED": "1", "WEB_CALL_MAX_CONCURRENT": "5", "WEB_CALL_MAX_SECONDS": "1",
                           "LIVEKIT_URL": "wss://example.invalid", "LIVEKIT_API_KEY": "devkey", "LIVEKIT_API_SECRET": "s" * 40})
        from ava.backend.app import create_app
        from ava.backend.store import SQLiteStore
        c = TestClient(create_app(store=SQLiteStore(":memory:")))
        h = {"x-forwarded-for": "7.7.7.7"}
        check("agents list shows live agents only", c.get("/api/agents").json() == {"agents": [{"id": "clinic", "title": "Clinic", "status": "live"}]})
        check("unknown agent is 404", c.post("/api/web_call", json={"agent_id": "nope"}, headers=h).status_code == 404)
        check("draft agent is 404 on the public endpoint", c.post("/api/web_call", json={"agent_id": "medical"}, headers=h).status_code == 404)
        used = len(c.app.state.web_gate.active)
        check("rejected ids use no call slot", used == 0, str(used))
        r = c.post("/api/web_call", headers=h)
        check("no body still gets the default agent", r.status_code == 200 and r.json()["agent_id"] == "clinic", r.text[:120])
    finally:
        profiles.ROOT, profiles.REGISTRY, release.ROOT = saved
        shutil.rmtree(tmp, ignore_errors=True)

    # 6. The browser token carries the agent_id in the dispatch metadata and still dispatches the same worker.
    import jwt
    from ava.backend.webcall import make_token
    c = jwt.decode(make_token("web-abc", "devkey", "s" * 40, 240), "s" * 40, algorithms=["HS256"], options={"verify_aud": False})
    ag = c["roomConfig"]["agents"][0]
    check("token dispatches the same worker name", ag["agentName"] == "clinic-ava")
    check("token carries the default agent_id", json.loads(ag["metadata"]) == {"agent_id": "clinic"})
    print("agent profiles OK")


if __name__ == "__main__":
    main()
