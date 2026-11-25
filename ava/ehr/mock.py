"""In-memory EHR for offline tests and evals. Synthetic data only.

It can copy the behavior measured on the athenahealth preview sandbox: a write that answers too late for a live call but
still lands on the server afterwards (commit_delay_s greater than submit_timeout_s)."""
import asyncio

from ava.ehr.base import EHRAdapter, Patient, Slot, TransportTimeout

PATIENTS = [
    Patient("P1", "Gary", "Testone", "1948-04-18", "02101"),
    Patient("P2", "Dorrie", "Testtwo", "1949-11-23", "02102"),
    # Two people with the same name and birthday: the identity check must ask for one more detail.
    Patient("P3", "Jovin", "Testthree", "1990-01-01", "02103"),
    Patient("P4", "Jovin", "Testthree", "1990-01-01", "90210"),
]


def default_slots():
    return [Slot(f"S{i}", "2026-12-01", f"{8 + i // 2:02d}:{'00' if i % 2 == 0 else '30'}", "D1", "1", "8") for i in range(8)]


class MockEHR(EHRAdapter):
    def __init__(self, patients=None, slots=None, commit_delay_s=0.0, submit_timeout_s=0.05,
                 reconcile_window_s=0.4, poll_interval_s=0.02, cancel_commit_delay_s=None, read_delay_s=0.0):
        super().__init__()
        self.patients = list(patients or PATIENTS)
        self.slots = {s.slot_id: s for s in (slots or default_slots())}
        self.state = {sid: {"state": "open", "patient_id": ""} for sid in self.slots}
        self.commit_delay_s = commit_delay_s          # server needs this long to apply a write
        self.cancel_commit_delay_s = cancel_commit_delay_s   # None: same as commit_delay_s
        self.submit_timeout_s = submit_timeout_s
        self.reconcile_window_s = reconcile_window_s
        self.poll_interval_s = poll_interval_s
        self.writes_sent = 0                            # every write that reached the "server", for no-double-write checks
        self.read_delay_s = read_delay_s              # simulated network time for reads
        self.refuse_next = False
        self.read_fails = 0                             # number of reads that raise before working again
        self._tasks = []

    async def find_patients(self, last_name, first_name=None, dob=None):
        if self.read_delay_s:
            await asyncio.sleep(self.read_delay_s)
        out = [p for p in self.patients if p.last_name.lower() == last_name.lower()
               and (first_name is None or p.first_name.lower() == first_name.lower())
               and (dob is None or p.dob == dob)]
        return out

    async def open_slots(self, department_id, appointment_type_id, start, end, limit=5):
        if self.read_delay_s:
            await asyncio.sleep(self.read_delay_s)
        return [s for sid, s in self.slots.items() if self.state[sid]["state"] == "open"
                and s.department_id == str(department_id) and s.appointment_type_id == str(appointment_type_id)][:limit]

    async def patient_appointments(self, patient_id, start, end):
        return [self.slots[sid] for sid, st in self.state.items()
                if st["state"] == "booked" and st["patient_id"] == patient_id and start <= self.slots[sid].date <= end]

    async def read_slot(self, slot_id):
        if self.read_fails > 0:
            self.read_fails -= 1
            raise ConnectionError("read failed")
        return dict(self.state.get(slot_id, {"state": "unknown", "patient_id": ""}))

    async def _apply(self, kind, slot_id, patient_id):
        delay = self.commit_delay_s if (kind == "book" or self.cancel_commit_delay_s is None) else self.cancel_commit_delay_s
        if delay:
            await asyncio.sleep(delay)
        self.writes_sent += 1
        cur = self.state[slot_id]
        if kind == "book":
            if cur["state"] == "booked" and cur["patient_id"] != patient_id:
                return "slot_taken"
            self.state[slot_id] = {"state": "booked", "patient_id": patient_id}
        else:
            if cur["state"] != "booked":
                return "not_found"
            self.state[slot_id] = {"state": "cancelled", "patient_id": patient_id}
        return "ok"

    async def _send(self, kind, slot_id, patient_id):
        if self.refuse_next:
            self.refuse_next = False
            raise RuntimeError("refused")
        task = asyncio.ensure_future(self._apply(kind, slot_id, patient_id))
        self._tasks.append(task)
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=self.submit_timeout_s)
        except asyncio.TimeoutError:
            raise TransportTimeout()

    async def _send_book(self, slot, patient):
        return await self._send("book", slot.slot_id, patient.patient_id)

    async def _send_cancel(self, appointment_id, patient_id):
        return await self._send("cancel", appointment_id, patient_id)

    async def settle(self):
        """Wait for writes still in flight (tests only)."""
        await asyncio.gather(*self._tasks, return_exceptions=True)


def simulated_practice(today, **kw):
    """The public demo records: SIMULATED, not a real EHR and not the athenahealth sandbox. Test people and times only.
    Slots run over the next weekdays after `today`; two test patients already hold one visit each so that
    cancel and reschedule can be tried. Must be labeled as simulated wherever it is shown."""
    import datetime as dt
    d0 = dt.date.fromisoformat(today)
    slots, n, day = [], 0, d0
    while len(slots) < 40:
        day += dt.timedelta(days=1)
        if day.weekday() >= 5:
            continue
        for hh, mm in (("09", "00"), ("10", "30"), ("14", "00"), ("15", "30")):
            slots.append(Slot(f"SIM{n:03d}", day.isoformat(), f"{hh}:{mm}", "D1", "1", "8"))
            n += 1
    kw.setdefault("commit_delay_s", 0.6)
    kw.setdefault("submit_timeout_s", 8.0)
    kw.setdefault("reconcile_window_s", 6.0)
    kw.setdefault("poll_interval_s", 0.3)
    kw.setdefault("read_delay_s", 0.35)
    ehr = MockEHR(slots=slots, **kw)
    ehr.state[slots[5].slot_id] = {"state": "booked", "patient_id": "P1"}
    ehr.state[slots[9].slot_id] = {"state": "booked", "patient_id": "P2"}
    return ehr
