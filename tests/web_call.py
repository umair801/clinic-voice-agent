"""Offline checks for the browser test line guard and token. Usage: python -c "from tests.web_call import main; main()" """
import os


def main():
    import jwt
    from ava.backend.webcall import WebCallGate, make_token

    # 1. Off by default.
    g = WebCallGate(env={})
    assert g.check("1.1.1.1") == (False, "disabled")

    # 2. Limits: concurrency, per-IP per hour, daily cap, release, expiry.
    t = [1_700_000_000.0]
    env = {"WEB_CALL_ENABLED": "1", "WEB_CALL_MAX_CONCURRENT": "2", "WEB_CALL_PER_IP_PER_HOUR": "3",
           "WEB_CALL_DAILY_CAP": "5", "WEB_CALL_MAX_SECONDS": "100"}
    g = WebCallGate(env=env, now=lambda: t[0])
    assert g.check("a") == (True, "")
    g.admit("a", "web-1"); g.admit("b", "web-2")
    assert g.check("c") == (False, "busy")
    assert g.release("web-1") and not g.release("web-1")
    assert g.check("c") == (True, "")
    g.admit("a", "web-3"); g.release("web-3"); g.admit("a", "web-4"); g.release("web-4")
    assert g.check("a") == (False, "ip_limit"), g.check("a")
    t[0] += 3700                      # an hour later the per-IP window has cleared, and calls expired
    assert g.check("a")[0] is True
    for i in range(3):                # daily cap: 5 admitted so far today is not reached until here
        g.admit(f"x{i}", f"web-d{i}"); g.release(f"web-d{i}")
    assert g.check("zz") == (False, "daily_cap"), g.check("zz")
    t[0] += 86400
    assert g.check("zz")[0] is True   # new day resets the count

    # 3. Token: join-only for one room, dispatches the clinic agent, short lifetime.
    tok = make_token("web-abc", "devkey", "s" * 40, 240)
    c = jwt.decode(tok, "s" * 40, algorithms=["HS256"], options={"verify_aud": False})
    assert c["video"]["room"] == "web-abc" and c["video"]["roomJoin"] is True
    assert not c["video"].get("roomAdmin") and not c["video"].get("roomCreate")
    assert c["roomConfig"]["agents"][0]["agentName"] == "clinic-ava"
    assert c["exp"] - c["nbf"] <= 400
    assert c["sub"].startswith("web-caller")

    # 4. Endpoints: off by default, then on with fake keys, never returns the secret.
    from fastapi.testclient import TestClient
    os.environ.pop("WEB_CALL_ENABLED", None)
    from ava.backend.app import create_app
    from ava.backend.store import SQLiteStore
    c1 = TestClient(create_app(store=SQLiteStore(":memory:")))
    assert c1.get("/api/web_call/status").json()["enabled"] is False
    assert c1.post("/api/web_call").status_code == 503
    os.environ.update({"WEB_CALL_ENABLED": "1", "WEB_CALL_MAX_CONCURRENT": "1", "WEB_CALL_MAX_SECONDS": "1",
                       "LIVEKIT_URL": "wss://example.invalid", "LIVEKIT_API_KEY": "devkey",
                       "LIVEKIT_API_SECRET": "s" * 40})
    c2 = TestClient(create_app(store=SQLiteStore(":memory:")))
    r = c2.post("/api/web_call", headers={"x-forwarded-for": "9.9.9.9"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["room"].startswith("web-") and "s" * 40 not in r.text and set(body) >= {"url", "token", "room"}
    assert c2.post("/api/web_call", headers={"x-forwarded-for": "8.8.8.8"}).status_code == 429   # busy
    assert c2.post("/api/web_call/end", json={"room": "web-notmine"}).json() == {"ended": False}
    print("web call guard, token and endpoints OK")


if __name__ == "__main__":
    main()
