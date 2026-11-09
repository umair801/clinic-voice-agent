"""Orchestration evals. LABEL: these run the REAL core (state, guards, language, routing, retrieval, backend,
idempotent booking) with a deterministic STUB MODEL standing in for the LLM. They prove the rails hold.
They do NOT measure LLM wording, voice latency, or cost. Those need real calls (see docs/BEFORE_AFTER.md).

Run:  python -c "from evals.run import main; main()"
"""
import asyncio
import json
import os
import tempfile

from ava import guards, language
from ava.backend.booking import BookingService, Fault
from ava.backend.client import InProcessTransport
from ava.backend.store import SQLiteStore
from ava.core import AvaCore
from ava.output_guard import violations
from ava.retrieval import KnowledgeBase
from ava.router import route
from ava.state import BookingStatus, CallState, Step
from ava.telemetry import CallLogger

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAMILY = [("self", "Maria", "1982-03-14"), ("child1", "Lucas", "2013-06-02"), ("child2", "Sofia", "2016-09-21")]
KB = KnowledgeBase()


def make(fault=None, call_id="eval", strict_turns=False):
    st = SQLiteStore()
    tr = InProcessTransport(BookingService(st, fault or Fault()))
    d = tempfile.mkdtemp()
    core = AvaCore(call_id, tr, kb=KB, logger=CallLogger(call_id, log_dir=d, alert_webhook=""), book_pause=0,
                   one_booking_per_turn=strict_turns)
    core.mark_greeted()
    return core, st


class StubModel:
    """Stateless on purpose: it reads ONLY core.state (like a fallback model would) and does the next action."""
    name = "stub"

    async def act(self, core):
        s = core.state
        step = s.step
        if step == Step.IDENTIFY:
            k, f, d = FAMILY[0]
            await core.identify_patient(k, f, d)
        elif step == Step.COLLECT_PATIENTS:
            if not s.expected_party_size:
                await core.set_party_size(len(FAMILY))
            for k, f, d in FAMILY:
                p = s.patient(k)
                if not p or not p.identified():
                    await core.identify_patient(k, f, d)
                    return step
        elif step == Step.CHOOSE_SLOT:
            slots = (await core.find_slots("pediatrics family", 6))["slots"]
            used = {p.slot for p in s.patients if p.slot}
            free = [x for x in slots if x["slot_id"] not in used]
            for p in s.active_patients():
                if not p.slot and p.status == BookingStatus.NONE.value:
                    await core.choose_slot(p.key, free.pop(0)["slot_id"])
                    return step
        elif step == Step.BOOK:
            await core.book_appointment(s.needs_booking()[0].key)
        elif step == Step.CONFIRM:
            await core.read_back_summary()
        return step


async def run_to_wrapup(core, model=None, max_steps=40, swap_at=None, models=None):
    model = model or StubModel()
    seen = []
    for i in range(max_steps):
        if swap_at is not None and i == swap_at and models:
            model = models  # fresh model object: nothing carried over except core.state
        step = await model.act(core)
        seen.append(step.value)
        if core.state.step == Step.WRAP_UP:
            break
    return seen


# ---------------------------------------------------------------- scenarios
async def s_family_with_lost_response():
    core, st = make(Fault(commit_then_timeout=1))
    await run_to_wrapup(core)
    rows = st.all_appointments()
    pats = sorted(r["patient_id"] for r in rows)
    ok = pats == ["pt_1001", "pt_1002", "pt_1003"] and len(core.state.booked()) == 3
    return ok, f"appointments for {pats}"


async def s_replay_of_bad_call_pattern():
    """The task's bad call: book times out, agent must not say booked, retry must not duplicate, children must be booked."""
    core, st = make(Fault(commit_then_timeout=1))
    core.state.add_patient("self", "Maria", "1982-03-14")
    await core.identify_patient("self", "Maria", "1982-03-14")
    await core.set_party_size(3)
    for k, f, d in FAMILY[1:]:
        await core.identify_patient(k, f, d)
    sl = (await core.find_slots("pediatrics", 3))["slots"]
    for (k, _, _), s in zip(FAMILY, sl):
        await core.choose_slot(k, s["slot_id"])
    first = await core.book_appointment("self")
    parent_rows = [r for r in st.all_appointments() if r["patient_id"] == "pt_1001"]
    for k in ("child1", "child2"):
        await core.book_appointment(k)
    by_patient = {}
    for r in st.all_appointments():
        by_patient[r["patient_id"]] = by_patient.get(r["patient_id"], 0) + 1
    ok = first["status"] == "booked" and len(parent_rows) == 1 and by_patient == {"pt_1001": 1, "pt_1002": 1, "pt_1003": 1}
    return ok, f"first status {first['status']} (reconciled), rows per patient {by_patient}"


