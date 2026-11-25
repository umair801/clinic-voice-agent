"""Orchestration evals for the medical practice receptionist. LABEL: they run the REAL core (state, identity gate, audit log,
redaction, output guard, EHR booking policy) against an in-memory mock EHR with fault injection, and a deterministic stub in
place of the LLM. They prove the rails hold. They do NOT measure LLM wording, voice latency or cost, and they say nothing about
the athenahealth sandbox's behavior beyond what the mock was built to copy (slow writes that still land).

Run:  python -c "from evals.receptionist import main; main()"
"""
import asyncio
import glob
import json
import os
import tempfile

from ava.ehr.base import WriteStatus
from ava.ehr.mock import MockEHR, simulated_practice
from ava.hipaa.audit import AuditLog
from ava.receptionist.core import MessageStore, ReceptionistCore
from ava.receptionist.guard import ReceptionistGuard, violations
from ava.receptionist.state import Step
from ava.telemetry import CallLogger

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRACTICE = json.load(open(os.path.join(ROOT, "agents", "receptionist", "practice.json")))
GARY = ("Gary", "Testone", "1948-04-18")
DORRIE = ("Dorrie", "Testtwo", "1949-11-23")


def make(ehr=None, call_id="eval", d=None):
    d = d or tempfile.mkdtemp()
    ehr = ehr or MockEHR()
    audit = AuditLog(os.path.join(d, "audit.jsonl"), salt="eval-salt")
    core = ReceptionistCore(call_id, ehr, audit=audit, logger=CallLogger(call_id, log_dir=d, alert_webhook=""),
                            messages=MessageStore(os.path.join(d, "messages")), practice=PRACTICE, log_dir=d)
    core.mark_greeted()
    return core, ehr, audit, d


async def verified(core, who=GARY):
    return await core.verify_identity(*who)


def guard_out(state, text):
    g = ReceptionistGuard(state)
    return g.feed(text + " ") + g.flush()


async def s_verified_caller_reaches_help_step():
    core, *_ = make()
    r = await verified(core)
    return r["status"] == "verified" and core.state.step == Step.HELP, f"status {r['status']}, step {core.state.step.value}"


async def s_wrong_details_same_message_then_lockout():
    core, *_ = make()
    a = await core.verify_identity("Gary", "Testone", "1950-01-01")
    b = await core.verify_identity("Gary", "Wrongname", "1948-04-18")
    c = await core.verify_identity("Gary", "Testone", "1950-02-02")
    d = await core.verify_identity(*GARY)          # right details after lockout must still be refused
    same = a["say"].split(" Let")[0] == b["say"].split(" Let")[0]
    leak = any(w in (a["say"] + b["say"]).lower() for w in ("birth", "dob", "last name", "first name"))
    ok = same and not leak and c["status"] == "locked" and d["status"] == "locked" and not core.state.verified
    return ok, f"same message {same}, no field named {not leak}, locked after 3 {c['status']}, refused after lock {d['status']}"


async def s_two_matches_need_zip():
    core, *_ = make()
    r = await core.verify_identity("Jovin", "Testthree", "1990-01-01")
    r2 = await core.add_verification_detail("90210")
    return r["status"] == "need_more" and r2["status"] == "verified" and core.state.gate.patient.patient_id == "P4", f"{r['status']} then {r2['status']}"


async def s_unverified_caller_gets_nothing():
    core, ehr, audit, d = make()
    sl = await ehr.open_slots("1", "8", "2026-11-25", "2026-12-25")
    outs = [await core.list_my_appointments(), await core.find_open_slots(), await core.book_visit("S0"),
            await core.cancel_visit("S0"), await core.reschedule_visit("S0", "S1")]
    refused = all(o.get("ok") is False or o.get("status") == "not_done" or o.get("error") for o in outs)
    states = [(await ehr.read_slot(x.slot_id))["state"] for x in sl]
    untouched = all(st == "open" for st in states)
    return refused and untouched and ehr.writes_sent == 0, f"all refused {refused}, no write reached the EHR {ehr.writes_sent == 0}"


async def s_third_party_gets_general_info_and_messages_only():
    core, ehr, audit, d = make()
    await core.caller_is_not_the_patient()
    a = await core.list_my_appointments()
    b = await core.book_visit("S0")
    info = await core.practice_info("what are your hours")
    msg = await core.take_message("Please tell Gary his daughter called.")
    ok = a.get("ok") is False and b.get("ok") is False and "8 AM" in info["info"] and msg["ok"] and core.state.step == Step.THIRD_PARTY
    return ok and ehr.writes_sent == 0, f"phi refused {a.get('ok') is False}, info given, message saved {msg['ok']}, step {core.state.step.value}"


