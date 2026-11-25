"""ReceptionistCore: the framework-independent brain for the medical practice receptionist.

Same contract as ava.core.AvaCore (the voice worker and the evals both drive THIS code), with these rules enforced here,
not by the prompt:
  * No personal or health detail, and no change, before the caller is verified (name + date of birth, ZIP if two people match).
  * A caller who is not the patient gets general practice information and a message service, nothing else.
  * A change is confirmed only by a READ of the records. A timeout is PENDING, never "done".
  * One scheduling change per caller turn, and only for slots this call actually offered, or appointments this patient owns.
  * Urgent words stop scheduling and send the caller to emergency services.
  * The call ends only after the caller's goodbye and with no pending work.
Every action writes an audit entry with pseudonymous references and no personal details. Free text in call logs is redacted."""
import hashlib
import json
import os
import re
import time

from ava import guards, language
from ava.backend.store import new_callback_ref
from ava.ehr.base import Patient, WriteStatus
from ava.hipaa.audit import FORBIDDEN_KEYS
from ava.hipaa.identity import IdentityGate
from ava.hipaa.redact import redact
from ava.receptionist.state import Op, ReceptionistState, Step
from ava.router import HUMAN_REQUEST
from ava.telemetry import CallLogger

# Words that mean "stop scheduling and send this person to emergency help". Deliberately broad: a false alarm costs one
# sentence, a missed emergency costs far more. This list is a safety net, not a medical triage tool.
URGENT = re.compile(r"\b(chest pain|can'?t breathe|cannot breathe|trouble breathing|difficulty breathing|short of breath|"
                    r"(having|think i'?m having|think he'?s having|think she'?s having) (a )?(heart attack|stroke|seizure)|"
                    r"heart attack|stroke|seizure|unconscious|passed out|not breathing|overdose|overdosed|"
                    r"bleeding (heavily|a lot|badly|won'?t stop)|severe bleeding|suicid\w*|kill myself|want to die|end my life|"
                    r"anaphyla\w*|choking|poison\w*|emergency)\b", re.I)

SAFE_KEYS_NOTE = "audit details never hold personal fields"


class SafeLog:
    """Wraps CallLogger: every free-text value is redacted before it is written, and personal field names are refused."""

    def __init__(self, inner, names_fn=lambda: ()):
        self.inner, self.names_fn = inner, names_fn
        self.call_id = getattr(inner, "call_id", "")

    def _clean(self, data):
        out = {}
        for k, v in data.items():
            if k.lower() in FORBIDDEN_KEYS:
                continue
            out[k] = redact(v, self.names_fn()) if isinstance(v, str) else v
        return out

    def event(self, name, level="info", **data):
        return self.inner.event(name, level=level, **self._clean(data))

    def turn(self, rec):
        return self.inner.turn(rec)

    def __getattr__(self, item):
        return getattr(self.inner, item)


class MessageStore:
    """Messages for staff. Holds personal details by nature, so it is its own folder with its own retention (ava.hipaa.retention),
    and it is never copied into call logs. One JSON line per message."""

    def __init__(self, directory):
        self.dir = directory
        os.makedirs(directory, exist_ok=True)

    def save(self, call_id, ref, kind, text):
        path = os.path.join(self.dir, f"{call_id}.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ref": ref, "ts": time.time(), "kind": kind, "text": text}) + "\n")
        return ref


class NullAudit:
    def record(self, *a, **k):
        return None


