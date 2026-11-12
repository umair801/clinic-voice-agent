"""AvaCore: the framework-independent brain. Voice worker and evals both drive THIS code.

Owns: call state, tool behavior, language policy, end_call guard, routing, retrieval, logging.
The LLM only chooses what to say and which tool to call. Every rule that must hold is enforced here.
"""
import time

from ava import guards, language
from ava.backend.booking import make_key
from ava.backend.reliable import reliable_book
from ava.retrieval import KnowledgeBase
from ava.router import HUMAN_REQUEST, MULTI_BOOKING_OK, route
from ava.state import BookingStatus, CallState, Step
from ava.telemetry import CallLogger


class AvaCore:
    def __init__(self, call_id, transport, kb=None, logger=None, state=None, book_pause=0.3, one_booking_per_turn=True):
        self.state = state or CallState(call_id=call_id)
        self.t = transport
        self.kb = kb or KnowledgeBase()
        self.log = logger or CallLogger(call_id)
        self.book_pause = book_pause
        self.last_tool_error = False
        self.last_user_text = ""
        self.one_booking_per_turn = one_booking_per_turn
        self.tool_ms = 0.0                       # summed duration of tool calls since the last reset (read by the voice worker)
        for name in ("identify_patient", "find_slots", "find_provider", "book_appointment"):
            setattr(self, name, self._timed(getattr(self, name)))

    def _timed(self, fn):
        async def run(*a, **k):
            t0 = time.perf_counter()
            try:
                return await fn(*a, **k)
            finally:
                self.tool_ms += (time.perf_counter() - t0) * 1000.0
        return run

    # ---- per-turn hook: runs BEFORE the model sees the turn ---------------
    def on_user_turn(self, text):
        s = self.state
        s.turn_count += 1
        self.last_user_text = text
        dec = language.observe(s, text)
        s.language_just_switched = bool(dec.switched)
        if dec.switched:
            self.log.event("language_switch", to=dec.language, reason=dec.reason)
        s.caller_goodbye = guards.detect_goodbye(text, s.step == Step.WRAP_UP)
        s.silence_prompts_sent = 0
        s.books_this_turn_keys = []
        s.multi_booking_ok = bool(MULTI_BOOKING_OK.search(text or ""))
        if HUMAN_REQUEST.search(text or ""):
            s.human_requested = True
        self._auto_decline_if_left(text)
        tier, reason = route(s, text, self.last_tool_error)
        return {"tier": tier, "route_reason": reason, "instruction": s.instruction(), "language": s.language,
                "language_switched": dec.switched}

    def _auto_decline_if_left(self, text):
        """Caller answered 'book now or leave it?' with a clear 'leave it'. The call state must not depend on the model
        remembering to call decline_patient (found in a web test call: the question repeated 3 times)."""
        s = self.state
        if not s.pending_asked or s.pending_tools or not text:
            return
        if not guards.DECLINE_OPEN.search(text) or guards.BOOK_NOW.search(text):
            return
        left = [p for p in s.active_patients() if p.status == BookingStatus.NONE.value]
        for p in left:
            p.declined = True
        if left:
            s.pending_asked = False
            self.log.event("auto_decline", who=[p.first_name or p.key for p in left])

    def mark_greeted(self):
        self.state.greeted = True

    # ---- tools ----------------------------------------------------------
    async def set_party_size(self, n):
        self.state.expected_party_size = max(1, min(int(n), 8))
        return {"ok": True, "party_size": self.state.expected_party_size}

    async def identify_patient(self, key, first_name, dob):
        s = self.state
        p = s.add_patient(key, first_name, dob)
        s.pending_tools.append("identify_patient")
        t0 = time.time()
        try:
            res = await self.t.identify(first_name, dob)
            self.last_tool_error = False
        except Exception as e:
            self.last_tool_error = True
            self.log.event("identify_error", level="error", error=type(e).__name__)
            return {"found": False, "error": "lookup unavailable", "say": "Tell the caller you could not reach the records right now and offer a transfer."}
        finally:
            s.pending_tools.remove("identify_patient")
        self.log.event("identify_done", ms=round((time.time() - t0) * 1000), found=bool(res.get("found")))
        if res.get("found"):
            p.patient_id = res["patient_id"]
            name = res.get("first_name") or p.first_name
            p.first_name = name          # speak the record spelling, not the speech-to-text guess
            return {"found": True, "key": key, "name": name}
        p.misses += 1
        if p.misses >= 2:   # speech-to-text often mishears names; letters are easier to hear than a whole name
            say = ("No matching record again. Ask the caller to spell the first name one letter at a time, then call identify_patient "
                   "again with the spelled name. Do not offer a transfer unless the caller asks for a person.")
        else:
            say = "No matching record. Ask the caller to repeat the first name and the date of birth."
        return {"found": False, "ambiguous": bool(res.get("ambiguous")), "say": say}

    async def decline_patient(self, key):
        p = self.state.patient(key)
        if p:
            p.declined = True
        return {"ok": True}

    def _slots_held_by_others(self, key=None):
        """Slot ids already chosen or booked for another person on this call."""
        return {p.slot: (p.first_name or p.key) for p in self.state.patients
                if p.slot and p.key != key and not p.declined}

    async def find_slots(self, query="", limit=3):
        self._last_slot_query = query
        provs = self.kb.find_providers(query, k=5) if query else []
        held = self._slots_held_by_others()
        slots = await self.t.slots([p["id"] for p in provs] or None, None, limit + len(held))
        slots = [s for s in slots if s["slot_id"] not in held][:limit]
        return {"slots": slots}

    async def choose_slot(self, key, slot_id):
        p = self.state.patient(key)
        if not p:
            return {"ok": False, "error": "unknown patient key"}
        owner = self._slots_held_by_others(key).get(slot_id)
        if owner:
            # Two people cannot hold the same slot. Fail now, with fresh options, instead of failing later at booking.
            self.log.event("slot_reuse_blocked", level="warn", patient=key, held_by=owner)
            fresh = (await self.find_slots(getattr(self, "_last_slot_query", ""), 3))["slots"]
            return {"ok": False, "error": f"that exact slot is already taken for {owner} on this call",
                    "slots": fresh,
                    "say": "Do not book that slot. Offer the caller the first slot in 'slots' for this person (it is a different slot, "
                           "possibly the same time with another provider) and wait for their yes."}
        p.slot = slot_id
        return {"ok": True, "status": "slot_chosen_not_booked",
                "say": "Slot noted. This is NOT a booking yet. Call book_appointment for this person before saying anything is booked."}

    async def find_provider(self, query, city=""):
        # Doctors who are accepting new patients come first; others only fill the list if needed.
        provs = self.kb.find_providers(query, k=2, city=city or None, new_patients=True)
        if len(provs) < 2:
            seen = {p["id"] for p in provs}
            provs += [p for p in self.kb.find_providers(query, k=2, city=city or None) if p["id"] not in seen][: 2 - len(provs)]
        return {"providers": self.kb.format(provs)}

    async def book_appointment(self, key):
        s = self.state
        p = s.patient(key)
        if not p or not p.slot or not p.patient_id:
            return {"status": "failed", "error": "patient not identified or no slot chosen"}
        if p.status == BookingStatus.BOOKED.value:
            return {"status": "booked", "appointment_id": p.appointment_id, "note": "already booked, nothing done"}
        # One booking per caller turn unless the caller asked for everyone together. A model that asks a question and
        # then books on its own is stopped here, so no visit is made or changed without the caller's yes.
        if (self.one_booking_per_turn and s.books_this_turn_keys and p.key not in s.books_this_turn_keys
                and not s.multi_booking_ok):
            self.log.event("book_blocked_needs_caller_yes", level="warn", patient=p.key, already=list(s.books_this_turn_keys))
            return {"status": "not_booked", "error": "needs the caller's yes first",
                    "say": "Do not book this person yet. Tell the caller the time you propose for this person and wait for their yes."}
        if p.key not in s.books_this_turn_keys:
            s.books_this_turn_keys.append(p.key)
        p.idempotency_key = p.idempotency_key or make_key(s.call_id, p.key, p.slot, p.attempts)
        s.pending_tools.append("book_appointment")
        t0 = time.time()
        try:
            out = await reliable_book(self.t, s.call_id, p.patient_id, p.slot, p.idempotency_key, pause=self.book_pause)
        finally:
            s.pending_tools.remove("book_appointment")
        for e in out.events:
            self.log.event(e.pop("event"), level="warn" if "unknown" in str(e) or "gave_up" in str(e) else "info", patient=p.key, **e)
        p.status = out.status
        if out.status == "booked":
            p.appointment_id = out.appointment_id
            self.last_tool_error = False
            res = {"status": "booked", "appointment_id": out.appointment_id, "start": out.start}
        elif out.status == "failed":
            p.attempts += 1                      # new key only after a CONFIRMED failure
            p.idempotency_key, p.slot, p.last_error = "", "", out.error
            self.last_tool_error = True
            self.log.event("book_failed", level="error", patient=p.key, error=out.error)
            res = {"status": "failed", "error": out.error, "alternatives": out.alternatives,
                   "say": "It did not go through. Offer the alternative slots or a transfer."}
        else:
            self.last_tool_error = True
            self.log.event("book_pending", level="error", patient=p.key)
            res = {"status": "pending", "say": "Say you are still confirming this visit. Do NOT say it is booked. Offer a transfer or a callback."}
        res["ms"] = round((time.time() - t0) * 1000)
        return res

    async def read_back_summary(self):
        s = self.state
        s.readback_done = True
        lines = []
        for p in s.active_patients():
            if p.status == BookingStatus.BOOKED.value:
                lines.append(f"{p.first_name}: booked, {p.slot}")
            else:
                lines.append(f"{p.first_name}: NOT booked ({p.status})")
        return {"summary": lines}

    async def request_human_transfer(self, reason):
        s = self.state
        failing = self.last_tool_error or any(p.status in (BookingStatus.PENDING.value, BookingStatus.FAILED.value) for p in s.patients)
        if not (s.human_requested or failing):
            self.log.event("transfer_declined", level="warn", reason=(reason or "")[:200])
            return {"ok": False, "reason": "The caller did not ask for a person and nothing has failed. Do not transfer. "
                                           "Tell the caller what is still open and keep helping."}
        s.transfer_requested = True
        try:
            await self.t.transfer(s.call_id, reason)
        except Exception:
            self.log.event("transfer_log_failed", level="error")
        self.log.event("transfer_requested", reason=reason)
        return {"ok": True}

    def transfer_failed(self):
        """The transfer could not happen (no number or an error). Stop treating the call as escalated."""
        self.state.transfer_requested = False
        self.log.event("transfer_unavailable", level="warn")

    async def leave_callback(self, note):
        """Record a callback request. ok is False only if saving it failed; ref is the spoken reference (or None)."""
        try:
            ref = await self.t.callback(self.state.call_id, note)
        except Exception:
            self.log.event("callback_log_failed", level="error")
            return {"ok": False, "ref": None}
        self.state.callback_saved = True
        self.log.event("callback_logged", ref=ref or "")
        return {"ok": True, "ref": ref}

    def agent_said_goodbye(self):
        self.state.agent_goodbye_said = True

    def end_call_check(self):
        d = guards.can_end_call(self.state)
        self.log.event("end_call_check", allowed=d.allowed, reason=d.reason)
        if d.allowed:
            self.state.ended = True
        return d