async def s_book_confirmed_by_read():
    core, ehr, *_ = make()
    await verified(core)
    sl = (await core.find_open_slots())["slots"]
    r = await core.book_visit(sl[0]["slot_id"])
    rec = await ehr.read_slot(sl[0]["slot_id"])
    ok = r["status"] == "booked" and rec["state"] == "booked" and rec["patient_id"] == "P1" and len(core.state.booked()) == 1
    return ok, f"status {r['status']}, record {rec['state']} for {rec['patient_id']}"


async def s_slot_not_offered_is_refused():
    core, ehr, *_ = make()
    await verified(core)
    r = await core.book_visit("S5")
    return r["status"] == "not_done" and ehr.writes_sent == 0, f"{r['status']}, writes {ehr.writes_sent}"


async def s_slow_write_is_pending_never_booked_then_reconciles_with_one_write():
    ehr = MockEHR(commit_delay_s=0.2, submit_timeout_s=0.03, reconcile_window_s=0.05)
    core, ehr, *_ = make(ehr)
    await verified(core)
    sl = (await core.find_open_slots())["slots"]
    r = await core.book_visit(sl[0]["slot_id"])
    blocked = guard_out(core.state, "You're all booked for December first.")
    end_blocked = core.end_call_check().allowed is False
    pend = core.state.has_pending_work()
    left = await core.refresh_pending(window_s=2.0)
    await ehr.settle()
    ok = (r["status"] == "pending" and "booked" not in blocked.lower() and end_blocked and pend and left == 0
          and len(core.state.booked()) == 1 and ehr.writes_sent == 1)
    return ok, f"first {r['status']}, false claim replaced {'booked' not in blocked.lower()}, end blocked {end_blocked}, later {len(core.state.booked())} booked, writes {ehr.writes_sent}"


async def s_repeated_book_request_does_not_double_book():
    core, ehr, *_ = make()
    await verified(core)
    sl = (await core.find_open_slots())["slots"]
    await core.book_visit(sl[0]["slot_id"])
    core.on_user_turn("yes book it")
    r = await core.book_visit(sl[0]["slot_id"])
    return r["status"] == "booked" and ehr.writes_sent == 1 and len(core.state.ops) == 1, f"writes {ehr.writes_sent}, ops {len(core.state.ops)}"


async def s_one_change_per_caller_turn():
    core, ehr, *_ = make()
    await verified(core)
    sl = (await core.find_open_slots())["slots"]
    a = await core.book_visit(sl[0]["slot_id"])
    b = await core.book_visit(sl[1]["slot_id"])
    core.on_user_turn("yes the second one too")
    c = await core.book_visit(sl[1]["slot_id"])
    return a["status"] == "booked" and b["status"] == "not_done" and c["status"] == "booked" and ehr.writes_sent == 2, f"{a['status']}, {b['status']}, then {c['status']}"


async def s_cannot_cancel_someone_elses_visit():
    ehr = MockEHR()
    gary, e1, *_ = make(ehr, "callA")
    await verified(gary)
    sl = (await gary.find_open_slots())["slots"]
    await gary.book_visit(sl[0]["slot_id"])
    dorrie, e2, *_ = make(ehr, "callB")
    await verified(dorrie, DORRIE)
    r = await dorrie.cancel_visit(sl[0]["slot_id"])
    rec = await ehr.read_slot(sl[0]["slot_id"])
    return r["status"] == "not_done" and rec["state"] == "booked", f"{r['status']}, record still {rec['state']}"


async def s_cancel_confirmed_and_claim_allowed_only_after():
    core, ehr, *_ = make()
    await verified(core)
    sl = (await core.find_open_slots())["slots"]
    await core.book_visit(sl[0]["slot_id"])
    core.on_user_turn("cancel my visit")
    mine = await core.list_my_appointments()
    early = guard_out(core.state, "Your visit has been cancelled.")
    r = await core.cancel_visit(mine["appointments"][0]["appointment_id"])
    late = guard_out(core.state, "Your visit has been cancelled.")
    rec = await ehr.read_slot(sl[0]["slot_id"])
    ok = r["status"] == "cancelled" and rec["state"] == "cancelled" and "cancelled" not in early and "cancelled" in late
    return ok, f"cancel {r['status']}, early claim replaced {'cancelled' not in early}, late claim allowed {'cancelled' in late}"


async def s_reschedule_books_new_then_cancels_old():
    core, ehr, *_ = make()
    await verified(core)
    sl = (await core.find_open_slots())["slots"]
    await core.book_visit(sl[0]["slot_id"])
    core.on_user_turn("move it to the next one")
    await core.find_open_slots()
    new = [x for x in core._offered.values() if x.slot_id != sl[0]["slot_id"]][0]
    r = await core.reschedule_visit(sl[0]["slot_id"], new.slot_id)
    a, b = await ehr.read_slot(sl[0]["slot_id"]), await ehr.read_slot(new.slot_id)
    return r["status"] == "booked" and a["state"] == "cancelled" and b["state"] == "booked", f"{r['status']}, old {a['state']}, new {b['state']}"


