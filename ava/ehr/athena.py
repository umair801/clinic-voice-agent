"""athenahealth PREVIEW (sandbox) adapter. Synthetic data only. Not a production integration.

Verified on the preview practice, 2026-11-25 (read calls and the book/cancel calls, with the timing noted in base.py):
  token       POST oauth2/v1/token (client credentials, scope athena/service/Athenanet.MDP.*), valid 3600 s
  patients    GET  /patients  lastname, firstname, dob (MM/DD/YYYY). A match returns the patient, a wrong dob returns none.
  slots       GET  /appointments/open  departmentid, appointmenttypeid, startdate, enddate (MM/DD/YYYY)
  mine        GET  /patients/{id}/appointments  startdate, enddate, showcancelled  (path from athena docs; see sandbox note)
  read        GET  /appointments/{id}  appointmentstatus: o open, f booked, x cancelled
  book        PUT  /appointments/{id}  patientid, appointmenttypeid, departmentid   (can take over 60 s on the sandbox)
  cancel      PUT  /appointments/{id}/cancel  patientid, cancellationreason          (can take over 60 s on the sandbox)
NOT verified: what a refused or double book returns (so any 4xx on book that mentions availability is treated as
slot_taken, anything else as a refusal), and athenahealth's own reschedule call (reschedule here = book new, then cancel old).

Credentials come from the environment (.env). Nothing here logs request or response bodies, because raw patient records
hold far more than this system needs. Only the minimum fields are kept.
"""
import asyncio
import os
import time

import httpx

from ava.ehr.base import EHRAdapter, Patient, Slot, TransportTimeout


class AthenaError(Exception):
    pass


def _us(iso):          # 2026-12-01 -> 12/01/2026
    y, m, d = iso.split("-")
    return f"{m}/{d}/{y}"


def _iso(us):          # 12/01/2026 -> 2026-12-01
    m, d, y = us.split("/")
    return f"{y}-{int(m):02d}-{int(d):02d}"


STATE = {"o": "open", "f": "booked", "x": "cancelled"}


