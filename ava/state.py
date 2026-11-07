"""Call state that lives OUTSIDE the model.

Fixes failures 6 (multi-patient state) and 8 (fallback skips steps).

The current workflow step is DERIVED from the data below on every turn, never remembered by
the model. So when the large model times out and a fallback model takes over, it receives the
same step instruction built from the same state and resumes at the same step. Nothing to skip.
"""
from dataclasses import dataclass, field, asdict
from enum import Enum
import json
import time


class Step(str, Enum):
    GREET = "greet"
    IDENTIFY = "identify"
    VERIFY_DOB = "verify_dob"          # kept for parity with the baseline workflow
    COLLECT_PATIENTS = "collect_patients"
    CHOOSE_SLOT = "choose_slot"
    BOOK = "book"
    CONFIRM = "confirm"
    WRAP_UP = "wrap_up"
    DONE = "done"


class BookingStatus(str, Enum):
    NONE = "none"
    PENDING = "pending"     # outcome unknown (for example a timeout). NEVER say "booked".
    BOOKED = "booked"       # confirmed by the clinic record
    FAILED = "failed"       # confirmed not booked


@dataclass
class PatientEntry:
    key: str                         # "self", "child1", "child2", ...
    first_name: str = ""
    dob: str = ""                    # YYYY-MM-DD
    patient_id: str = ""             # set once the clinic record is matched
    slot: str = ""                   # chosen slot id
    status: str = BookingStatus.NONE.value
    idempotency_key: str = ""
    appointment_id: str = ""
    attempts: int = 0
    last_error: str = ""
    declined: bool = False           # caller said this person does not need an appointment

    def identified(self):
        return bool(self.patient_id)

    def collected(self):
        return bool(self.first_name and self.dob)