async def s_reschedule_into_taken_slot_keeps_old_visit():
    ehr = MockEHR()
    gary, *_ = make(ehr, "callA")
    await verified(gary)
    sl = (await gary.find_open_slots())["slots"]
    await gary.book_visit(sl[0]["slot_id"])
    await ehr.book(ehr.slots[sl[1]["slot_id"]], [p for p in ehr.patients if p.patient_id == "P2"][0], "other")
    gary.on_user_turn("move it to the second one")
    gary._offered[sl[1]["slot_id"]] = ehr.slots[sl[1]["slot_id"]]
    r = await gary.reschedule_visit(sl[0]["slot_id"], sl[1]["slot_id"])
    old = await ehr.read_slot(sl[0]["slot_id"])
    return r["status"] == "slot_taken" and old["state"] == "booked", f"{r['status']}, old visit {old['state']}"


async def s_reschedule_old_cancel_unconfirmed_is_told_honestly():
    ehr = MockEHR(cancel_commit_delay_s=0.3, submit_timeout_s=0.03, reconcile_window_s=0.05)
    core, ehr, *_ = make(ehr)
    await verified(core)
    sl = (await core.find_open_slots())["slots"]
    await core.book_visit(sl[0]["slot_id"])
    core.on_user_turn("move it")
    await core.find_open_slots()
    new = [x for x in core._offered.values() if x.slot_id != sl[0]["slot_id"]][0]
    r = await core.reschedule_visit(sl[0]["slot_id"], new.slot_id)
    ok = r["status"] == "pending" and r.get("new_visit") == "booked" and "NEW time is confirmed" in r["say"] and core.state.has_pending_work()
    await ehr.settle()
    return ok, f"status {r['status']}, new visit {r.get('new_visit')}, old {r.get('old_visit')}"


async def s_urgent_words_stop_scheduling():
    core, *_ = make()
    d = core.on_user_turn("I think I'm having chest pain and I need an appointment")
    calm = make()[0].on_user_turn("I would like a physical next month")
    ok = core.state.step == Step.URGENT and "911" in d["instruction"] and d["tier"] == "large" and calm["tier"] == "small"
    return ok and "URGENT" not in calm["instruction"], f"step {core.state.step.value}, routine request not flagged {calm['tier'] == 'small'}"


async def s_end_call_needs_goodbye_and_no_pending_work():
    core, *_ = make()
    core.on_user_turn("what are your hours")
    a = core.end_call_check().allowed
    core.on_user_turn("okay thanks, goodbye")
    core.agent_said_goodbye()
    b = core.end_call_check().allowed
    ehr = MockEHR(commit_delay_s=0.2, submit_timeout_s=0.03, reconcile_window_s=0.05)
    c2, e2, *_ = make(ehr)
    await verified(c2)
    sl = (await c2.find_open_slots())["slots"]
    await c2.book_visit(sl[0]["slot_id"])
    c2.on_user_turn("goodbye")
    c2.agent_said_goodbye()
    c = c2.end_call_check().allowed
    await e2.settle()
    return (not a) and b and (not c), f"no goodbye {a}, goodbye {b}, goodbye over pending change {c}"


async def s_transfer_only_when_asked_or_failing():
    core, *_ = make()
    a = await core.request_human_transfer("just because")
    core.on_user_turn("can I talk to a real person")
    b = await core.request_human_transfer("caller asked")
    return a["ok"] is False and b["ok"] is True, f"unasked {a['ok']}, asked {b['ok']}"


async def s_message_text_stays_out_of_call_logs_and_audit():
    core, ehr, audit, d = make()
    await verified(core)
    secret = "Please call my daughter Anna on 276-444-1589 about the blood test"
    r = await core.take_message(secret)
    logs = "".join(open(p).read() for p in glob.glob(os.path.join(d, "*.jsonl")))
    stored = "".join(open(p).read() for p in glob.glob(os.path.join(d, "messages", "*.jsonl")))
    aud = open(audit.path).read()
    ok = r["ok"] and "444-1589" in stored and "444-1589" not in logs and "Anna" not in logs and "blood test" not in aud
    return ok, f"saved {r['ok']}, text in message store only {'444-1589' in stored and '444-1589' not in logs}"


