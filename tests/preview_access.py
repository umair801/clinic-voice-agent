"""Private access to a draft agent and to a chosen EHR mode. Run: python -c "from tests.preview_access import main; main()"

Proves: without the preview key a draft agent looks like one that does not exist, a wrong key is the same, the right key gets a token whose
dispatch metadata carries the agent and the EHR mode, and an EHR mode is refused without the key or when it is not a known mode."""
import json
import os


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f": {detail}" if detail and not cond else ""))
    if not cond:
        raise SystemExit(1)


def main():
    import jwt
    from fastapi.testclient import TestClient
    from ava import profiles
    from ava.backend.app import create_app
    from ava.backend.store import SQLiteStore

    os.environ.update({"WEB_CALL_ENABLED": "1", "WEB_CALL_MAX_CONCURRENT": "20", "WEB_CALL_PER_IP_PER_HOUR": "50", "WEB_CALL_DAILY_CAP": "99",
                       "LIVEKIT_URL": "wss://example.invalid", "LIVEKIT_API_KEY": "devkey", "LIVEKIT_API_SECRET": "s" * 40,
                       "PREVIEW_KEY": "k" * 24})
    check("receptionist is a draft in the registry", profiles.get("receptionist")["status"] == "draft")
    c = TestClient(create_app(store=SQLiteStore(":memory:")))
    h = {"x-forwarded-for": "7.7.7.7"}

    def post(body, key=None):
        hh = dict(h)
        if key is not None:
            hh["x-preview-key"] = key
        return c.post("/api/web_call", json=body, headers=hh)

    check("draft agent without a key is 404", post({"agent_id": "receptionist"}).status_code == 404)
    check("draft agent with a wrong key is 404", post({"agent_id": "receptionist"}, "wrong").status_code == 404)
    check("draft agent is not in the public list", "receptionist" not in [a["id"] for a in c.get("/api/agents").json()["agents"]])
    r = post({"agent_id": "receptionist"}, "k" * 24)
    check("draft agent with the right key gets a call", r.status_code == 200 and r.json()["agent_id"] == "receptionist", r.text[:120])
    check("the key is never echoed", "k" * 24 not in r.text)
    meta = json.loads(jwt.decode(r.json()["token"], "s" * 40, algorithms=["HS256"], options={"verify_aud": False})["roomConfig"]["agents"][0]["metadata"])
    check("no ehr_mode in the token unless asked", meta == {"agent_id": "receptionist"}, str(meta))
    r = post({"agent_id": "receptionist", "ehr_mode": "athena_sandbox"}, "k" * 24)
    meta = json.loads(jwt.decode(r.json()["token"], "s" * 40, algorithms=["HS256"], options={"verify_aud": False})["roomConfig"]["agents"][0]["metadata"])
    check("sandbox mode rides in the signed token", meta == {"agent_id": "receptionist", "ehr_mode": "athena_sandbox"}, str(meta))
    check("ehr_mode without the key is refused", post({"agent_id": "clinic", "ehr_mode": "athena_sandbox"}).status_code == 403)
    check("unknown ehr_mode is refused", post({"agent_id": "receptionist", "ehr_mode": "production"}, "k" * 24).status_code == 400)
    check("clinic still works with no key and no body", c.post("/api/web_call", headers=h).status_code == 200)
    os.environ.pop("PREVIEW_KEY")
    c2 = TestClient(create_app(store=SQLiteStore(":memory:")))
    check("no PREVIEW_KEY configured: nobody gets a draft agent, not even with an empty key",
          c2.post("/api/web_call", json={"agent_id": "receptionist"}, headers={**h, "x-preview-key": ""}).status_code == 404)
    check("metadata field reader", profiles.metadata_field('{"ehr_mode": "simulated"}', "ehr_mode") == "simulated"
          and profiles.metadata_field("nope", "ehr_mode") is None and profiles.metadata_field('{"ehr_mode": 5}', "ehr_mode") is None)
    print("preview access OK")


if __name__ == "__main__":
    main()
