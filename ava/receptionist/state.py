"""Call state for the receptionist, OUTSIDE the model (same idea as ava.state for the clinic agent).

The workflow step is derived from this data on every turn, so a fallback model resumes at the same step.
Patient details stay in the identity gate and the EHR. This object keeps only ids and statuses, never names or birth dates."""
import json
import time
from dataclasses import dataclass, field
from enum import Enum

from ava.ehr.base import WriteStatus


class Step(str, Enum):
    GREET = "greet"
    VERIFY = "verify"
    THIRD_PARTY = "third_party"
    HELP = "help"
    CONFIRM = "confirm"
    URGENT = "urgent"
    WRAP_UP = "wrap_up"
    DONE = "done"


@dataclass
class Op:
    """One scheduling action. status is a WriteStatus value, or 'none' before anything was sent."""
    kind: str                       # book | cancel | reschedule
    slot_id: str = ""               # the NEW slot for book and reschedule
    old_appointment_id: str = ""    # for cancel and reschedule
    status: str = "none"
    appointment_id: str = ""
    key: str = ""
    announced: bool = False         # the outcome was told to the caller
    detail: str = ""

    def settled(self):
        return self.status in (WriteStatus.BOOKED.value, WriteStatus.CANCELLED.value, WriteStatus.FAILED.value,
                               WriteStatus.SLOT_TAKEN.value)


@dataclass
class ReceptionistState:
    call_id: str
    language: str = "en"
    greeted: bool = False
    gate: object = field(default=None, repr=False)          # IdentityGate; holds the verified patient
    ops: list = field(default_factory=list)
    my_appointments: list = field(default=None, repr=False)  # list of Slot, fetched after verification
    urgent: bool = False
    human_requested: bool = False
    transfer_requested: bool = False
    callback_saved: bool = False
    message_saved: bool = False
    caller_goodbye: bool = False
    agent_goodbye_said: bool = False
    language_just_switched: bool = False
    language_explicit: bool = False
    non_english_streak: int = 0
    english_streak: int = 0
    pending_tools: list = field(default_factory=list)
    pending_asked: bool = False
    silence_prompts_sent: int = 0
    turn_count: int = 0
    ended: bool = False
    created_at: float = field(default_factory=time.time)

    # ---- facts ----------------------------------------------------------
    @property
    def verified(self):
        return bool(self.gate and self.gate.may_share_phi())

    @property
    def third_party(self):
        return bool(self.gate and self.gate.on_behalf)

    def booked(self):
        """Confirmed scheduling results (book, reschedule). Name kept for the shared voice worker."""
        return [o for o in self.ops if o.kind in ("book", "reschedule") and o.status == WriteStatus.BOOKED.value]

    def confirmed(self, kind):
        want = {"book": WriteStatus.BOOKED.value, "reschedule": WriteStatus.BOOKED.value, "cancel": WriteStatus.CANCELLED.value}
        return [o for o in self.ops if o.kind == kind and o.status == want[kind]]

    def pending_ops(self):
        return [o for o in self.ops if o.status == WriteStatus.PENDING.value]

    def unannounced(self):
        return [o for o in self.ops if o.settled() and not o.announced]

    def has_pending_work(self):
        return bool(self.pending_tools or self.pending_ops())

    def active_patients(self):       # compatibility with the shared voice worker: this agent has no patient list
        return []

    patients = property(lambda self: [])

    @property
    def step(self):
        if self.ended:
            return Step.DONE
        if self.urgent:
            return Step.URGENT
        if not self.greeted:
            return Step.GREET
        if self.third_party:
            return Step.THIRD_PARTY
        if not self.verified:
            return Step.VERIFY
        if self.unannounced() or self.pending_ops():
            return Step.CONFIRM
        if self.ops:
            return Step.WRAP_UP
        return Step.HELP

    # ---- the line handed to WHICHEVER model answers the next turn ------
    def instruction(self):
        s = self.step
        out = [f"CURRENT STEP: {s.value}.", f"CALL LANGUAGE: {'Spanish' if self.language == 'es' else 'English'}."]
        if s == Step.URGENT:
            out.append("The caller may have a medical emergency. Tell them clearly to hang up and call 911 now. For a mental health "
                       "crisis they can call or text 988. Do not schedule anything and do not ask more questions. Offer to stay on the line "
                       "only until they have called.")
        elif s == Step.GREET:
            out.append("Greet the caller and ask how you can help. Do not call any tool yet.")
        elif s == Step.VERIFY:
            g = self.gate
            if g is not None and g.candidates and not g.patient:
                out.append("More than one person matches. Ask for the ZIP code and call add_verification_detail.")
            elif g is not None and g.locked:
                out.append("Verification is locked. Say you cannot verify them on this call and call request_human_transfer. "
                           "Share no details of any kind.")
            else:
                out.append("Before sharing or changing anything personal, verify the caller: ask for first name, last name and date of "
                           "birth, then call verify_identity. Never say which detail did not match. General questions about the "
                           "practice (hours, location, services) can be answered now with practice_info. If the caller says they are "
                           "calling for someone else, call caller_is_not_the_patient.")
        elif s == Step.THIRD_PARTY:
            out.append("The caller is NOT the patient. Share no appointment or health details and book or change nothing. You may answer "
                       "general practice questions with practice_info, take a message for the patient with take_message, or transfer to a "
                       "person if they ask.")
        elif s == Step.HELP:
            out.append("The caller is verified. Help with what they need: list_my_appointments, find_open_slots, book_visit, "
                       "cancel_visit, reschedule_visit, or take_message. Offer ONE time at a time and wait for a clear yes before booking, "
                       "cancelling or changing anything. A chosen time is NOT a booking.")
        elif s == Step.CONFIRM:
            out.append("Tell the caller the outcome of what was just done, using ONLY the tool result: booked, cancelled, moved, "
                       "still being confirmed, or did not go through. Then ask if there is anything else.")
        elif s == Step.WRAP_UP:
            out.append("Ask if there is anything else. Call end_call only after the caller's explicit goodbye.")
        pend = self.pending_ops()
        if pend:
            out.append(f"{len(pend)} change(s) are still being confirmed with the records system. Do NOT say they are done. "
                       "Say you are still confirming and that a person will follow up if it does not finish.")
        if self.language == "es":
            out.append("Speak ONLY Spanish. You CAN speak Spanish. Never tell the caller you can only use English.")
        if self.language_just_switched:
            out.append("The caller just asked to change language. Confirm in the new language in one short sentence, then continue.")
        out.append("RULE: never read out birth dates, ZIP codes or ID numbers back unless the caller asks. Never give medical advice.")
        out.append("RULE: never transfer the call unless the caller asks for a person, verification is locked, or a tool has failed.")
        return "\n".join(out)

    def to_json(self):
        return json.dumps({"call_id": self.call_id, "step": self.step.value, "ops": [o.__dict__ for o in self.ops],
                           "verified": self.verified, "urgent": self.urgent})