async def s_timeout_before_commit():
    core, st = make(Fault(timeout_before_commit=1))
    await run_to_wrapup(core)
    return len(st.all_appointments()) == 3, f"{len(st.all_appointments())} rows"


async def s_outage_never_says_booked():
    core, st = make(Fault(timeout_before_commit=999))
    await run_to_wrapup(core, max_steps=12)
    pend = core.state.pending_patients()
    bad = violations(core.state, "Great, you're all booked!")
    ok = not core.state.booked() and len(pend) >= 1 and len(st.all_appointments()) == 0 and bool(bad)
    return ok, f"pending={[p.key for p in pend]} claim flagged={bool(bad)}"


async def s_pending_retry_same_key_no_dup():
    core, st = make(Fault(timeout_before_commit=999))
    await run_to_wrapup(core, max_steps=14)
    key1 = core.state.patient("self").idempotency_key
    assert key1 and core.state.patient("self").status == "pending", "precondition: booking attempted and left pending"
    core.t.svc.fault.timeout_before_commit = 0  # backend recovers
    await core.book_appointment("self")
    p = core.state.patient("self")
    return p.status == "booked" and p.idempotency_key == key1 and len(st.all_appointments()) == 1, f"key reused={p.idempotency_key == key1}"


async def s_slot_conflict_then_rebook():
    core, st = make()
    await core.identify_patient("self", "Maria", "1982-03-14")
    sl = (await core.find_slots("family", 3))["slots"]
    st.db.execute("UPDATE slots SET taken_by='someone_else' WHERE slot_id=?", (sl[0]["slot_id"],)); st.db.commit()
    await core.choose_slot("self", sl[0]["slot_id"])
    r = await core.book_appointment("self")
    await core.choose_slot("self", r["alternatives"][0]["slot_id"])
    r2 = await core.book_appointment("self")
    return r["status"] == "failed" and r2["status"] == "booked" and len(st.all_appointments()) == 1, f"{r['status']} then {r2['status']}"


async def s_double_book_call_no_dup():
    core, st = make()
    await run_to_wrapup(core)
    await core.book_appointment("self")
    await core.book_appointment("self")
    return len(st.all_appointments()) == 3, f"{len(st.all_appointments())} rows after repeat calls"


async def s_unknown_patient():
    core, _ = make()
    r = await core.identify_patient("self", "Zed", "1999-01-01")
    return r["found"] is False and core.state.step == Step.IDENTIFY, "stays on identify, no crash"


async def s_identify_backend_down():
    core, _ = make()
    async def boom(*a, **k): raise RuntimeError("down")
    core.t.identify = boom
    r = await core.identify_patient("self", "Maria", "1982-03-14")
    return r["found"] is False and "error" in r and core.last_tool_error, "error surfaced, large model routed next"


def _lang(*texts, start="en"):
    s = CallState(call_id="l"); s.language = start
    for t in texts:
        language.observe(s, t)
    return s.language


async def s_lang_si_stays_english():
    return _lang("Sí", "Sí, that works") == "en", "single 'Sí' and 'Sí, that works' stay English"


async def s_lang_name_stays_english():
    return _lang("My name is José Álvarez", "Álvarez, Maria", "Alvarez") == "en", "Spanish names stay English"


async def s_lang_real_spanish_switches():
    return _lang("Hola, quiero una cita para mi hijo") == "es", "full Spanish sentence switches"


async def s_lang_explicit_both_ways():
    a = _lang("Spanish please")
    b = _lang("English, please", start="es")
    return a == "es" and b == "en", f"{a}, {b}"


async def s_end_call_blocked_mid_booking():
    core, _ = make()
    await core.identify_patient("self", "Maria", "1982-03-14")
    await core.set_party_size(3)
    core.state.caller_goodbye = True; core.state.agent_goodbye_said = True
    d = core.end_call_check()
    return (not d.allowed) and not core.state.ended, d.reason


