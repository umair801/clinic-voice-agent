"""Voice side of the medical practice receptionist. Thin: every rule lives in ava.receptionist.core (shared with the evals).
Imported lazily by the worker entrypoint when the agent profile has kind "receptionist"."""
import datetime as dt
import json
import os

from livekit.agents import JobContext, RunContext, function_tool

from ava.ehr.athena import AthenaEHR
from ava.ehr.mock import MockEHR, simulated_practice
from ava.hipaa.audit import AuditLog
from ava.hipaa.retention import purge
from ava.receptionist.core import MessageStore, ReceptionistCore
from ava.receptionist.guard import ReceptionistGuard, violations
from ava.voice.agent import BOOK_FILLER, LOOKUP_FILLER, SHORT_FILLER, VoiceAgentBase, ROOT

FILLERS = {"verify_identity": LOOKUP_FILLER, "add_verification_detail": LOOKUP_FILLER, "list_my_appointments": LOOKUP_FILLER,
           "find_open_slots": LOOKUP_FILLER, "book_visit": BOOK_FILLER, "cancel_visit": BOOK_FILLER, "reschedule_visit": BOOK_FILLER,
           "take_message": SHORT_FILLER, "practice_info": SHORT_FILLER, "caller_is_not_the_patient": SHORT_FILLER}


def build_ehr(cfg, override=None):
    e = cfg.get("ehr", {})
    mode = override or e.get("mode", "simulated")
    if mode == "simulated":            # public demo records: SIMULATED, labeled as such on the page and in the log
        return simulated_practice(dt.date.today().isoformat())
    if mode == "athena_sandbox":       # real HTTP calls to the athenahealth PREVIEW sandbox, synthetic data
        return AthenaEHR.from_env(submit_timeout_s=float(e.get("submit_timeout_s", 8.0)),
                                  reconcile_window_s=float(e.get("reconcile_window_s", 20.0)))
    if mode == "mock":                 # simulated records, for offline demos only. Must be labeled as simulated wherever shown.
        return MockEHR()
    raise ValueError(f"unknown ehr mode {mode!r}")


def build_receptionist(call_id, cfg, prompt, llms, manifest, ctx: JobContext, raw_logger, ehr_override=None):
    comp = cfg.get("compliance", {})
    salt = os.getenv("AUDIT_SALT", "")
    if not salt:
        raw_logger.event("audit_salt_missing", level="warn")
        salt = "dev-salt"
    audit = AuditLog(os.path.join(ROOT, comp.get("audit_log", "logs/audit/receptionist.jsonl")), salt=salt)
    messages_dir = os.path.join(ROOT, comp.get("messages_dir", "logs/messages"))
    ehr_cfg = cfg.get("ehr", {})
    practice = json.load(open(os.path.join(ROOT, "agents", "receptionist", "practice.json")))
    days = int(comp.get("retention_days", 30))
    old = purge(messages_dir, days, apply=False)      # dry run only here: listing is logged, deletion is an explicit operator action
    if old:
        raw_logger.event("retention_due", level="warn", files=len(old), days=days)
    mode = ehr_override or ehr_cfg.get("mode", "simulated")
    core = ReceptionistCore(call_id, build_ehr(cfg, ehr_override), audit=audit, logger=raw_logger, messages=MessageStore(messages_dir),
                            practice=practice, today=dt.date.today().isoformat(), department_id=ehr_cfg.get("department_id", "1"),
                            appointment_type_id=ehr_cfg.get("appointment_type_id", "8"), horizon_days=int(ehr_cfg.get("horizon_days", 30)))
    raw_logger.event("ehr_mode", mode=mode, simulated=(mode == "simulated"))
    return core, ReceptionistAgent(core, prompt, llms, cfg, manifest, ctx)


class ReceptionistAgent(VoiceAgentBase):
    guard_cls = ReceptionistGuard
    violations_fn = staticmethod(violations)
    fillers = FILLERS

    def _filler_for(self, name, ctx):
        lang = self.core.state.language
        return ctx.with_filler(self._logged_filler(FILLERS[name][lang], name), delay=1.0, interval=4.0, max_steps=3)

    @function_tool
    async def verify_identity(self, context: RunContext, first_name: str, last_name: str, date_of_birth: str):
        """Verify the caller. date_of_birth is YYYY-MM-DD. Never say which detail did not match."""
        self._record("verify_identity")
        async with self._filler_for("verify_identity", context):
            return await self.core.verify_identity(first_name, last_name, date_of_birth)

    @function_tool
    async def add_verification_detail(self, context: RunContext, zip_code: str):
        """Only when verify_identity says more than one person matches: the caller's ZIP code."""
        self._record("add_verification_detail")
        return await self.core.add_verification_detail(zip_code)

    @function_tool
    async def caller_is_not_the_patient(self, context: RunContext):
        """The caller says they are calling for someone else. No appointment or health details may be shared."""
        self._record("caller_is_not_the_patient")
        return await self.core.caller_is_not_the_patient()

    @function_tool
    async def list_my_appointments(self, context: RunContext):
        """The verified caller's upcoming appointments."""
        self._record("list_my_appointments")
        async with self._filler_for("list_my_appointments", context):
            return await self.core.list_my_appointments()

    @function_tool
    async def find_open_slots(self, context: RunContext, reason: str = ""):
        """Find the next open appointment times for a verified caller."""
        self._record("find_open_slots")
        async with self._filler_for("find_open_slots", context):
            return await self.core.find_open_slots(reason)

    @function_tool
    async def book_visit(self, context: RunContext, slot_id: str):
        """Book an offered time, only after the caller's clear yes. Say 'booked' only if the status returned is 'booked'."""
        self._record("book_visit")
        async with self._filler_for("book_visit", context):
            return await self.core.book_visit(slot_id)

    @function_tool
    async def cancel_visit(self, context: RunContext, appointment_id: str):
        """Cancel one of the caller's upcoming visits, only after the caller's clear yes."""
        self._record("cancel_visit")
        async with self._filler_for("cancel_visit", context):
            return await self.core.cancel_visit(appointment_id)

    @function_tool
    async def reschedule_visit(self, context: RunContext, old_appointment_id: str, new_slot_id: str):
        """Move a visit to an offered time, only after the caller's clear yes. The new time is booked first, then the old one cancelled."""
        self._record("reschedule_visit")
        async with self._filler_for("reschedule_visit", context):
            return await self.core.reschedule_visit(old_appointment_id, new_slot_id)

    @function_tool
    async def take_message(self, context: RunContext, message: str, callback_number_if_given: str = ""):
        """Save a message for the staff. Works for any caller."""
        self._record("take_message")
        return await self.core.take_message(message, callback_number_if_given)

    @function_tool
    async def practice_info(self, context: RunContext, topic: str):
        """General information: hours, location, services, new patients, after hours, cancellation policy, insurance."""
        self._record("practice_info")
        return await self.core.practice_info(topic)
