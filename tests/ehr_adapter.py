"""Offline tests for the EHR layer: mock with slow writes, and the athena adapter on httpx.MockTransport."""
import asyncio
import httpx

from ava.ehr.base import WriteStatus, TransportTimeout
from ava.ehr.mock import MockEHR, PATIENTS
from ava.ehr.athena import AthenaEHR, AthenaError

FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


async def mock_tests():
    p = PATIENTS[0]
    # fast book
    m = MockEHR()
    s = (await m.open_slots("1", "8", "2026-12-01", "2026-12-07"))[0]
    r = await m.book(s, p, "k1")
    check("fast book is booked", r.status == WriteStatus.BOOKED)
    r2 = await m.book(s, p, "k1")
    check("same key returns same answer, one write", r2.status == WriteStatus.BOOKED and m.writes_sent == 1)

    # slow commit: timeout becomes pending then reconciled by read, exactly one write
    m = MockEHR(commit_delay_s=0.15, submit_timeout_s=0.03, reconcile_window_s=0.05)
    s = (await m.open_slots("1", "8", "2026-12-01", "2026-12-07"))[0]
    r = await m.book(s, p, "k2")
    check("slow write is pending, not failed or booked", r.status == WriteStatus.PENDING)
    m.reconcile_window_s = 1.0
    r = await m.book(s, p, "k2")
    await m.settle()
    check("pending key reconciles to booked by reading", r.status == WriteStatus.BOOKED)
    check("pending key never resent the write", m.writes_sent == 1)

    # slot taken by someone else
    m = MockEHR()
    s = (await m.open_slots("1", "8", "2026-12-01", "2026-12-07"))[0]
    await m.book(s, PATIENTS[1], "a")
    r = await m.book(s, p, "b")
    check("taken slot is slot_taken, never booked", r.status == WriteStatus.SLOT_TAKEN)

    # refused write is failed
    m = MockEHR()
    s = (await m.open_slots("1", "8", "2026-12-01", "2026-12-07"))[1]
    m.refuse_next = True
    r = await m.book(s, p, "c")
    check("refused write is failed", r.status == WriteStatus.FAILED)

    # cancel then reschedule order
    m = MockEHR()
    sl = await m.open_slots("1", "8", "2026-12-01", "2026-12-07")
    await m.book(sl[0], p, "d")
    new, old = await m.reschedule(sl[0].slot_id, sl[1], p, "e")
    check("reschedule books new and cancels old", new.status == WriteStatus.BOOKED and old.status == WriteStatus.CANCELLED
          and m.state[sl[0].slot_id]["state"] == "cancelled" and m.state[sl[1].slot_id]["state"] == "booked")
    # reschedule into a taken slot keeps the old booking
    m = MockEHR()
    sl = await m.open_slots("1", "8", "2026-12-01", "2026-12-07")
    await m.book(sl[0], p, "f")
    await m.book(sl[1], PATIENTS[1], "g")
    new, old = await m.reschedule(sl[0].slot_id, sl[1], p, "h")
    check("failed reschedule keeps the old appointment", new.status != WriteStatus.BOOKED and old is None and m.state[sl[0].slot_id]["state"] == "booked")

    # find patients
    m = MockEHR()
    check("name+dob finds one", len(await m.find_patients("Testone", "Gary", "1948-04-18")) == 1)
    check("wrong dob finds none", len(await m.find_patients("Testone", "Gary", "1948-04-19")) == 0)
    sl = (await m.open_slots("1", "8", "2026-12-01", "2026-12-07"))[0]
    await m.book(sl, p, "z")
    check("mock lists a patient's booked visits", [x.slot_id for x in await m.patient_appointments("P1", "2026-11-25", "2027-11-25")] == [sl.slot_id])
    check("ambiguous name+dob returns two", len(await m.find_patients("Testthree", "Jovin", "1990-01-01")) == 2)


def athena_with(handler, **kw):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return AthenaEHR("https://x.test/v1/1", "https://x.test/token", "id", "secret", http=http,
                     max_read_retries=2, **kw)