async def s_end_call_needs_goodbye():
    core, _ = make()
    await run_to_wrapup(core)
    d = core.end_call_check()
    return not d.allowed, d.reason


async def s_end_call_after_goodbye_ok():
    core, _ = make()
    await run_to_wrapup(core)
    core.on_user_turn("No, that's all, thank you. Goodbye.")
    core.agent_said_goodbye()
    d = core.end_call_check()
    return d.allowed and core.state.ended, d.reason


async def s_wait_my_son_not_goodbye():
    core, _ = make()
    await run_to_wrapup(core)
    core.on_user_turn("No, wait, my son too")
    return not core.state.caller_goodbye, "continuation phrase is not a goodbye"


async def s_silence_policy():
    core, _ = make()
    a = guards.silence_action(core.state, 3)
    b = guards.silence_action(core.state, 9)
    await core.identify_patient("self", "Maria", "1982-03-14"); await core.set_party_size(3)
    core.state.silence_prompts_sent = 2
    c = guards.silence_action(core.state, 60)
    return (a, b, c) == ("wait", "reprompt", "handoff"), f"{a}, {b}, {c} (pending work is handed off, never dropped)"


async def s_fallback_resumes_same_step():
    core, st = make()
    m1 = StubModel()
    for _ in range(4):
        await m1.act(core)
    step_before = core.state.step
    m2 = StubModel()  # a brand new model object with no memory, like a fallback model
    await m2.act(core)
    await run_to_wrapup(core, model=m2)
    ok = step_before in (Step.COLLECT_PATIENTS, Step.CHOOSE_SLOT, Step.BOOK) and len(st.all_appointments()) == 3
    return ok, f"swapped at step {step_before.value}; finished with {len(st.all_appointments())} appointments, none skipped"


async def s_instruction_identical_for_any_model():
    core, _ = make()
    await core.identify_patient("self", "Maria", "1982-03-14")
    return core.state.instruction() == CallState.from_json(core.state.to_json()).instruction(), "instruction rebuilds identically from saved state"


async def s_prompt_small_and_retrieval_tiny():
    prompt = open(os.path.join(ROOT, "prompts", "system.md")).read()
    r = KB.format(KB.find_providers("my son needs a checkup", k=3))
    return len(prompt) < 20000 and len(prompt) < 3000 and len(r) < 1000, f"prompt {len(prompt)} chars, retrieval result {len(r)} chars"


async def s_retrieval_correct():
    r = KB.find_providers("my son needs a checkup", k=3)
    city = KB.find_providers("cardiology", k=3, city="Riverton")
    ok = r and r[0]["specialty"] in ("Pediatrics", "Family Medicine") and all(KB.location(p["location_id"])["city"] == "Riverton" for p in city)
    return bool(ok), f"top={r[0]['specialty'] if r else None}"


async def s_routing():
    s = CallState(call_id="r"); s.greeted = True
    a = route(s, "hello")[0]
    b = route(s, "I want a real person")[0]
    s.add_patient("self", "Maria", "1982-03-14").status = "pending"
    c = route(s, "ok")[0]
    d = route(CallState(call_id="x"), "hi", last_tool_error=True)[0]
    return (a, b, c, d) == ("small", "large", "large", "large"), f"{a},{b},{c},{d}"


async def s_routing_share_in_family_call():
    core, _ = make()
    tiers = []
    for _ in range(14):
        tiers.append(core.on_user_turn("ok")["tier"])
        await StubModel().act(core)
    share = tiers.count("small") / len(tiers)
    return share >= 0.5, f"small-model share {share:.0%} of turns in a clean family call (stub-driven, routing only)"


async def s_output_guard_allows_truth():
    core, _ = make()
    await run_to_wrapup(core)
    ok_text = "Maria, Lucas and Sofia are booked."
    return violations(core.state, ok_text) == [], "true claim passes"


async def s_call_log_and_replay():
    core, _ = make(Fault(commit_then_timeout=1))
    await run_to_wrapup(core)
    from ava.telemetry import read_call, summarize
    rows = read_call(core.log.path)
    names = {r.get("event") for r in rows}
    return "book_unknown_outcome" in names and "reconcile" in names and summarize(rows) != {}, f"events logged: {sorted(n for n in names if n)}"


