"""Reliable booking from the agent side. Fixes failure 5 (false "booked", silent retry).

Given a transport (HTTP in production, in-process in evals), this:
  1. sends the booking with a fixed idempotency key and a timeout budget;
  2. on timeout or transport error, returns "pending" to the model (never "booked");
  3. reconciles by key (asks the clinic record whether that key exists) BEFORE any retry;
  4. retries with the SAME key, bounded, so the database cannot create a duplicate;
  5. returns the truth: booked, pending (still unknown after the budget), or failed.
Every attempt is logged for the call log and alerts.
"""
import asyncio
from dataclasses import dataclass, field

from ava.backend.booking import BackendTimeout, BookResult

BOOK_TIMEOUT_S = 6.0
LOOKUP_TIMEOUT_S = 3.0
MAX_ATTEMPTS = 3
RECONCILE_PAUSE_S = 0.3


class TransportError(Exception):
    pass


@dataclass
class ReliableOutcome:
    status: str                      # booked | pending | failed
    appointment_id: str = ""
    outcome: str = ""
    error: str = ""
    attempts: int = 0
    reconciled: bool = False
    start: str = ""
    alternatives: list = field(default_factory=list)
    events: list = field(default_factory=list)   # for the call log


async def reliable_book(transport, call_id, patient_id, slot_id, key, pause=RECONCILE_PAUSE_S):
    out = ReliableOutcome(status="pending")
    for attempt in range(1, MAX_ATTEMPTS + 1):
        out.attempts = attempt
        try:
            res = await asyncio.wait_for(transport.book(call_id, patient_id, slot_id, key), BOOK_TIMEOUT_S)
            out.events.append({"event": "book_response", "attempt": attempt, "status": res.status, "outcome": res.outcome})
            return _from_result(res, out)
        except (BackendTimeout, TransportError, asyncio.TimeoutError) as e:
            out.events.append({"event": "book_unknown_outcome", "attempt": attempt, "error": type(e).__name__})
        # Unknown outcome. Ask the record BEFORE retrying.
        try:
            look = await asyncio.wait_for(transport.lookup(key), LOOKUP_TIMEOUT_S)
            out.events.append({"event": "reconcile", "attempt": attempt, "status": look.status})
            if look.status == "booked":
                out.reconciled = True
                return _from_result(look, out)
        except (BackendTimeout, TransportError, asyncio.TimeoutError) as e:
            out.events.append({"event": "reconcile_failed", "attempt": attempt, "error": type(e).__name__})
        if pause:
            await asyncio.sleep(pause)
    out.status = "pending"
    out.error = "outcome unknown after retries"
    out.events.append({"event": "gave_up_pending", "attempts": out.attempts})
    return out


def _from_result(res, out):
    out.status = res.status
    out.appointment_id = res.appointment_id
    out.outcome = res.outcome
    out.error = res.error
    out.start = res.start
    out.alternatives = res.alternatives
    return out
