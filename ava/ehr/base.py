"""EHR adapter interface plus the booking policy that must hold for every EHR (failure 5 again, with a real EHR).

Measured on the athenahealth preview sandbox (2026-11-25): reads answer in about 1.5 s, but a book or cancel call can
take over 60 s, and the change still lands on the server AFTER a client has timed out. So a timeout is NOT a failure.
The policy here, shared by the mock and the real adapter, is:

  1. Before writing, read the slot. Already booked for this patient: return booked (idempotent hit). Booked for
     someone else: slot_taken. Never write blindly.
  2. Send the write with a short timeout. An answer from the EHR is still verified by a read before "booked" is said.
  3. A timeout or unknown answer means PENDING, never failed. Reconcile by READING until the record shows the result
     or the window ends. The write is never sent a second time on a guess.
  4. Same idempotency key, same answer: a repeated request cannot book twice.

Only the minimum patient fields are kept. Raw EHR records contain much more (address, licence, balances) and must not
travel further than the adapter.
"""
import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum


class WriteStatus(str, Enum):
    BOOKED = "booked"
    PENDING = "pending"          # sent, outcome not confirmed yet: say "still confirming"
    FAILED = "failed"            # the EHR refused it: safe to offer another time
    SLOT_TAKEN = "slot_taken"    # someone else holds the slot: offer another time
    CANCELLED = "cancelled"


class TransportTimeout(Exception):
    """The write got no answer in time. The EHR may still apply it."""


@dataclass(frozen=True)
class Patient:
    patient_id: str
    first_name: str
    last_name: str
    dob: str            # ISO date, YYYY-MM-DD
    zip: str = ""


@dataclass(frozen=True)
class Slot:
    slot_id: str
    date: str           # ISO date
    start_time: str     # HH:MM
    provider_id: str
    department_id: str
    appointment_type_id: str


@dataclass
class WriteResult:
    status: WriteStatus
    appointment_id: str = ""
    detail: str = ""            # short reason code for logs, never patient data
    reconciled: bool = False    # True when the result came from reading the record after a timeout
    elapsed_s: float = 0.0