async def s_release_gate_hash():
    from ava.release import _sha
    p = os.path.join(ROOT, "config", "agent.json")
    cfg = json.load(open(p))
    ok = os.path.exists(os.path.join(ROOT, cfg["prompt_file"])) and len(_sha(p)) == 16
    return ok, f"config version {cfg['version']} references an in-repo prompt"


async def _guard_stream(state, text, step=7):
    from ava.output_guard import SentenceGuard
    blocked = []
    g = SentenceGuard(state, on_block=lambda v, sent: blocked.append(sent))
    out = "".join(g.feed(text[i:i + step]) for i in range(0, len(text), step)) + g.flush()
    return out, blocked


async def s_real_call_premature_claim_blocked():
    # From real test call 2: the model said "you're set" after choose_slot, before book_appointment confirmed.
    core, _ = make()
    await core.identify_patient("self", "Maria", "1982-03-14")
    slot = (await core.find_slots("pediatrics", 3))["slots"][0]["slot_id"]
    await core.choose_slot("self", slot)
    text = "Maria, you're set for November 12 at 9:00 AM. Lucas can have the next opening. Does that work for him?"
    out, blocked = await _guard_stream(core.state, text)
    ok = len(blocked) == 1 and "you're set" not in out and "Lucas can have the next opening." in out and out.endswith("for him?")
    return ok, f"spoken text: {out!r}"


async def s_real_call_true_statements_not_blocked():
    # From real test call 2: these true sentences were wrongly flagged by the first version of the detector.
    core, _ = make()
    await core.identify_patient("self", "Maria", "1982-03-14")
    await core.identify_patient("child1", "Lucas", "2013-06-02")
    await core.identify_patient("child2", "Sofia", "2016-09-21")
    slot = (await core.find_slots("pediatrics", 3))["slots"][0]["slot_id"]
    await core.choose_slot("self", slot)
    await core.book_appointment("self")
    texts = ["Thanks, Maria. Your visit is booked for November 12 at 9:00 AM. Lucas and Sofia still don't have visits. Want me to book them now?",
             "I could not transfer you right now. Your own checkup is bookedfor November 12 at 9:00 AM."]
    res = [await _guard_stream(core.state, t) for t in texts]
    wrong = await _guard_stream(core.state, "You're all set! Everyone is booked.")
    ok = all(not b for _, b in res) and len(wrong[1]) == 2
    return ok, f"true statements blocked: {sum(len(b) for _, b in res)}; false 'everyone' claims blocked: {len(wrong[1])}/2"


async def s_end_call_refusal_does_not_invite_transfer():
    # From real test call 2: "finish or hand off" made the model transfer a caller who never asked for a person.
    core, _ = make()
    await core.identify_patient("self", "Maria", "1982-03-14")
    await core.set_party_size(3)
    core.state.caller_goodbye = True; core.state.agent_goodbye_said = True
    d = core.end_call_check()
    ok = (not d.allowed) and "hand off" not in d.reason and "Do NOT transfer" in d.reason and "Maria" in d.reason
    return ok, d.reason


async def s_transfer_needs_a_reason_and_does_not_stick():
    core, _ = make()
    await core.identify_patient("self", "Maria", "1982-03-14")
    r1 = await core.request_human_transfer("end call was refused")
    core.on_user_turn("Can I talk to a real person please")
    r2 = await core.request_human_transfer("caller asked")
    core.transfer_failed()
    tier = route(core.state, "ok")[0]
    c2, _ = make()
    c2.on_user_turn("Quiero hablar con una persona")
    ok = (not r1["ok"]) and r2["ok"] and tier == "small" and c2.state.human_requested
    return ok, f"unasked transfer declined={not r1['ok']}, asked transfer allowed={r2['ok']}, tier after failed transfer={tier}"


async def s_fault_env_slow_lookup():
    import time
    old = os.environ.get("FAULT_IDENTIFY_DELAY_S")
    os.environ["FAULT_IDENTIFY_DELAY_S"] = "0.2"
    try:
        f = Fault.from_env()
        svc = BookingService(SQLiteStore(), f)
        t0 = time.time(); svc.identify("Maria", "1982-03-14"); dt = time.time() - t0
    finally:
        os.environ.pop("FAULT_IDENTIFY_DELAY_S", None) if old is None else os.environ.__setitem__("FAULT_IDENTIFY_DELAY_S", old)
    return f.active() and dt >= 0.2 and not Fault.from_env().active(), f"slow lookup injected via env: {dt:.2f}s; default is off"