class ReceptionistCore:
    def __init__(self, call_id, ehr, *, audit=None, logger=None, messages=None, practice=None, state=None,
                 today="2026-11-25", department_id="1", appointment_type_id="8", horizon_days=30, log_dir=None):
        self.state = state or ReceptionistState(call_id=call_id)
        self.state.gate = self.state.gate or IdentityGate(ehr)
        self.ehr, self.audit = ehr, audit or NullAudit()
        self.messages = messages or MessageStore(os.path.join(log_dir or "logs", "messages"))
        self.practice = practice or {}
        self.today, self.dept, self.appt_type, self.horizon = today, str(department_id), str(appointment_type_id), horizon_days
        raw = logger or CallLogger(call_id, log_dir=log_dir, alert_webhook="")
        self.log = SafeLog(raw, self._names)
        self.last_user_text = ""
        self.last_tool_error = False
        self.tool_ms = 0.0
        self._offered = {}               # slot_id -> Slot, only what find_open_slots returned on THIS call
        self._writes_this_turn = 0
        self._slot_for_op = {}           # op key -> Slot
        for n in ("verify_identity", "add_verification_detail", "list_my_appointments", "find_open_slots", "book_visit",
                  "cancel_visit", "reschedule_visit"):
            setattr(self, n, self._timed(getattr(self, n)))

    # ---- helpers --------------------------------------------------------
    def _names(self):
        p = self.state.gate.patient if self.state.gate else None
        return [p.first_name, p.last_name] if p else []

    def _timed(self, fn):
        async def run(*a, **k):
            t0 = time.perf_counter()
            try:
                return await fn(*a, **k)
            finally:
                self.tool_ms += (time.perf_counter() - t0) * 1000.0
        return run

    def _aud(self, action, outcome="ok", **detail):
        p = self.state.gate.patient if self.state.gate else None
        self.audit.record(self.state.call_id, action, subject=p.patient_id if p else None, outcome=outcome, **detail)

    def _key(self, kind, *parts):
        raw = ":".join([self.state.call_id, self.state.gate.patient.patient_id if self.state.gate.patient else "?", kind, *parts])
        return hashlib.sha256(raw.encode()).hexdigest()[:20]

    def _need_verified(self, action):
        s = self.state
        if s.verified:
            return None
        why = "third_party" if s.third_party else "not_verified"
        self.log.event("phi_refused", level="warn", action=action, why=why)
        self._aud(action, outcome="refused_" + why)
        return {"ok": False, "error": "not verified", "say": "You cannot do this yet: the caller is not verified" +
                (" and is not the patient. Offer general practice information or a message instead." if s.third_party
                 else ". Ask for first name, last name and date of birth and call verify_identity.")}

    def _one_write_per_turn(self, action):
        if self._writes_this_turn >= 1:
            self.log.event("write_blocked_needs_caller_yes", level="warn", action=action)
            return {"status": "not_done", "error": "needs the caller's yes first",
                    "say": "Do not do this yet. Tell the caller exactly what you propose and wait for their yes."}
        self._writes_this_turn += 1
        return None

    # ---- per-turn hook: runs BEFORE the model sees the turn --------------
    def on_user_turn(self, text):
        s = self.state
        s.turn_count += 1
        self.mark_announced()       # whatever the model said after the last tool call has been spoken by now
        self.last_user_text = text or ""
        self._writes_this_turn = 0
        dec = language.observe(s, text)
        s.language_just_switched = bool(dec.switched)
        if dec.switched:
            self.log.event("language_switch", to=dec.language, reason=dec.reason)
        s.caller_goodbye = guards.detect_goodbye(text or "", s.step == Step.WRAP_UP)
        s.silence_prompts_sent = 0
        if HUMAN_REQUEST.search(text or ""):
            s.human_requested = True
        if URGENT.search(text or "") and not s.urgent:
            s.urgent = True
            self.log.event("urgent_detected", level="warn")
            self._aud("urgent_detected", outcome="routed_to_emergency_guidance")
        tier = "small"
        reason = "routine turn"
        g = s.gate
        if s.urgent:
            tier, reason = "large", "possible emergency"
        elif self.last_tool_error:
            tier, reason = "large", "tool error on previous step"
        elif s.pending_ops():
            tier, reason = "large", "change outcome needs careful wording (pending)"
        elif g and g.locked:
            tier, reason = "large", "verification locked"
        elif s.human_requested or s.transfer_requested:
            tier, reason = "large", "caller asked for a person"
        return {"tier": tier, "route_reason": reason, "instruction": s.instruction(), "language": s.language,
                "language_switched": dec.switched}

    def mark_greeted(self):
        self.state.greeted = True

    # ---- identity -------------------------------------------------------
    async def verify_identity(self, first_name, last_name, date_of_birth):
        s = self.state
        s.pending_tools.append("verify_identity")
        try:
            res = await s.gate.verify(first_name, last_name, date_of_birth)
            self.last_tool_error = False
        except Exception as e:
            self.last_tool_error = True
            self.log.event("verify_error", level="error", error=type(e).__name__)
            return {"status": "unavailable", "say": "Say you cannot reach the records right now and offer a message or a transfer."}
        finally:
            s.pending_tools.remove("verify_identity")
        self._aud("verify_identity", outcome=res.status, attempts=s.gate.attempts)
        self.log.event("verify_result", status=res.status, attempts=s.gate.attempts)
        if res.status == "verified":
            return {"status": "verified", "say": "The caller is verified. Do not read back their birth date. Ask how you can help."}
        if res.status == "need_more":
            return {"status": "need_more", "say": res.message}
        if res.status == "locked":
            return {"status": "locked", "say": res.message + " Call request_human_transfer. Share no details."}
        return {"status": "failed", "say": res.message}

    async def add_verification_detail(self, zip_code):
        s = self.state
        res = await s.gate.add_detail(zip_code)
        self._aud("verify_detail", outcome=res.status, attempts=s.gate.attempts)
        self.log.event("verify_result", status=res.status, attempts=s.gate.attempts)
        if res.status == "verified":
            return {"status": "verified", "say": "The caller is verified. Ask how you can help."}
        if res.status == "locked":
            return {"status": "locked", "say": res.message + " Call request_human_transfer. Share no details."}
        return {"status": "failed", "say": res.message}

    async def caller_is_not_the_patient(self):
        self.state.gate.third_party()
        self._aud("third_party_caller")
        self.log.event("third_party_caller")
        return {"ok": True, "say": "Share no appointment or health details. Offer general practice information or taking a message."}

    # ---- reads ----------------------------------------------------------
    def _window(self):
        import datetime as dt
        end = dt.date.fromisoformat(self.today) + dt.timedelta(days=self.horizon)
        return self.today, end.isoformat()

    async def list_my_appointments(self):
        bad = self._need_verified("list_appointments")
        if bad:
            return bad
        p = self.state.gate.patient
        start, end = self._window()
        end_long = f"{int(self.today[:4]) + 1}{self.today[4:]}"
        try:
            mine = await self.ehr.patient_appointments(p.patient_id, start, end_long)
            self.last_tool_error = False
        except Exception as e:
            self.last_tool_error = True
            self.log.event("list_error", level="error", error=type(e).__name__)
            return {"ok": False, "say": "Say you could not reach the records right now and offer a message or a transfer."}
        self.state.my_appointments = list(mine)
        self._aud("list_appointments", count=len(mine))
        rows = [{"appointment_id": a.slot_id, "date": a.date, "time": a.start_time} for a in mine[:5]]
        return {"ok": True, "appointments": rows,
                "say": "Read at most two upcoming visits, date and time only, then ask what the caller wants to do." if rows
                       else "Tell the caller there are no upcoming visits on file."}

    async def find_open_slots(self, reason=""):
        bad = self._need_verified("find_slots")
        if bad:
            return bad
        start, end = self._window()
        try:
            slots = await self.ehr.open_slots(self.dept, self.appt_type, start, end, limit=3)
            self.last_tool_error = False
        except Exception as e:
            self.last_tool_error = True
            self.log.event("slots_error", level="error", error=type(e).__name__)
            return {"ok": False, "say": "Say you could not reach the schedule right now and offer a message or a transfer."}
        self._offered = {x.slot_id: x for x in slots}
        self._aud("find_slots", count=len(slots))
        return {"ok": True, "slots": [{"slot_id": x.slot_id, "date": x.date, "time": x.start_time} for x in slots],
                "say": "Offer ONE time and ask a yes or no question. Never read the whole list." if slots
                       else "Say there are no open times in the next month and offer a message for the scheduling team."}

    # ---- writes ---------------------------------------------------------
    def _op(self, kind, key, **kw):
        for o in self.state.ops:
            if o.key == key:
                return o
        o = Op(kind=kind, key=key, **kw)
        self.state.ops.append(o)
        return o

    def _apply(self, op, res, kind):
        op.status = res.status.value
        op.appointment_id = res.appointment_id or op.appointment_id
        op.detail = res.detail
        op.announced = False
        self.last_tool_error = res.status in (WriteStatus.PENDING, WriteStatus.FAILED)
        self._aud(kind, outcome=res.status.value, ms=round(res.elapsed_s * 1000), reconciled=res.reconciled)
        self.log.event(f"{kind}_result", level="warn" if self.last_tool_error else "info", status=res.status.value,
                       detail=res.detail, ms=round(res.elapsed_s * 1000))

    def _say_for(self, status, what):
        if status == WriteStatus.BOOKED:
            return f"The records system confirms it. You may say {what} is done."
        if status == WriteStatus.CANCELLED:
            return "The records system confirms it is cancelled. You may say so."
        if status == WriteStatus.PENDING:
            return ("Say you have sent the change and are still confirming it with the records system. Do NOT say it is done. "
                    "Offer a message so a person can follow up.")
        if status == WriteStatus.SLOT_TAKEN:
            return "That time was just taken. Say so and offer another time with find_open_slots."
        return "It did not go through. Say so, and offer another time, a message, or a transfer."

    async def book_visit(self, slot_id):
        bad = self._need_verified("book")
        if bad:
            return bad
        slot = self._offered.get(str(slot_id))
        if slot is None:
            return {"status": "not_done", "error": "unknown slot", "say": "That time was not offered on this call. Call find_open_slots first."}
        key = self._key("book", slot.slot_id)
        existing = next((o for o in self.state.ops if o.key == key), None)
        if existing is None:
            wait = self._one_write_per_turn("book")
            if wait:
                return wait
        op = self._op("book", key, slot_id=slot.slot_id)
        self.state.pending_tools.append("book_visit")
        try:
            res = await self.ehr.book(slot, self.state.gate.patient, key)
        finally:
            self.state.pending_tools.remove("book_visit")
        self._slot_for_op[key] = slot
        self._apply(op, res, "book")
        return {"status": res.status.value, "appointment_id": res.appointment_id, "date": slot.date, "time": slot.start_time,
                "say": self._say_for(res.status, "the booking")}

    async def cancel_visit(self, appointment_id):
        bad = self._need_verified("cancel")
        if bad:
            return bad
        appt_id = str(appointment_id)
        if self.state.my_appointments is None:
            await self.list_my_appointments()
        if appt_id not in {a.slot_id for a in (self.state.my_appointments or [])}:
            self._aud("cancel", outcome="refused_not_this_patients")
            return {"status": "not_done", "error": "unknown appointment",
                    "say": "That is not one of this caller's upcoming visits. Call list_my_appointments."}
        key = self._key("cancel", appt_id)
        if not any(o.key == key for o in self.state.ops):
            wait = self._one_write_per_turn("cancel")
            if wait:
                return wait
        op = self._op("cancel", key, old_appointment_id=appt_id)
        self.state.pending_tools.append("cancel_visit")
        try:
            res = await self.ehr.cancel(appt_id, self.state.gate.patient.patient_id, key)
        finally:
            self.state.pending_tools.remove("cancel_visit")
        self._apply(op, res, "cancel")
        if res.status == WriteStatus.CANCELLED:
            self.state.my_appointments = [a for a in self.state.my_appointments if a.slot_id != appt_id]
        return {"status": res.status.value, "say": self._say_for(res.status, "the cancellation")}

    async def reschedule_visit(self, old_appointment_id, new_slot_id):
        bad = self._need_verified("reschedule")
        if bad:
            return bad
        old_id = str(old_appointment_id)
        if self.state.my_appointments is None:
            await self.list_my_appointments()
        if old_id not in {a.slot_id for a in (self.state.my_appointments or [])}:
            self._aud("reschedule", outcome="refused_not_this_patients")
            return {"status": "not_done", "error": "unknown appointment", "say": "That is not one of this caller's upcoming visits."}
        slot = self._offered.get(str(new_slot_id))
        if slot is None:
            return {"status": "not_done", "error": "unknown slot", "say": "That time was not offered on this call. Call find_open_slots first."}
        key = self._key("reschedule", old_id, slot.slot_id)
        if not any(o.key == key for o in self.state.ops):
            wait = self._one_write_per_turn("reschedule")
            if wait:
                return wait
        op = self._op("reschedule", key, slot_id=slot.slot_id, old_appointment_id=old_id)
        self.state.pending_tools.append("reschedule_visit")
        try:
            new, old = await self.ehr.reschedule(old_id, slot, self.state.gate.patient, key)
        finally:
            self.state.pending_tools.remove("reschedule_visit")
        self._slot_for_op[key] = slot
        if new.status != WriteStatus.BOOKED:
            self._apply(op, new, "reschedule")
            return {"status": new.status.value, "old_visit": "unchanged", "say": self._say_for(new.status, "the new time") +
                    " The old visit is unchanged."}
        if old is not None and old.status == WriteStatus.CANCELLED:
            self._apply(op, new, "reschedule")
            self.state.my_appointments = [a for a in (self.state.my_appointments or []) if a.slot_id != old_id]
            return {"status": "booked", "old_visit": "cancelled", "say": "The new time is confirmed and the old one is cancelled."}
        # New time confirmed, old one not yet confirmed cancelled: be honest about both halves.
        from ava.ehr.base import WriteResult
        self._apply(op, WriteResult(WriteStatus.PENDING, new.appointment_id, "new_booked_old_cancel_unconfirmed", True, new.elapsed_s), "reschedule")
        return {"status": "pending", "new_visit": "booked", "old_visit": "cancellation still being confirmed",
                "say": "Say the NEW time is confirmed, but you are still confirming that the OLD time was removed, and a person will check."}

    async def refresh_pending(self, window_s=2.0):
        """Last look at unconfirmed changes (end of call). Reads only, never writes. Returns how many are still pending."""
        from ava.ehr.base import WriteResult
        for op in list(self.state.pending_ops()):
            try:
                if op.kind == "cancel":
                    res = await self.ehr.reconcile_cancel(op.old_appointment_id, window_s=window_s)
                    if res.status == WriteStatus.CANCELLED:
                        op.status, op.announced = WriteStatus.CANCELLED.value, False
                elif op.key in self._slot_for_op:
                    slot = self._slot_for_op[op.key]
                    res = await self.ehr.reconcile_book(slot, self.state.gate.patient, window_s=window_s)
                    if res.status == WriteStatus.BOOKED:
                        op.status, op.announced = WriteStatus.BOOKED.value, False
                        if op.kind == "reschedule" and op.old_appointment_id:
                            old = await self.ehr.cancel(op.old_appointment_id, self.state.gate.patient.patient_id, op.key + ":old")
                            if old.status != WriteStatus.CANCELLED:
                                op.status = WriteStatus.PENDING.value
            except Exception as e:
                self.log.event("refresh_error", level="error", error=type(e).__name__)
        n = len(self.state.pending_ops())
        self._aud("refresh_pending", outcome="still_pending" if n else "clear", pending=n)
        return n

    # ---- messages, info, transfer ----------------------------------------
    async def practice_info(self, topic=""):
        q = set(re.findall(r"[a-z]+", (topic or "").lower()))
        keys = {"hours": {"hour", "hours", "open", "close", "closed", "when"}, "location": {"where", "address", "location", "directions", "parking"},
                "services": {"service", "services", "offer", "vaccine", "vaccination", "physical", "lab"},
                "new_patient": {"new"}, "after_hours": {"after", "night", "weekend", "emergency"},
                "cancellation": {"cancel", "cancellation", "late", "fee"}, "insurance": {"insurance", "billing", "bill", "copay", "cost"}}
        hit = [k for k, w in keys.items() if q & w and k in self.practice]
        if not hit:
            return {"info": "No matching information. Offer to take a message for the staff.", "practice": self.practice.get("name", "")}
        return {"info": " ".join(self.practice[k] for k in hit[:2]), "practice": self.practice.get("name", "")}

    async def take_message(self, message, callback_number_if_given=""):
        """Save a message for staff. Works for any caller. The text goes to the message store only, never to call logs."""
        ref = new_callback_ref()
        text = (message or "").strip()
        if callback_number_if_given:
            text += f" | callback: {callback_number_if_given}"
        try:
            self.messages.save(self.state.call_id, ref, "message", text)
        except Exception as e:
            self.log.event("message_save_failed", level="error", error=type(e).__name__)
            return {"ok": False, "ref": None, "say": "Say you could not save the message just now and suggest calling the practice again."}
        self.state.message_saved = True
        self._aud("take_message", outcome="saved")
        self.log.event("message_saved", ref=ref)
        return {"ok": True, "ref": ref, "say": "Say a staff member will follow up. Read the reference slowly, one character at a time, once: " + " ".join(ref)}

    async def leave_callback(self, note):
        """Same signature as the clinic agent, used by the shared voice worker when a transfer cannot happen."""
        r = await self.take_message(note)
        self.state.callback_saved = bool(r.get("ok"))
        return {"ok": bool(r.get("ok")), "ref": r.get("ref")}

    async def request_human_transfer(self, reason):
        s = self.state
        failing = self.last_tool_error or bool(s.pending_ops()) or bool(s.gate and s.gate.locked) or s.urgent
        if not (s.human_requested or failing):
            self.log.event("transfer_declined", level="warn")
            return {"ok": False, "reason": "The caller did not ask for a person and nothing has failed. Do not transfer. Keep helping."}
        s.transfer_requested = True
        self._aud("transfer_requested")
        self.log.event("transfer_requested", reason=reason)
        return {"ok": True}

    def transfer_failed(self):
        self.state.transfer_requested = False
        self.log.event("transfer_unavailable", level="warn")

    # ---- ending ---------------------------------------------------------
    def mark_announced(self):
        for o in self.state.ops:
            if o.settled():
                o.announced = True

    def agent_said_goodbye(self):
        self.state.agent_goodbye_said = True

    def end_call_check(self):
        s = self.state
        if s.has_pending_work() and not s.callback_saved:
            allowed, why = False, "a change is still being confirmed. Tell the caller, and offer a message so a person can follow up."
        elif s.urgent and not s.caller_goodbye:
            allowed, why = False, "possible emergency: make sure the caller has the emergency number first"
        elif not s.caller_goodbye:
            allowed, why = False, "caller has not said goodbye"
        elif not s.agent_goodbye_said:
            allowed, why = False, "say goodbye to the caller first"
        else:
            allowed, why = True, "caller said goodbye, no pending work, agent said goodbye"
        self.log.event("end_call_check", allowed=allowed, reason=why)
        if allowed:
            s.ended = True
            self._aud("end_call")
        return type("Decision", (), {"allowed": allowed, "reason": why})()
