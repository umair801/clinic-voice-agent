"""Clinic records store. Synthetic data only.

Two implementations of one interface:
  SQLiteStore    used for local runs and all offline tests. Real SQL, real UNIQUE constraint.
  SupabaseStore  same interface over Supabase REST plus one SQL function (scripts/supabase_schema.sql).
                 WRITTEN BUT NOT YET RUN against a live project. Treat as untested until you run the
                 Supabase checks (scripts/seed_supabase.py).

The idempotency guarantee lives HERE, in the database: appointments.idempotency_key is UNIQUE and
the slot claim plus the appointment insert happen in one transaction.
"""
import sqlite3
import threading
import datetime as dt

from clinic.data import FAMILY, build_providers, FIRST, LAST

BASE_DAY = dt.date(2026, 11, 12)   # fixed, so seeded data and evals are deterministic
SLOT_PROVIDERS = 60                # providers that get slots
SLOT_DAYS = 5
SLOT_TIMES = [f"{h:02d}:{m:02d}" for h in range(9, 17) for m in (0, 30)]


def seed_patients():
    rows = [(p["id"], p["first"], p["last"], p["dob"]) for p in FAMILY.values()]
    # a few more synthetic patients so lookups are not trivially one family
    import random
    rng = random.Random(580)
    for i in range(30):
        rows.append((f"pt_{2000 + i}", rng.choice(FIRST), rng.choice(LAST),
                     f"{rng.randint(1950, 2018)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"))
    return rows


def seed_slots():
    out = []
    for p in build_providers(400)[:SLOT_PROVIDERS]:
        for d in range(SLOT_DAYS):
            day = BASE_DAY + dt.timedelta(days=d)
            if day.weekday() >= 5:
                continue
            for t in SLOT_TIMES:
                out.append((f"slot_{p['id']}_{day.strftime('%Y%m%d')}_{t.replace(':', '')}", p["id"], f"{day.isoformat()}T{t}"))
    return out



def fold_name(name):
    """Lower-case and strip accents so 'María' matches 'Maria'. Spanish speech-to-text and the model often add accents."""
    import unicodedata
    n = unicodedata.normalize("NFKD", (name or "").strip())
    return "".join(ch for ch in n if not unicodedata.combining(ch)).lower()


def _sound_key(name):
    """Spoken-name key: 'Sophia' and 'Sofia', 'Lukas' and 'Lucas' sound the same. Speech-to-text picks one spelling."""
    import re
    n = fold_name(name)
    n = n.replace("ph", "f").replace("ck", "k").replace("c", "k").replace("y", "i")
    return re.sub(r"(.)\1+", r"\1", n)


def pick_matches(rows, first_name):
    """Records for one exact date of birth. Exact (accent-insensitive) first-name match wins; otherwise a name that
    sounds the same or is within a small spelling distance. The date of birth must always match exactly."""
    from difflib import SequenceMatcher
    want = fold_name(first_name)
    exact = [x for x in rows if fold_name(x["first_name"]) == want]
    if exact:
        return exact
    k = _sound_key(first_name)
    return [x for x in rows if _sound_key(x["first_name"]) == k
            or (len(k) >= 4 and SequenceMatcher(None, _sound_key(x["first_name"]), k).ratio() >= 0.86)]