async def _family_ready(strict=True):
    core, st = make(strict_turns=strict)
    await core.set_party_size(3)
    for k, f, d in FAMILY:
        await core.identify_patient(k, f, d)
    sl = (await core.find_slots("pediatrics", 6))["slots"]
    for (k, _, _), s in zip(FAMILY, sl):
        await core.choose_slot(k, s["slot_id"])
    return core, st


async def s_real_call_book_only_with_callers_yes():
    """Simulated call: the agent asked 'Should Sofia have 9:00?' and booked two people in the same reply. Now it is stopped."""
    core, st = await _family_ready()
    core.on_user_turn("Yes, that is all correct. Thank you.")      # 'all correct' must not unlock multi-booking
    a = await core.book_appointment("self")
    b = await core.book_appointment("child2")                      # same reply, no new caller yes
    blocked = [e for e in __import__("ava.telemetry", fromlist=["read_call"]).read_call(core.log.path) if e.get("event") == "book_blocked_needs_caller_yes"]
    ok = a["status"] == "booked" and b["status"] == "not_booked" and len(st.all_appointments()) == 1 and len(blocked) == 1
    core.on_user_turn("Yes, book Sofia too.")
    c = await core.book_appointment("child2")
    ok = ok and c["status"] == "booked" and len(st.all_appointments()) == 2
    return ok, f"first {a['status']}, same-turn second {b['status']}, after caller yes {c['status']}, rows {len(st.all_appointments())}"


async def s_caller_asks_for_everyone_together():
    core, st = await _family_ready()
    core.on_user_turn("Please book all three of us at those times.")
    out = [(await core.book_appointment(k))["status"] for k, _, _ in FAMILY]
    return out == ["booked"] * 3 and len(st.all_appointments()) == 3, f"statuses {out}"


async def s_same_person_retry_in_one_turn_allowed():
    core, st = await _family_ready()
    core.on_user_turn("Yes please.")
    a = await core.book_appointment("self")
    b = await core.book_appointment("self")                        # repeat for the same person is not a new booking
    return a["status"] == "booked" and b["status"] == "booked" and len(st.all_appointments()) == 1, f"{a['status']}, {b['status']}"


async def s_family_flow_works_with_turn_rule():
    """Normal flow: a caller turn before every step. The new rail must not get in the way."""
    core, st = make(strict_turns=True)
    model = StubModel()
    for _ in range(40):
        core.on_user_turn("yes please")
        await model.act(core)
        if core.state.step == Step.WRAP_UP:
            break
    return len(core.state.booked()) == 3 and len(st.all_appointments()) == 3, f"booked {len(core.state.booked())}, rows {len(st.all_appointments())}"


async def s_goodbye_with_unbooked_child_asks_once_no_goodbye():
    """Simulated call: caller said 'that is everything, goodbye' while Sofia had no appointment. Ava said goodbye, then the guard refused."""
    core, st = await _family_ready()
    core.on_user_turn("Yes, book me.")
    await core.book_appointment("self")
    core.on_user_turn("Yes, book Lucas.")
    await core.book_appointment("child1")
    out = core.on_user_turn("No, that is everything. Goodbye.")
    ins = out["instruction"]
    asks = "STILL HAS NO APPOINTMENT" in ins and "Do NOT say goodbye" in ins and "Sofia" in ins
    d = core.end_call_check()
    await core.decline_patient("child2")
    out2 = core.on_user_turn("Yes, leave her without one. Goodbye.")
    clean = "STILL HAS NO APPOINTMENT" not in out2["instruction"]
    core.agent_said_goodbye()
    d2 = core.end_call_check()
    return asks and (not d.allowed) and clean and d2.allowed, f"instruction asks once {asks}, guard blocked {not d.allowed}, after decline instruction clean {clean}, end allowed {d2.allowed}"