@dataclass
class CallState:
    call_id: str
    language: str = "en"
    greeted: bool = False
    expected_party_size: int = 0                 # 0 = not yet known
    patients: list = field(default_factory=list)  # list[PatientEntry]
    pending_tools: list = field(default_factory=list)
    caller_goodbye: bool = False
    agent_goodbye_said: bool = False
    transfer_requested: bool = False
    human_requested: bool = False               # the caller asked for a person
    books_this_turn_keys: list = field(default_factory=list)   # people booked since the caller last spoke
    multi_booking_ok: bool = False              # the caller said to book everyone together this turn
    ended: bool = False
    turn_count: int = 0
    non_english_streak: int = 0
    english_streak: int = 0
    silence_prompts_sent: int = 0
    readback_done: bool = False
    created_at: float = field(default_factory=time.time)

    # ---- patients -------------------------------------------------------
    def patient(self, key):
        for p in self.patients:
            if p.key == key:
                return p
        return None

    def add_patient(self, key, first_name="", dob=""):
        """Add or update one patient. Re-adding the same key updates it, never duplicates it."""
        p = self.patient(key)
        if p is None:
            p = PatientEntry(key=key)
            self.patients.append(p)
        if first_name:
            p.first_name = first_name
        if dob:
            p.dob = dob
        return p

    # ---- derived facts --------------------------------------------------
    def booked(self):
        return [p for p in self.patients if p.status == BookingStatus.BOOKED.value]

    def pending_patients(self):
        return [p for p in self.patients if p.status == BookingStatus.PENDING.value]

    def needs_booking(self):
        """Patients with a chosen slot whose outcome is not settled."""
        return [p for p in self.patients
                if not p.declined and p.slot and p.status in (BookingStatus.NONE.value, BookingStatus.PENDING.value)]

    def has_pending_work(self):
        """True if hanging up now would drop work. Used by the end_call guard."""
        if self.pending_tools:
            return True
        if self.needs_booking():
            return True
        if self.expected_party_size and len(self.active_patients()) < self.expected_party_size - self.declined_count():
            return True
        # any patient started but never given a slot or a final outcome
        for p in self.patients:
            if p.declined:
                continue
            if p.status in (BookingStatus.NONE.value, BookingStatus.PENDING.value):
                return True
        return False

    def active_patients(self):
        return [p for p in self.patients if not p.declined]

    def declined_count(self):
        return sum(1 for p in self.patients if p.declined)

    @property
    def step(self):
        """The workflow step, derived from data. The model never owns this."""
        if self.ended:
            return Step.DONE
        if not self.greeted:
            return Step.GREET
        me = self.patient("self")
        if me is None or not me.collected() or not me.identified():
            return Step.IDENTIFY
        if self.expected_party_size == 0:
            return Step.COLLECT_PATIENTS
        act = self.active_patients()
        if len(self.patients) < self.expected_party_size or any(not p.collected() for p in act):
            return Step.COLLECT_PATIENTS
        if any(not p.identified() for p in act):
            return Step.COLLECT_PATIENTS
        if any(not p.slot and p.status == BookingStatus.NONE.value for p in act):
            return Step.CHOOSE_SLOT
        if self.needs_booking():
            return Step.BOOK
        if (self.booked() or self.pending_patients()) and not self.readback_done:
            return Step.CONFIRM
        return Step.WRAP_UP

    # ---- instruction handed to WHICHEVER model answers the next turn ---
    def instruction(self):
        s = self.step
        lines = [f"CURRENT STEP: {s.value}.", f"CALL LANGUAGE: {'Spanish' if self.language == 'es' else 'English'}."]
        known = ", ".join(
            f"{p.key}={p.first_name or '?'}(dob {'yes' if p.dob else 'no'}, id {'yes' if p.identified() else 'no'}, "
            f"slot {p.slot or 'none'}, status {p.status})" for p in self.patients) or "none yet"
        lines.append(f"PATIENTS ON THIS CALL: {known}.")
        if self.expected_party_size:
            lines.append(f"CALLER SAID THE PARTY HAS {self.expected_party_size} PEOPLE.")
        if s == Step.GREET:
            lines.append("Greet the caller and ask how you can help. Do not call any tool yet.")
        elif s == Step.IDENTIFY:
            lines.append("Get the caller's first name and date of birth, then call identify_patient with key 'self'.")
        elif s == Step.COLLECT_PATIENTS:
            missing = [p.key for p in self.patients if not p.collected() or not p.identified()]
            need = max(0, self.expected_party_size - len(self.patients))
            lines.append("Collect each remaining person's first name and date of birth, then call add_patient and "
                         f"identify_patient for each. Still missing details for: {missing or 'none'}; "
                         f"{need} more people not yet added. If the party size is unknown, ask who needs an appointment.")
        elif s == Step.CHOOSE_SLOT:
            lines.append("Offer slots with find_slots and let the caller choose one slot per person. After the caller picks a slot "
                         "for someone, call choose_slot, then book_appointment for that person. A chosen slot is NOT a booking: "
                         "never say booked, set, scheduled or confirmed until book_appointment returns status 'booked'. "
                         "Offer a time for ONE person at a time and wait for the caller's yes before booking that person. "
                         "Never change a person's time without the caller's yes. If you asked the caller a question, "
                         "stop and wait for the answer before any tool call.")
        elif s == Step.BOOK:
            nxt = self.needs_booking()[0]
            lines.append(f"Call book_appointment for patient key '{nxt.key}' now. Book ONE patient at a time. "
                         "Never retry by calling for the same person with a new request; the tool handles safe retries. "
                         "Only book a person whose time the caller has just agreed to.")
        elif s in (Step.CONFIRM, Step.WRAP_UP):
            if s == Step.CONFIRM:
                lines.append("Call read_back_summary and read its result to the caller. Then ask if there is anything else.")
            else:
                lines.append("Ask if there is anything else. Do not say goodbye until the caller says they are done. "
                             "Call end_call only after the caller's explicit goodbye.")
        if self.caller_goodbye and self.has_pending_work() and not self.pending_tools:
            open_names = [p.first_name or p.key for p in self.active_patients() if p.status != BookingStatus.BOOKED.value]
            who = ", ".join(open_names) or "someone"
            lines.append(f"THE CALLER WANTS TO FINISH, BUT {who} STILL HAS NO APPOINTMENT. Do NOT say goodbye and do NOT call end_call "
                         f"in this reply. Ask ONE short question: should {who} be left without a visit, or booked now? "
                         f"If the caller says leave {who} without one, call decline_patient for that person, then say goodbye and call end_call.")
        lines.append("RULE: never transfer the call unless the caller asks for a person or a tool has failed.")
        lines.append("RULE: say a visit is booked ONLY for patients whose status above is 'booked'. "
                     "If a status is 'pending', say you are still confirming it. If 'failed', say it did not go through "
                     "and offer to try again or transfer to a person.")
        return "\n".join(lines)

    # ---- persistence ------------------------------------------------------
    def to_json(self):
        d = asdict(self)
        return json.dumps(d)

    @classmethod
    def from_json(cls, raw):
        d = json.loads(raw)
        pts = [PatientEntry(**p) for p in d.pop("patients", [])]
        st = cls(**d)
        st.patients = pts
        return st
