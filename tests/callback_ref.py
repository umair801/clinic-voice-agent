"""Offline checks: a callback gets a spoken reference only when it was really saved. Usage: python -c "from tests.callback_ref import main; main()" """
import asyncio
import os
import re


class _Boom:
    async def callback(self, call_id, note):
        raise RuntimeError("down")


def main():
    from ava.backend.booking import BookingService, Fault
    from ava.backend.client import InProcessTransport
    from ava.backend.store import SQLiteStore, new_callback_ref
    from ava.core import AvaCore
    from ava.voice.agent import callback_say

    assert re.fullmatch(r"CB\d{5}", new_callback_ref())

    store = SQLiteStore(":memory:")
    core = AvaCore("call_t1", InProcessTransport(BookingService(store, Fault())))
    res = asyncio.run(core.leave_callback("caller went silent with work pending"))
    assert res["ok"] and re.fullmatch(r"CB\d{5}", res["ref"]), res
    row = store.db.execute("SELECT note FROM callbacks").fetchone()[0]
    assert row.startswith(f"[{res['ref']}] "), row           # the reference is findable in the stored note
    say = callback_say(res)
    assert " ".join(res["ref"]) in say and "one character at a time" in say

    bad = asyncio.run(AvaCore("call_t2", _Boom()).leave_callback("x"))
    assert bad == {"ok": False, "ref": None}
    assert "could not save" in callback_say(bad) and "reference" not in callback_say(bad).lower()

    from fastapi.testclient import TestClient
    from ava.backend.app import create_app
    os.environ.pop("BACKEND_API_KEY", None)
    c = TestClient(create_app(store=SQLiteStore(":memory:")))
    r = c.post("/v1/callback", json={"call_id": "call_t3", "note": "n"}).json()
    assert r["logged"] is True and re.fullmatch(r"CB\d{5}", r["ref"]), r
    print("callback reference checks OK")


if __name__ == "__main__":
    main()