async def s_real_call_goodbye_before_caller_goodbye_replaced():
    """Simulated call: caller said 'yes, that is all correct, thank you' and Ava answered 'Have a great day, and goodbye.'"""
    core, st = make()
    core.on_user_turn("Yes, that is all correct. Thank you.")
    out, blocked = await _guard_stream(core.state, "You're welcome, Maria. Have a great day, and goodbye. ")
    ok = "goodbye" not in out.lower() and "anything else" in out.lower() and len(blocked) == 1
    core.on_user_turn("No, that is everything. Goodbye.")
    out2, blocked2 = await _guard_stream(core.state, "Thank you for calling, Maria. Goodbye. ")
    det = [guards.detect_goodbye(t, True) for t in ("Yes, that is all correct. Thank you.", "That's all right, thanks.", "That is all, thank you.", "That's all. Bye.")]
    ok = ok and det == [False, False, True, True]
    return ok and "Goodbye" in out2 and not blocked2, f"detector {det}; premature replaced: {ok} -> {out.strip()!r}; real goodbye untouched: {'Goodbye' in out2}"


async def s_tool_ms_is_measured():
    core, st = make()
    core.tool_ms = 0.0
    await core.identify_patient("self", "Maria", "1982-03-14")
    first = core.tool_ms
    await core.find_slots("pediatrics", 3)
    return first > 0 and core.tool_ms > first, f"identify {first:.2f} ms, after find_slots {core.tool_ms:.2f} ms"


async def s_collect_step_asks_for_parallel_calls():
    core, st = make()
    core.on_user_turn("Hi, I am Maria, born March 14 1982.")
    await core.identify_patient("self", "Maria", "1982-03-14")
    ins = core.on_user_turn("I also need visits for my two kids.")["instruction"]
    return "SAME step" in ins and "set_party_size" in ins, f"step {core.state.step.value}"


async def s_real_call_same_slot_for_two_people_blocked():
    """Simulated call: after Maria booked 10:00, Lucas was offered 'the same time', the model reused Maria's slot id and booking failed."""
    core2, st2 = make(strict_turns=False)
    await core2.set_party_size(2)
    for k, f, d in FAMILY[:2]:
        await core2.identify_patient(k, f, d)
    sl = (await core2.find_slots("pediatrics", 3))["slots"]
    first = sl[0]["slot_id"]
    await core2.choose_slot("self", first)
    core2.on_user_turn("Yes, book me.")
    b = await core2.book_appointment("self")
    again = await core2.find_slots("pediatrics", 3)
    r = await core2.choose_slot("child1", first)                    # the model reuses Maria's slot id
    ok = (b["status"] == "booked" and first not in [x["slot_id"] for x in again["slots"]]
          and r.get("ok") is False and r.get("slots") and first not in [x["slot_id"] for x in r["slots"]])
    r2 = await core2.choose_slot("child1", r["slots"][0]["slot_id"])
    return ok and r2.get("ok") is True, f"reuse blocked: {r.get('ok') is False}, fresh slots offered: {len(r.get('slots', []))}, new choice accepted: {r2.get('ok')}"


async def s_accented_name_still_matches():
    """Simulated Spanish call: the model wrote 'María' and the record is 'Maria', so the lookup said not found."""
    core, st = make()
    a = await core.identify_patient("self", "María", "1982-03-14")
    b = await core.identify_patient("self", "MARIA", "1982-03-14")
    c = await core.identify_patient("self", "Mario", "1982-03-14")
    return a["found"] and b["found"] and not c["found"], f"María {a['found']}, MARIA {b['found']}, Mario {c['found']}"


async def s_negated_statement_is_not_a_false_claim():
    core, st = make()
    ok_text = "No visit is booked yet, so please call us back when you're ready to schedule Sofia's checkup."
    v1 = violations(st if hasattr(st, "active_patients") else core.state, ok_text)
    await core.identify_patient("child2", "Sofia", "2016-09-21")
    v2 = violations(core.state, ok_text)
    v3 = violations(core.state, "Sofia is booked for November 12th.")
    return not v1 and not v2 and bool(v3), f"negated honest sentence flagged {bool(v1 or v2)}, real false claim flagged {bool(v3)}"


