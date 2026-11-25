import asyncio, os, tempfile, time, json
from ava.ehr.mock import MockEHR
from ava.hipaa.identity import IdentityGate, normalize_dob
from ava.hipaa.redact import redact
from ava.hipaa.audit import AuditLog
from ava.hipaa.retention import purge

FAILS = []


def check(n, c):
    print(("PASS " if c else "FAIL ") + n)
    if not c:
        FAILS.append(n)


async def ident():
    for t, e in [("1948-04-18", "1948-04-18"), ("04/18/1948", "1948-04-18"), ("4-18-48", "1948-04-18"), ("April 18th, 1948", "1948-04-18"),
                 ("18 April 1948", "1948-04-18"), ("02/30/1990", None), ("garbage", None), ("13/01/1990", None), ("1/1/05", "2005-01-01")]:
        check(f"dob {t!r}", normalize_dob(t) == e)
    g = IdentityGate(MockEHR())
    r = await g.verify("Gary", "Testone", "April 18 1948")
    check("right details verify", r.status == "verified" and g.may_share_phi())
    g = IdentityGate(MockEHR())
    r1 = await g.verify("Gary", "Testone", "01/01/1950")
    r2 = await g.verify("Gary", "Wrong", "04/18/1948")
    check("wrong dob and wrong name give the same message", r1.message == r2.message and r1.status == "failed")
    check("message never names the wrong field", not any(w in r1.message.lower() for w in ("birth", "dob", "last name", "first name")))
    r3 = await g.verify("Gary", "Testone", "01/01/1950")
    check("locked after 3 misses", r3.status == "locked" and g.locked)
    r4 = await g.verify("Gary", "Testone", "04/18/1948")
    check("locked gate refuses even the right details", r4.status == "locked" and not g.may_share_phi())
    g = IdentityGate(MockEHR())
    r = await g.verify("Jovin", "Testthree", "01/01/1990")
    check("two matches ask for one more detail, not counted as a miss", r.status == "need_more" and g.attempts == 0)
    r = await g.add_detail("90210")
    check("zip picks the right person", r.status == "verified" and r.patient.patient_id == "P4")
    g = IdentityGate(MockEHR())
    await g.verify("Gary", "Testone", "04/18/1948")
    g.third_party()
    check("third party caller gets no PHI", not g.may_share_phi())


def red():
    t = "Hi I'm Gary Testone, born April 18 1948, call 276-444-1589 or gary@x.com, ssn 123-45-6789, id 1486358, code four one five two nine"
    o = redact(t, names=["Gary", "Testone"])
    for bad in ("Gary", "Testone", "1948", "444-1589", "gary@x.com", "123-45-6789", "1486358", "four one five"):
        check(f"redacted {bad}", bad not in o)
    check("ordinary words kept", "call" in o and "Hi" in o)
    check("empty ok", redact("") == "")


def aud():
    d = tempfile.mkdtemp()
    a = AuditLog(os.path.join(d, "audit.jsonl"), salt="s")
    a.record("call1", "verify_identity", subject="P1", outcome="verified")
    a.record("call1", "book", subject="P1", outcome="booked", slot="S1")
    check("chain verifies", a.verify() == (True, None))
    try:
        a.record("c", "x", name="Gary")
        check("personal field refused", False)
    except ValueError:
        check("personal field refused", True)
    raw = open(a.path).read()
    check("no raw ids in log", "P1" not in raw and "call1" not in raw)
    lines = raw.splitlines()
    e = json.loads(lines[0]); e["outcome"] = "changed"; lines[0] = json.dumps(e, sort_keys=True)
    open(a.path, "w").write("\n".join(lines) + "\n")
    ok, bad = a.verify()
    check("edit is detected", ok is False and bad == 1)
    b = AuditLog(os.path.join(d, "a2.jsonl")); b.record("c", "x"); b2 = AuditLog(b.path); b2.record("c", "y")
    check("log continues chain after restart", b2.verify() == (True, None))


def ret():
    d = tempfile.mkdtemp()
    old, new = os.path.join(d, "old.jsonl"), os.path.join(d, "new.jsonl")
    for p in (old, new):
        open(p, "w").write("x")
    t = time.time() - 40 * 86400
    os.utime(old, (t, t))
    check("dry run lists but keeps", purge(d, 30) == [old] and os.path.exists(old))
    purge(d, 30, apply=True)
    check("apply removes only old", not os.path.exists(old) and os.path.exists(new))


def main():
    asyncio.run(ident()); red(); aud(); ret()
    print(f"{'OK' if not FAILS else 'FAILED'}: {len(FAILS)} failure(s)")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
