"""Booking service. Fixes failures 5 and 6 on the backend side.

Contract with the agent:
  status "booked"  : the clinic record confirms it. The ONLY status that allows the words "you're booked".
  status "pending" : outcome unknown (timeout, network error). The agent says it is still confirming,
                     then reconciles by idempotency key before any retry.
  status "failed"  : confirmed NOT booked (slot gone, unknown patient). The agent says so and offers options.
  (lookup by key may also return "not_found": no row for that key right now, so a retry with the SAME key is safe.)

Safe retry: every attempt for the same booking reuses the SAME idempotency key, and the database
enforces uniqueness on it, so a retry after a timeout can never create a second appointment.
"""
import hashlib
import os
import time
from dataclasses import dataclass, field


class BackendTimeout(Exception):
    """The caller did not get a response in time. The write may or may not have happened."""


@dataclass
class Fault:
    """Test hook. commit_then_timeout=N: the next N bookings are COMMITTED, then the response is lost.
    This reproduces the baseline's failure exactly: the row is written, the caller sees a timeout."""
    commit_then_timeout: int = 0
    timeout_before_commit: int = 0
    identify_delay_s: float = 0.0     # slow lookup (failure 1). Live tests only.

    @classmethod
    def from_env(cls):
        """FAULT_IDENTIFY_DELAY_S, FAULT_COMMIT_THEN_TIMEOUT, FAULT_TIMEOUT_BEFORE_COMMIT. All default to off."""
        return cls(commit_then_timeout=int(os.getenv("FAULT_COMMIT_THEN_TIMEOUT", "0") or 0),
                   timeout_before_commit=int(os.getenv("FAULT_TIMEOUT_BEFORE_COMMIT", "0") or 0),
                   identify_delay_s=float(os.getenv("FAULT_IDENTIFY_DELAY_S", "0") or 0))

    def active(self):
        return bool(self.commit_then_timeout or self.timeout_before_commit or self.identify_delay_s)


@dataclass
class BookResult:
    status: str                      # booked | pending | failed
    appointment_id: str = ""
    outcome: str = ""                # created | replay | duplicate_prevented | slot_unavailable | ...
    error: str = ""
    start: str = ""
    provider_id: str = ""
    alternatives: list = field(default_factory=list)


def make_key(call_id, patient_key, slot_id, attempt_epoch=0):
    """Deterministic idempotency key. attempt_epoch only increases after a CONFIRMED failure,
    so a retry of an unknown outcome always reuses the same key."""
    raw = f"{call_id}|{patient_key}|{slot_id}|{attempt_epoch}"
    return "idem_" + hashlib.sha256(raw.encode()).hexdigest()[:20]


class BookingService:
    def __init__(self, store, fault=None):
        self.store = store
        self.fault = fault or Fault()

    def identify(self, first_name, dob):
        if self.fault.identify_delay_s > 0:
            time.sleep(self.fault.identify_delay_s)
        rows = self.store.identify(first_name, dob)
        if len(rows) == 1:
            r = rows[0]
            return {"found": True, "patient_id": r["id"], "first_name": r["first_name"]}
        if len(rows) > 1:
            return {"found": False, "ambiguous": True}
        return {"found": False}

    def book(self, call_id, patient_id, slot_id, key):
        if self.fault.timeout_before_commit > 0:
            self.fault.timeout_before_commit -= 1
            raise BackendTimeout("lost before commit")
        record, outcome = self.store.book_atomic(call_id, patient_id, slot_id, key)
        if self.fault.commit_then_timeout > 0:
            self.fault.commit_then_timeout -= 1
            raise BackendTimeout("committed, response lost")
        if outcome in ("created", "replay", "duplicate_prevented"):
            return BookResult("booked", record["appointment_id"], outcome, start=record["start"], provider_id=record["provider_id"])
        alts = self.store.find_slots(limit=3)
        return BookResult("failed", "", outcome, error=outcome, alternatives=alts)

    def lookup(self, key):
        rec = self.store.get_by_key(key)
        if not rec:
            # Not "failed": it only means no row exists for this key right now. Retrying with the SAME key is safe.
            return BookResult("not_found", "", "not_found")
        return BookResult("booked", rec["appointment_id"], "found", start=rec["start"], provider_id=rec["provider_id"])