async def s_goodbye_over_open_work_becomes_one_question():
    """Simulated call: caller said goodbye while a person still had no appointment; the model said goodbye and the guard then refused."""
    core, st = await _family_ready(strict=False)
    core.on_user_turn("Yes, book me.")
    await core.book_appointment("self")
    core.on_user_turn("No, that is everything. Goodbye.")
    out, blocked = await _guard_stream(core.state, "Maria is booked. Goodbye, and take care. Have a great day. ")
    asks_once = out.count("still has no appointment") == 1 and "oodbye" not in out
    out2, _ = await _guard_stream(core.state, "Should I book Lucas now? Goodbye, take care. ")
    no_double = out2.count("?") == 1 and "oodbye" not in out2
    return asks_once and no_double and len(blocked) >= 1, f"asks once: {asks_once}; no second question after one was asked: {no_double}"


async def s_spanish_request_is_honoured_not_refused():
    core, st = make()
    out = core.on_user_turn("Hello, can we speak in Spanish please?")
    ins = out["instruction"]
    ok = core.state.language == "es" and "Speak ONLY Spanish. You CAN speak Spanish" in ins and "just asked to change language" in ins
    out2 = core.on_user_turn("Hola, me llamo Maria.")["instruction"]
    return ok and "just asked to change language" not in out2, f"language {core.state.language}, switch notice only once: {'just asked' not in out2}"

async def s_real_call_reasoning_text_never_spoken():
    """Real simulated call: the model said 'The caller said yes to a time, but didn't specify which...' out loud."""
    core, st = make()
    from ava.output_guard import SentenceGuard
    blocked = []
    g = SentenceGuard(core.state, on_block=lambda v, t: blocked.append(v))
    out = g.feed("The caller said yes to a time, but didn't specify which. I should confirm rather than guess. "
                 "Would you like the 11:00 AM slot or the 11:30 AM slot? ") + g.flush()
    return "caller" not in out.lower() and "guess" not in out.lower() and "11:00" in out and len(blocked) == 2, f"spoken: {out.strip()!r}, blocked {len(blocked)}"


async def s_real_call_markdown_and_provider_ids_not_spoken():
    core, st = make()
    from ava.output_guard import SentenceGuard
    g = SentenceGuard(core.state)
    out = g.feed("**Here you go.**\n- Maria, with provider 5.\n- Lucas, with provider 7.\n") + g.flush()
    return "*" not in out and "-" not in out and not any(ch.isdigit() for ch in out), f"spoken: {out.strip()!r}"


async def s_real_call_transfer_not_promised_without_a_request():
    core, st = make()
    from ava.output_guard import SentenceGuard
    blocked = []
    g = SentenceGuard(core.state, on_block=lambda v, t: blocked.append(v))
    a = g.feed("I'm transferring you to a person at the clinic now. ") + g.flush()
    core.state.human_requested = True
    g2 = SentenceGuard(core.state)
    b = g2.feed("I'm transferring you to a person at the clinic now. ") + g2.flush()
    offer = SentenceGuard(core.state).feed("I can transfer you to a person if you like. ")
    return a == "" and bool(blocked) and "transferring" in b and "transfer you" in offer, f"unasked: {a!r}, asked: {b.strip()!r}, offer kept: {'transfer you' in offer}"


async def s_real_call_explicit_spanish_survives_one_noisy_transcript():
    """Real call: Spanish was requested, one transcript came back as an English sentence and the call flipped to English."""
    core, st = make()
    core.on_user_turn("Hello, can we speak in Spanish please?")
    core.on_user_turn("so low for me that is all I need for the check up please")
    stayed = core.state.language == "es"
    core.on_user_turn("English please")
    back = core.state.language == "en"
    return stayed and back, f"stayed Spanish after one noisy English transcript: {stayed}, explicit English request honoured: {back}"


async def s_real_call_sophia_matches_sofia():
    """Real call: speech-to-text wrote 'Sophia' for the record 'Sofia'. The exact date of birth still has to match."""
    core, st = make()
    a = await core.identify_patient("child2", "Sophia", "2016-09-21")
    b = await core.identify_patient("child1", "Lukas", "2013-06-02")
    c = await core.identify_patient("child3", "Sophia", "2016-09-22")
    d = await core.identify_patient("child4", "Mario", "1982-03-14")
    return a["found"] and b["found"] and not c["found"] and not d["found"], f"Sophia {a['found']}, Lukas {b['found']}, wrong DOB {c['found']}, Mario {d['found']}"


