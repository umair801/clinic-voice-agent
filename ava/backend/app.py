"""Backend tool API (FastAPI). The voice worker calls these over HTTP.

Run locally:  python -c "import uvicorn; uvicorn.run('ava.backend.app:app', port=8080)"
Auth: bearer token from BACKEND_API_KEY (required when set; set it on Railway).
"""
import os

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from ava.backend.booking import BackendTimeout, BookingService, Fault
from ava.backend.store import SQLiteStore, SupabaseStore


def build_store():
    url, key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if os.getenv("STORE", "sqlite") == "supabase" and url and key:
        return SupabaseStore(url, key)
    return SQLiteStore(os.getenv("SQLITE_PATH", ":memory:"))


class IdentifyReq(BaseModel):
    first_name: str
    dob: str


class BookReq(BaseModel):
    call_id: str
    patient_id: str
    slot_id: str
    idempotency_key: str


class TransferReq(BaseModel):
    call_id: str
    reason: str


class CallbackReq(BaseModel):
    call_id: str
    note: str


class CallLogReq(BaseModel):
    rows: list[dict]


def create_app(store=None, fault=None):
    app = FastAPI(title="Clinic backend tools", docs_url=None, redoc_url=None, openapi_url=None)  # no public /docs
    fault = fault or Fault.from_env()
    if fault.active():
        print(f"WARNING: FAULT INJECTION ACTIVE {fault}. For test calls only, never in production.")
    svc = BookingService(store or build_store(), fault)
    app.state.svc = svc

    def auth(authorization: str = Header(default="")):
        expected = os.getenv("BACKEND_API_KEY", "")
        if expected and authorization != f"Bearer {expected}":
            raise HTTPException(401, "unauthorized")

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "store": type(svc.store).__name__}   # lets us verify the durable store is active, no secrets

    @app.post("/v1/identify_patient", dependencies=[Depends(auth)])
    def identify(req: IdentifyReq):
        return svc.identify(req.first_name, req.dob)

    @app.post("/v1/book_appointment", dependencies=[Depends(auth)])
    def book(req: BookReq):
        try:
            r = svc.book(req.call_id, req.patient_id, req.slot_id, req.idempotency_key)
        except BackendTimeout:
            raise HTTPException(504, "timeout")
        return r.__dict__

    @app.get("/v1/appointments/by-key/{key}", dependencies=[Depends(auth)])
    def by_key(key: str):
        return svc.lookup(key).__dict__

    @app.get("/v1/slots", dependencies=[Depends(auth)])
    def slots(provider_ids: str = "", after: str = "", limit: int = 3):
        ids = [x for x in provider_ids.split(",") if x]
        return {"slots": svc.store.find_slots(ids or None, after or None, min(limit, 10))}

    @app.post("/v1/transfer", dependencies=[Depends(auth)])
    def transfer(req: TransferReq):
        svc.store.log_transfer(req.call_id, req.reason)
        return {"logged": True}

    @app.post("/v1/calllog", dependencies=[Depends(auth)])
    def calllog(req: CallLogReq):
        """Durable call logs from the voice worker. Content is synthetic test data only. Never fails the caller path."""
        rows = req.rows[:200]
        try:
            svc.store.log_rows(rows)
        except Exception:
            raise HTTPException(503, "log store unavailable")
        return {"stored": len(rows)}

    @app.post("/v1/callback", dependencies=[Depends(auth)])
    def callback(req: CallbackReq):
        svc.store.log_callback(req.call_id, req.note)
        return {"logged": True}

    # Public demo page: shows eval and baseline results only (synthetic data, no call content).
    web = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "web")
    results = os.path.join(os.path.dirname(web), "results")

    @app.get("/api/results/{name}")
    def public_results(name: str):
        if name not in ("evals", "baseline", "cost_model", "live_calls"):
            raise HTTPException(404, "not found")
        path = os.path.join(results, f"{name}.json")
        if not os.path.exists(path):
            return {"available": False}
        import json
        return json.load(open(path))

    from fastapi.responses import FileResponse

    @app.get("/")
    def home():
        return FileResponse(os.path.join(web, "index.html"))

    return app


app = create_app()