class EHRAdapter(ABC):
    submit_timeout_s = 8.0        # how long a live call waits for a write answer
    reconcile_window_s = 20.0     # how long a live call keeps reading before it answers "pending"
    poll_interval_s = 2.0

    def __init__(self):
        self._memo = {}           # idempotency key -> WriteResult (in memory; a durable store comes with the worker wiring)
        self._locks = {}

    # ---- transport: implemented per EHR -----------------------------------
    @abstractmethod
    async def find_patients(self, last_name, first_name=None, dob=None):
        """Return a list of Patient (minimal fields only)."""

    @abstractmethod
    async def open_slots(self, department_id, appointment_type_id, start, end, limit=5):
        """Return a list of Slot."""

    @abstractmethod
    async def patient_appointments(self, patient_id, start, end):
        """Booked (future) appointments of ONE verified patient, as a list of Slot. Read only."""

    @abstractmethod
    async def read_slot(self, slot_id):
        """Return {"state": "open"|"booked"|"cancelled"|"other"|"unknown", "patient_id": str}. A read, so it is fast and safe."""

    @abstractmethod
    async def _send_book(self, slot, patient):
        """Send the write. Return "ok" or "slot_taken", or raise TransportTimeout. Raise any other error for a refusal."""

    @abstractmethod
    async def _send_cancel(self, appointment_id, patient_id):
        """Send the write. Return "ok" or "not_found", or raise TransportTimeout."""

    # ---- policy: shared ----------------------------------------------------
    def _lock(self, key):
        return self._locks.setdefault(key, asyncio.Lock())

    async def book(self, slot, patient, idempotency_key):
        async with self._lock(idempotency_key):
            prior = self._memo.get(idempotency_key)
            if prior is not None and prior.status != WriteStatus.PENDING:
                return prior                    # same key, same answer: it cannot book twice
            t0 = time.monotonic()
            if prior is not None:               # a write is already in flight or unconfirmed: only READ, never write again
                res = await self.reconcile_book(slot, patient, t0=t0, detail="reconciled_after_pending")
            else:
                res = await self._book_once(slot, patient, t0)
            self._memo[idempotency_key] = res
            return res

    async def _book_once(self, slot, patient, t0):
        cur = await self.read_slot(slot.slot_id)
        if cur["state"] == "booked":
            who = cur["patient_id"] == patient.patient_id
            return WriteResult(WriteStatus.BOOKED if who else WriteStatus.SLOT_TAKEN, slot.slot_id,
                               "already_booked_for_this_patient" if who else "booked_by_someone_else",
                               elapsed_s=time.monotonic() - t0)
        try:
            outcome = await self._send_book(slot, patient)
        except TransportTimeout:
            return await self.reconcile_book(slot, patient, t0=t0, detail="write_timeout")
        except Exception as e:   # the EHR refused: nothing was written, but confirm by reading before saying failed
            cur = await self._safe_read(slot.slot_id)
            if cur and cur["state"] == "booked" and cur["patient_id"] == patient.patient_id:
                return WriteResult(WriteStatus.BOOKED, slot.slot_id, "booked_despite_error", True, time.monotonic() - t0)
            return WriteResult(WriteStatus.FAILED, "", f"refused:{type(e).__name__}", elapsed_s=time.monotonic() - t0)
        if outcome == "slot_taken":
            return WriteResult(WriteStatus.SLOT_TAKEN, "", "slot_taken", elapsed_s=time.monotonic() - t0)
        return await self.reconcile_book(slot, patient, t0=t0, detail="verified_by_read")

    async def reconcile_book(self, slot, patient, window_s=None, t0=None, detail="reconcile"):
        """Read until the record shows the booking. Never writes. Returns booked or pending."""
        t0 = t0 or time.monotonic()
        window_s = self.reconcile_window_s if window_s is None else window_s
        while True:
            cur = await self._safe_read(slot.slot_id)
            if cur and cur["state"] == "booked":
                ok = cur["patient_id"] == patient.patient_id
                return WriteResult(WriteStatus.BOOKED if ok else WriteStatus.SLOT_TAKEN, slot.slot_id if ok else "",
                                   detail if ok else "booked_by_someone_else", True, time.monotonic() - t0)
            if time.monotonic() - t0 >= window_s:
                return WriteResult(WriteStatus.PENDING, slot.slot_id, "not_confirmed_in_window", True, time.monotonic() - t0)
            await asyncio.sleep(self.poll_interval_s)

    async def cancel(self, appointment_id, patient_id, idempotency_key):
        async with self._lock(idempotency_key):
            prior = self._memo.get(idempotency_key)
            if prior is not None and prior.status != WriteStatus.PENDING:
                return prior
            t0 = time.monotonic()
            cur = await self.read_slot(appointment_id)
            if prior is not None:               # pending before: only READ, never send the cancel again
                res = await self.reconcile_cancel(appointment_id, t0=t0, detail="reconciled_after_pending")
            elif cur["state"] == "cancelled":
                res = WriteResult(WriteStatus.CANCELLED, appointment_id, "already_cancelled", elapsed_s=time.monotonic() - t0)
            elif cur["state"] == "booked" and cur["patient_id"] != patient_id:
                res = WriteResult(WriteStatus.FAILED, appointment_id, "not_this_patients_appointment", elapsed_s=time.monotonic() - t0)
            else:
                try:
                    outcome = await self._send_cancel(appointment_id, patient_id)
                    res = (await self.reconcile_cancel(appointment_id, t0=t0)) if outcome == "ok" else \
                        WriteResult(WriteStatus.FAILED, appointment_id, outcome, elapsed_s=time.monotonic() - t0)
                except TransportTimeout:
                    res = await self.reconcile_cancel(appointment_id, t0=t0, detail="write_timeout")
                except Exception as e:
                    res = WriteResult(WriteStatus.FAILED, appointment_id, f"refused:{type(e).__name__}", elapsed_s=time.monotonic() - t0)
            self._memo[idempotency_key] = res
            return res

    async def reconcile_cancel(self, appointment_id, window_s=None, t0=None, detail="verified_by_read"):
        t0 = t0 or time.monotonic()
        window_s = self.reconcile_window_s if window_s is None else window_s
        while True:
            cur = await self._safe_read(appointment_id)
            if cur and cur["state"] == "cancelled":
                return WriteResult(WriteStatus.CANCELLED, appointment_id, detail, True, time.monotonic() - t0)
            if time.monotonic() - t0 >= window_s:
                return WriteResult(WriteStatus.PENDING, appointment_id, "not_confirmed_in_window", True, time.monotonic() - t0)
            await asyncio.sleep(self.poll_interval_s)

    async def reschedule(self, old_appointment_id, new_slot, patient, idempotency_key):
        """Book the new time FIRST, cancel the old one only after the new one is confirmed, so the patient is never left
        with no appointment. Both steps are checked by reads. Uses only the book and cancel calls verified on the sandbox."""
        new = await self.book(new_slot, patient, idempotency_key + ":new")
        if new.status != WriteStatus.BOOKED:
            return new, None
        old = await self.cancel(old_appointment_id, patient.patient_id, idempotency_key + ":old")
        return new, old

    async def _safe_read(self, slot_id):
        try:
            return await self.read_slot(slot_id)
        except Exception:
            return None   # a failed read is not evidence either way