async def s_real_call_rule_talk_never_spoken():
    """Real simulated call: 'However, the rules say to speak the language named on the CALL LANGUAGE line only...'"""
    core, st = make()
    from ava.output_guard import SentenceGuard
    g = SentenceGuard(core.state)
    out = g.feed("However, the rules say to speak the language named on the CALL LANGUAGE line only. It says English. "
                 "So I stay in English. What is your date of birth? ") + g.flush()
    return "rules" not in out.lower() and "CALL LANGUAGE" not in out and "date of birth" in out, f"spoken: {out.strip()!r}"


async def s_call_log_sink_never_breaks_a_call():
    """Logging must never hurt a caller: a dead log store, a full queue and a normal flush."""
    import tempfile
    from ava.telemetry import CallLogger, RemoteSink
    calls = []

    def bad_post(rows):
        calls.append(len(rows))
        raise RuntimeError("log store down")

    sink = RemoteSink("http://example.invalid", post=bad_post, interval=3600)
    log = CallLogger("call_sink_test", log_dir=tempfile.mkdtemp(), sink=sink)
    log.event("identify_done", ms=40, found=True)       # must not raise even though the sink is broken
    sink.flush()
    dead_ok = sink.dropped == 1 and calls == [1, 1, 1]  # three tries, then dropped, no exception
    good = []
    sink2 = RemoteSink("http://example.invalid", post=lambda rows: good.append(rows), interval=3600)
    log2 = CallLogger("call_sink_test2", log_dir=tempfile.mkdtemp(), sink=sink2)
    log2.event("call_start", release="x")
    log2.event("call_end", turns=1)
    sink2.flush()
    ok2 = len(good) == 1 and len(good[0]) == 2 and sink2.sent == 2
    return dead_ok and ok2, f"broken sink: dropped {sink.dropped} after {len(calls)} tries without raising; working sink sent {sink2.sent}"


async def s_backend_stores_call_logs():
    from fastapi.testclient import TestClient
    from ava.backend.app import create_app
    from ava.backend.store import SQLiteStore
    st = SQLiteStore()
    c = TestClient(create_app(store=st))
    r = c.post("/v1/calllog", json={"rows": [{"call_id": "c1", "ts": 1.0, "kind": "event", "event": "call_start"},
                                              {"call_id": "c1", "ts": 2.0, "kind": "turn"}]})
    n = st.db.execute("SELECT COUNT(*) FROM call_logs").fetchone()[0]
    return r.status_code == 200 and r.json()["stored"] == 2 and n == 2, f"status {r.status_code}, rows stored {n}"


async def s_call_end_flushes_the_log_tail():
    """Real probe call: the last events (call_end) never reached Supabase because the process exited first."""
    import tempfile
    from ava.telemetry import CallLogger, RemoteSink
    got = []
    sink = RemoteSink("http://example.invalid", post=lambda rows: got.extend(rows), interval=3600)
    log = CallLogger("call_flush_test", log_dir=tempfile.mkdtemp(), sink=sink)
    log.event("identify_done", ms=40, found=True)
    log.event("call_end", turns=1, booked=1)          # no waiting for the timer
    return [r.get("event") for r in got] == ["identify_done", "call_end"], f"events sent at call end: {[r.get('event') for r in got]}"


SCENARIOS = [(n[2:], f) for n, f in list(globals().items()) if n.startswith("s_") and callable(f)]


def run_all(verbose=True):
    results = []
    for name, fn in SCENARIOS:
        try:
            ok, detail = asyncio.run(fn())
        except Exception as e:  # a crash is a failure
            ok, detail = False, f"EXCEPTION {type(e).__name__}: {e}"
        results.append({"name": name, "passed": bool(ok), "detail": detail})
        if verbose:
            print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")
    return results


def main():
    res = run_all()
    n = sum(r["passed"] for r in res)
    print(f"\n{n}/{len(res)} orchestration evals passed (stub model; see module docstring)")
    os.makedirs(os.path.join(ROOT, "results"), exist_ok=True)
    json.dump({"label": "orchestration evals, stub model", "passed": n, "total": len(res), "results": res},
              open(os.path.join(ROOT, "results", "evals.json"), "w"), indent=2)
    return n == len(res)