async def athena_tests():
    calls = {"token": 0, "put": 0, "get": 0}
    raw = {"patients": [{"patientid": 9, "firstname": "Gary", "lastname": "Testone", "dob": "04/18/1948", "zip": "02101",
                         "address1": "SECRET ST", "driverslicense": "X123", "balances": [1]}]}

    def h(req):
        u = str(req.url)
        if u.endswith("/token"):
            calls["token"] += 1
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        if req.method == "GET" and "/patients" in u:
            calls["get"] += 1
            return httpx.Response(200, json=raw)
        if req.method == "GET" and "/appointments/open" in u:
            return httpx.Response(200, json={"appointments": [{"appointmentid": 5, "date": "12/01/2026", "starttime": "08:00",
                                  "providerid": 2, "departmentid": 1, "appointmenttypeid": 8}]})
        if req.method == "GET" and "/patients/9/appointments" in u:
            return httpx.Response(200, json={"appointments": [
                {"appointmentid": 5, "date": "12/01/2026", "starttime": "08:00", "providerid": 2, "departmentid": 1, "appointmentstatus": "f", "patientid": 9,
                 "patientphone": "SECRET"},
                {"appointmentid": 6, "date": "12/02/2026", "starttime": "09:00", "providerid": 2, "departmentid": 1, "appointmentstatus": "x"}]})
        if req.method == "GET" and "/appointments/5" in u:
            return httpx.Response(200, json=[{"appointmentstatus": "f", "patientid": 9}])
        return httpx.Response(404)

    a = athena_with(h)
    ps = await a.find_patients("Testone", "Gary", "1948-04-18")
    check("athena patient is minimized", len(ps) == 1 and ps[0].dob == "1948-04-18" and not hasattr(ps[0], "address1"))
    await a.find_patients("Testone")
    await a.open_slots("1", "8", "2026-12-01", "2026-12-07")
    check("token cached across calls", calls["token"] == 1)
    sl = (await a.open_slots("1", "8", "2026-12-01", "2026-12-07"))[0]
    check("slot parsed", sl.slot_id == "5" and sl.date == "2026-12-01")
    mine = await a.patient_appointments("9", "2026-11-25", "2027-11-25")
    check("my appointments: only booked ones, minimal fields", [m.slot_id for m in mine] == ["5"] and mine[0].date == "2026-12-01"
          and not hasattr(mine[0], "patientphone"))
    check("read maps f to booked", (await a.read_slot("5"))["state"] == "booked")

    # read retry on 503
    n = {"i": 0}

    def h2(req):
        if str(req.url).endswith("/token"):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        n["i"] += 1
        return httpx.Response(503) if n["i"] < 3 else httpx.Response(200, json=raw)
    a = athena_with(h2)
    check("read retries 5xx then works", len(await a.find_patients("Testone")) == 1 and n["i"] == 3)

    # write timeout: one PUT only, then pending, reconcile by GET
    state = {"put": 0, "booked": False}

    def h3(req):
        u = str(req.url)
        if u.endswith("/token"):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        if req.method == "PUT":
            state["put"] += 1
            raise httpx.ReadTimeout("slow", request=req)
        if "/appointments/5" in u:
            return httpx.Response(200, json=[{"appointmentstatus": "f" if state["booked"] else "o", "patientid": 9 if state["booked"] else ""}])
        return httpx.Response(404)
    a = athena_with(h3, reconcile_window_s=0.3, poll_interval_s=0.05)
    from ava.ehr.base import Slot, Patient
    slot, pat = Slot("5", "2026-12-01", "08:00", "2", "1", "8"), Patient("9", "Gary", "Testone", "1948-04-18")
    r = await a.book(slot, pat, "k")
    check("athena write timeout is pending", r.status == WriteStatus.PENDING)
    state["booked"] = True
    r = await a.book(slot, pat, "k")
    check("pending reconciles to booked", r.status == WriteStatus.BOOKED)
    check("exactly one PUT was sent", state["put"] == 1)

    # from_env needs all settings, error does not leak values
    try:
        AthenaEHR.from_env({"ATHENA_CLIENT_ID": "abc"})
        check("from_env rejects missing settings", False)
    except AthenaError as e:
        check("from_env rejects missing settings, no values leaked", "abc" not in str(e))


async def main_async():
    await mock_tests()
    await athena_tests()


def main():
    asyncio.run(main_async())
    print(f"{'OK' if not FAILS else 'FAILED'}: {len(FAILS)} failure(s)")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
