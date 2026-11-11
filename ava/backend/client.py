"""Transports the agent uses to reach the booking backend.

HttpTransport     production. httpx against BACKEND_URL with the bearer token.
InProcessTransport evals and tests. Calls BookingService directly, so fault injection is exact.
Both raise BackendTimeout / TransportError the same way, so reliable_book behaves identically.
"""
import os

import httpx

from ava.backend.booking import BackendTimeout, BookResult
from ava.backend.reliable import TransportError
from ava.backend.store import new_callback_ref

IDENTIFY_TIMEOUT_S = 9.0   # one attempt; the filler line repeats every 4s while it runs


class InProcessTransport:
    def __init__(self, service):
        self.svc = service

    async def identify(self, first_name, dob):
        return self.svc.identify(first_name, dob)

    async def book(self, call_id, patient_id, slot_id, key):
        return self.svc.book(call_id, patient_id, slot_id, key)

    async def lookup(self, key):
        return self.svc.lookup(key)

    async def slots(self, provider_ids=None, after=None, limit=3):
        return self.svc.store.find_slots(provider_ids, after, limit)

    async def transfer(self, call_id, reason):
        self.svc.store.log_transfer(call_id, reason)

    async def callback(self, call_id, note):
        ref = new_callback_ref()
        self.svc.store.log_callback(call_id, f"[{ref}] {note}")
        return ref


class HttpTransport:
    def __init__(self, base_url=None, api_key=None):
        self.base = (base_url or os.getenv("BACKEND_URL", "http://localhost:8080")).rstrip("/")
        key = api_key if api_key is not None else os.getenv("BACKEND_API_KEY", "")
        self.h = {"Authorization": f"Bearer {key}"} if key else {}
        self.http = httpx.AsyncClient(base_url=self.base, headers=self.h)

    async def _call(self, method, path, timeout, **kw):
        try:
            r = await self.http.request(method, path, timeout=timeout, **kw)
        except (httpx.TimeoutException,) as e:
            raise BackendTimeout(str(e))
        except httpx.HTTPError as e:
            raise TransportError(str(e))
        if r.status_code in (502, 503, 504):
            raise BackendTimeout(f"http {r.status_code}")
        if r.status_code >= 400:
            raise TransportError(f"http {r.status_code}")
        return r.json()

    async def identify(self, first_name, dob):
        # read-only. One long attempt beats two short ones: a 7.8s lookup that would succeed must not be cut off at 4s.
        return await self._call("POST", "/v1/identify_patient", IDENTIFY_TIMEOUT_S, json={"first_name": first_name, "dob": dob})

    async def book(self, call_id, patient_id, slot_id, key):
        d = await self._call("POST", "/v1/book_appointment", 6.0, json={
            "call_id": call_id, "patient_id": patient_id, "slot_id": slot_id, "idempotency_key": key})
        return BookResult(**d)

    async def lookup(self, key):
        d = await self._call("GET", f"/v1/appointments/by-key/{key}", 3.0)
        return BookResult(**d)

    async def slots(self, provider_ids=None, after=None, limit=3):
        d = await self._call("GET", "/v1/slots", 3.0, params={"provider_ids": ",".join(provider_ids or []), "after": after or "", "limit": limit})
        return d["slots"]

    async def transfer(self, call_id, reason):
        await self._call("POST", "/v1/transfer", 3.0, json={"call_id": call_id, "reason": reason})

    async def callback(self, call_id, note):
        d = await self._call("POST", "/v1/callback", 3.0, json={"call_id": call_id, "note": note})
        return d.get("ref")