class SQLiteStore:
    def __init__(self, path=":memory:"):
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._init()

    def _init(self):
        c = self.db
        c.executescript("""
        CREATE TABLE IF NOT EXISTS patients(id TEXT PRIMARY KEY, first_name TEXT, last_name TEXT, dob TEXT);
        CREATE INDEX IF NOT EXISTS patients_name_dob ON patients(lower(first_name), dob);
        CREATE TABLE IF NOT EXISTS slots(slot_id TEXT PRIMARY KEY, provider_id TEXT, start TEXT, taken_by TEXT);
        CREATE TABLE IF NOT EXISTS appointments(
            appointment_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
            call_id TEXT, patient_id TEXT, slot_id TEXT, provider_id TEXT, start TEXT, status TEXT, created_at TEXT);
        CREATE TABLE IF NOT EXISTS transfers(id INTEGER PRIMARY KEY AUTOINCREMENT, call_id TEXT, reason TEXT, created_at TEXT);
        CREATE TABLE IF NOT EXISTS callbacks(id INTEGER PRIMARY KEY AUTOINCREMENT, call_id TEXT, note TEXT, created_at TEXT);
        CREATE TABLE IF NOT EXISTS call_logs(id INTEGER PRIMARY KEY AUTOINCREMENT, call_id TEXT, ts REAL, kind TEXT, event TEXT, data TEXT);
        """)
        if not c.execute("SELECT 1 FROM patients LIMIT 1").fetchone():
            c.executemany("INSERT INTO patients VALUES (?,?,?,?)", seed_patients())
            c.executemany("INSERT INTO slots(slot_id,provider_id,start,taken_by) VALUES (?,?,?,NULL)", seed_slots())
            c.commit()

    # ---- reads ------------------------------------------------------------
    def identify(self, first_name, dob):
        r = self.db.execute("SELECT * FROM patients WHERE dob=?", (dob,)).fetchall()
        return pick_matches([dict(x) for x in r], first_name)

    def get_by_key(self, key):
        r = self.db.execute("SELECT * FROM appointments WHERE idempotency_key=?", (key,)).fetchone()
        return dict(r) if r else None

    def appointments_for_patient(self, patient_id):
        return [dict(x) for x in self.db.execute("SELECT * FROM appointments WHERE patient_id=?", (patient_id,))]

    def all_appointments(self):
        return [dict(x) for x in self.db.execute("SELECT * FROM appointments")]

    def patient_exists(self, patient_id):
        return self.db.execute("SELECT 1 FROM patients WHERE id=?", (patient_id,)).fetchone() is not None

    def find_slots(self, provider_ids=None, after=None, limit=3):
        q = "SELECT slot_id, provider_id, start FROM slots WHERE taken_by IS NULL"
        args = []
        if provider_ids:
            q += f" AND provider_id IN ({','.join('?' * len(provider_ids))})"
            args += list(provider_ids)
        if after:
            q += " AND start >= ?"
            args.append(after)
        q += " ORDER BY start, provider_id LIMIT ?"
        args.append(limit)
        return [dict(x) for x in self.db.execute(q, args)]

    # ---- the one write that matters --------------------------------------
    def book_atomic(self, call_id, patient_id, slot_id, key):
        """Claim the slot and insert the appointment in ONE transaction. Returns (record, outcome).

        outcome: "created" | "replay" (same key seen before) | "duplicate_prevented" (same patient,
        same slot, different key) | "slot_unavailable" | "unknown_patient" | "unknown_slot".
        """
        with self.lock:
            existing = self.get_by_key(key)
            if existing:
                return existing, "replay"
            if not self.patient_exists(patient_id):
                return None, "unknown_patient"
            slot = self.db.execute("SELECT * FROM slots WHERE slot_id=?", (slot_id,)).fetchone()
            if not slot:
                return None, "unknown_slot"
            if slot["taken_by"]:
                mine = self.db.execute("SELECT * FROM appointments WHERE slot_id=? AND patient_id=?", (slot_id, patient_id)).fetchone()
                if mine:
                    return dict(mine), "duplicate_prevented"
                return None, "slot_unavailable"
            appt_id = f"apt_{len(self.all_appointments()) + 1:05d}"
            now = dt.datetime(2026, 11, 5, 9, 0, 0).isoformat()
            try:
                with self.db:
                    self.db.execute("UPDATE slots SET taken_by=? WHERE slot_id=? AND taken_by IS NULL", (appt_id, slot_id))
                    self.db.execute("INSERT INTO appointments VALUES (?,?,?,?,?,?,?,?,?)",
                                    (appt_id, key, call_id, patient_id, slot_id, slot["provider_id"], slot["start"], "booked", now))
            except sqlite3.IntegrityError:
                return self.get_by_key(key), "replay"
            return self.get_by_key(key), "created"

    def log_transfer(self, call_id, reason):
        with self.lock, self.db:
            self.db.execute("INSERT INTO transfers(call_id,reason,created_at) VALUES (?,?,?)", (call_id, reason, "n/a"))

    def log_callback(self, call_id, note):
        with self.lock, self.db:
            self.db.execute("INSERT INTO callbacks(call_id,note,created_at) VALUES (?,?,?)", (call_id, note, "n/a"))

    def log_rows(self, rows):
        import json
        with self.lock, self.db:
            self.db.executemany("INSERT INTO call_logs(call_id,ts,kind,event,data) VALUES (?,?,?,?,?)",
                                [(r.get("call_id"), r.get("ts"), r.get("kind"), r.get("event"), json.dumps(r)) for r in rows])


class SupabaseStore:
    """Same interface over Supabase REST. NOT RUN against a live project yet.

    Needs scripts/supabase_schema.sql applied first. Uses the service-role key, which must stay
    server-side only (Railway variable), never in the voice worker's logs or the repo.
    """

    def __init__(self, url, service_key, timeout=4.0):
        import httpx
        self.base = url.rstrip("/") + "/rest/v1"
        self.h = {"apikey": service_key, "Authorization": f"Bearer {service_key}", "Content-Type": "application/json"}
        self.http = httpx.Client(timeout=timeout)

    def _get(self, table, params):
        r = self.http.get(f"{self.base}/{table}", headers=self.h, params=params)
        r.raise_for_status()
        return r.json()

    def identify(self, first_name, dob):
        rows = self._get("cva_patients", {"dob": f"eq.{dob}", "select": "*"})
        return pick_matches(rows, first_name)

    def get_by_key(self, key):
        rows = self._get("cva_appointments", {"idempotency_key": f"eq.{key}", "select": "*"})
        return rows[0] if rows else None

    def find_slots(self, provider_ids=None, after=None, limit=3):
        params = {"taken_by": "is.null", "select": "slot_id,provider_id,start", "order": "start.asc", "limit": str(limit)}
        if provider_ids:
            params["provider_id"] = f"in.({','.join(provider_ids)})"
        if after:
            params["start"] = f"gte.{after}"
        return self._get("cva_slots", params)

    def book_atomic(self, call_id, patient_id, slot_id, key):
        r = self.http.post(f"{self.base}/rpc/cva_book", headers=self.h, json={
            "p_call_id": call_id, "p_patient_id": patient_id, "p_slot_id": slot_id, "p_key": key})
        r.raise_for_status()
        out = r.json()
        return out.get("record"), out["outcome"]

    def log_transfer(self, call_id, reason):
        self.http.post(f"{self.base}/cva_transfers", headers=self.h, json={"call_id": call_id, "reason": reason}).raise_for_status()

    def log_callback(self, call_id, note):
        self.http.post(f"{self.base}/cva_callbacks", headers=self.h, json={"call_id": call_id, "note": note}).raise_for_status()

    def log_rows(self, rows):
        body = [{"call_id": r.get("call_id"), "ts": r.get("ts"), "kind": r.get("kind"), "event": r.get("event"), "data": r} for r in rows]
        self.http.post(f"{self.base}/cva_call_logs", headers={**self.h, "Prefer": "return=minimal"}, json=body).raise_for_status()