class AthenaEHR(EHRAdapter):
    def __init__(self, base_url, token_url, client_id, client_secret, *, http=None, scope="athena/service/Athenanet.MDP.*",
                 submit_timeout_s=8.0, read_timeout_s=15.0, reconcile_window_s=20.0, poll_interval_s=2.0, max_read_retries=2):
        super().__init__()
        self.base, self.token_url = base_url.rstrip("/"), token_url
        self._cid, self._secret, self.scope = client_id, client_secret, scope
        self.http = http or httpx.AsyncClient()
        self.submit_timeout_s, self.read_timeout_s = submit_timeout_s, read_timeout_s
        self.reconcile_window_s, self.poll_interval_s, self.max_read_retries = reconcile_window_s, poll_interval_s, max_read_retries
        self._token, self._token_exp = None, 0.0
        self._token_lock = asyncio.Lock()
        self.token_requests = 0            # sandbox limit is 5 new tokens per minute, so tests assert this stays low

    @classmethod
    def from_env(cls, env=None, **kw):
        e = env if env is not None else os.environ
        need = ["ATHENA_CLIENT_ID", "ATHENA_CLIENT_SECRET", "ATHENA_TOKEN_URL", "ATHENA_BASE_URL"]
        missing = [k for k in need if not (e.get(k) or "").strip()]
        if missing:
            raise AthenaError(f"missing settings: {missing}")
        return cls(e["ATHENA_BASE_URL"].strip(), e["ATHENA_TOKEN_URL"].strip(), e["ATHENA_CLIENT_ID"].strip(),
                   e["ATHENA_CLIENT_SECRET"].strip(), **kw)

    async def _auth(self):
        async with self._token_lock:
            if self._token and time.monotonic() < self._token_exp - 120:
                return {"Authorization": "Bearer " + self._token}
            self.token_requests += 1
            r = await self.http.post(self.token_url, auth=(self._cid, self._secret),
                                     data={"grant_type": "client_credentials", "scope": self.scope}, timeout=self.read_timeout_s)
            if r.status_code != 200:
                raise AthenaError(f"token request failed: HTTP {r.status_code}")
            body = r.json()
            self._token, self._token_exp = body["access_token"], time.monotonic() + float(body.get("expires_in", 3600))
            return {"Authorization": "Bearer " + self._token}

    async def _get(self, path, **params):
        """Reads are safe to retry: on 429 or 5xx or a network error, up to max_read_retries more times."""
        last = None
        for attempt in range(self.max_read_retries + 1):
            try:
                r = await self.http.get(self.base + path, headers=await self._auth(), params=params, timeout=self.read_timeout_s)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last = type(e).__name__
            else:
                if r.status_code == 200:
                    return r.json()
                if r.status_code == 401:
                    self._token = None                       # token rejected: fetch a new one on the next try
                elif r.status_code not in (429, 500, 502, 503, 504):
                    raise AthenaError(f"GET {path.split('/')[1] if '/' in path else path}: HTTP {r.status_code}")
                last = f"HTTP {r.status_code}"
                if r.status_code == 429:
                    await asyncio.sleep(min(float(r.headers.get("retry-after", 1)), 5))
            await asyncio.sleep(0.2 * (attempt + 1))
        raise AthenaError(f"read failed after retries: {last}")

    async def _put(self, path, data):
        """Writes are NEVER retried here. A timeout raises TransportTimeout and the caller reconciles by reading."""
        try:
            r = await self.http.put(self.base + path, headers=await self._auth(), data=data, timeout=self.submit_timeout_s)
        except (httpx.TimeoutException, httpx.TransportError):
            raise TransportTimeout()
        return r

    async def find_patients(self, last_name, first_name=None, dob=None):
        params = {"lastname": last_name, "limit": 20}
        if first_name:
            params["firstname"] = first_name
        if dob:
            params["dob"] = _us(dob)
        body = await self._get("/patients", **params)
        out = []
        for p in body.get("patients", []) if isinstance(body, dict) else []:
            out.append(Patient(str(p["patientid"]), p.get("firstname", ""), p.get("lastname", ""),
                               _iso(p["dob"]) if p.get("dob") else "", str(p.get("zip") or "")))   # everything else is dropped here
        return out

    async def open_slots(self, department_id, appointment_type_id, start, end, limit=5):
        body = await self._get("/appointments/open", departmentid=department_id, appointmenttypeid=appointment_type_id,
                               startdate=_us(start), enddate=_us(end), limit=limit)
        return [Slot(str(a["appointmentid"]), _iso(a["date"]), a["starttime"], str(a["providerid"]), str(a["departmentid"]),
                     str(a.get("appointmenttypeid", appointment_type_id))) for a in body.get("appointments", [])]

    async def patient_appointments(self, patient_id, start, end):
        body = await self._get(f"/patients/{patient_id}/appointments", startdate=_us(start), enddate=_us(end), showcancelled="false")
        out = []
        for a in body.get("appointments", []) if isinstance(body, dict) else []:
            if a.get("appointmentstatus") not in (None, "f"):
                continue          # only booked ones; cancelled or open are not "my appointments"
            out.append(Slot(str(a["appointmentid"]), _iso(a["date"]), a["starttime"], str(a.get("providerid", "")),
                            str(a.get("departmentid", "")), str(a.get("appointmenttypeid", ""))))
        return out

    async def read_slot(self, slot_id):
        body = await self._get(f"/appointments/{slot_id}")
        rec = body[0] if isinstance(body, list) and body else body
        if not isinstance(rec, dict) or "appointmentstatus" not in rec:
            return {"state": "unknown", "patient_id": ""}
        return {"state": STATE.get(rec["appointmentstatus"], "other"), "patient_id": str(rec.get("patientid") or "")}

    async def _send_book(self, slot, patient):
        r = await self._put(f"/appointments/{slot.slot_id}", {"patientid": patient.patient_id,
                            "appointmenttypeid": slot.appointment_type_id, "departmentid": slot.department_id})
        if r.status_code == 200:
            return "ok"
        text = r.text.lower()
        if r.status_code in (400, 409) and any(w in text for w in ("not available", "no longer", "already", "taken", "booked")):
            return "slot_taken"
        raise AthenaError(f"book refused: HTTP {r.status_code}")

    async def _send_cancel(self, appointment_id, patient_id):
        r = await self._put(f"/appointments/{appointment_id}/cancel", {"patientid": patient_id, "cancellationreason": "patient request"})
        if r.status_code == 200:
            return "ok"
        if r.status_code in (400, 404):
            return "not_found"      # seen on the sandbox: "no longer valid" for an appointment that was not booked yet
        raise AthenaError(f"cancel refused: HTTP {r.status_code}")