async def s_call_logs_redact_free_text():
    core, *_ = make()
    await verified(core)
    core.log.event("note", text="x", detail="Gary Testone called from 276-444-1589, born April 18 1948, mail gary@x.com")
    logs = "".join(open(p).read() for p in glob.glob(os.path.join(core.log.dir, "*.jsonl")))
    bad = [w for w in ("Gary", "Testone", "444-1589", "1948", "gary@x.com") if w in logs]
    return not bad, f"leaked: {bad}"


async def s_audit_chain_valid_and_holds_no_personal_details():
    core, ehr, audit, d = make()
    await verified(core)
    sl = (await core.find_open_slots())["slots"]
    await core.book_visit(sl[0]["slot_id"])
    core.on_user_turn("what are my visits")
    await core.list_my_appointments()
    raw = open(audit.path).read()
    bad = [w for w in ("Gary", "Testone", "1948", "P1", "02101") if w in raw]
    ok_chain = audit.verify() == (True, None)
    return ok_chain and not bad and len(raw.splitlines()) >= 4, f"chain valid {ok_chain}, entries {len(raw.splitlines())}, leaked {bad}"


async def s_details_not_spoken_to_unverified_caller():
    core, *_ = make()
    a = guard_out(core.state, "Your next appointment is on December first at 9 AM.")
    await verified(core)
    b = guard_out(core.state, "Your next appointment is on December first at 9 AM.")
    return "December" not in a and "December" in b, f"unverified blocked {'December' not in a}, verified allowed {'December' in b}"


async def s_language_does_not_flip_on_a_single_word():
    core, *_ = make()
    core.on_user_turn("Sí")
    one = core.state.language
    core.on_user_turn("Spanish please")
    two = core.state.language
    core.on_user_turn("English please")
    return one == "en" and two == "es" and core.state.language == "en", f"after Sí {one}, after request {two}, back {core.state.language}"


async def s_fallback_model_gets_the_same_step_from_state_alone():
    core, ehr, *_ = make()
    await verified(core)
    a = core.state.instruction()
    twin = ReceptionistCore("eval", ehr, state=core.state, practice=PRACTICE, log_dir=tempfile.mkdtemp())
    b = twin.state.instruction()
    return a == b and "list_my_appointments" in a and core.state.step == Step.HELP, "a second core built on the same state resumes at the same step"


async def s_simulated_demo_practice_supports_the_full_flow():
    ehr = simulated_practice("2026-11-25", commit_delay_s=0.0, read_delay_s=0.0, poll_interval_s=0.01, reconcile_window_s=0.2)
    core, ehr, *_ = make(ehr)
    await verified(core)
    mine = await core.list_my_appointments()
    has_one = len(mine["appointments"]) == 1
    core.on_user_turn("move it to something earlier")
    slots = (await core.find_open_slots())["slots"]
    r = await core.reschedule_visit(mine["appointments"][0]["appointment_id"], slots[0]["slot_id"])
    core.on_user_turn("and what do I have now")
    after = await core.list_my_appointments()
    ok = has_one and r["status"] == "booked" and len(after["appointments"]) == 1 and after["appointments"][0]["appointment_id"] == slots[0]["slot_id"]
    return ok, f"seeded visit found {has_one}, reschedule {r['status']}, now holds the new time {ok}"


async def s_early_goodbye_never_repeats_the_question():
    """Found in simulated test calls: a model reply "Is there anything else I can help you with? Goodbye." became the question twice."""
    core, *_ = make()
    await verified(core)
    out = guard_out(core.state, "Is there anything else I can help you with? Thank you, goodbye.")
    out2 = guard_out(core.state, "Thank you, goodbye. Take care, goodbye.")
    q = "anything else i can help"
    ok = out.lower().count(q) == 1 and out2.lower().count(q) == 1 and "goodbye" not in (out + out2).lower()
    return ok, f"first {out!r}, second {out2!r}"


SCENARIOS = [(n[2:], f) for n, f in list(globals().items()) if n.startswith("s_") and callable(f)]


def run_all(verbose=True):
    results = []
    for name, fn in SCENARIOS:
        try:
            ok, detail = asyncio.run(fn())
        except Exception as e:
            ok, detail = False, f"EXCEPTION {type(e).__name__}: {e}"
        results.append({"name": name, "passed": bool(ok), "detail": detail})
        if verbose:
            print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")
    return results


def main(write=True):
    res = run_all()
    n = sum(r["passed"] for r in res)
    print(f"\n{n}/{len(res)} receptionist evals passed (mock EHR, stub model; see module docstring)")
    if write:
        os.makedirs(os.path.join(ROOT, "results"), exist_ok=True)
        json.dump({"label": "receptionist orchestration evals, mock EHR, stub model", "passed": n, "total": len(res), "results": res},
                  open(os.path.join(ROOT, "results", "evals_receptionist.json"), "w"), indent=2)
    return n == len(res)


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
