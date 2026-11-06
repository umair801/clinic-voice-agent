"""Load the SAME synthetic patients and slots into Supabase. Run by Umair after applying supabase_schema.sql.
Usage: python scripts/seed_supabase.py   (reads SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY from .env)"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import httpx
from dotenv import load_dotenv
from ava.backend.store import SQLiteStore

load_dotenv()
url, key = os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1", os.environ["SUPABASE_SERVICE_ROLE_KEY"]
h = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json", "Prefer": "resolution=ignore-duplicates"}
db = SQLiteStore().db
pts = [{"id": r["id"], "first_name": r["first_name"], "last_name": r["last_name"], "dob": r["dob"]} for r in db.execute("select * from patients")]
slots = [{"slot_id": r["slot_id"], "provider_id": r["provider_id"], "start": r["start"]} for r in db.execute("select * from slots")]
def post(table, rows):
    for i in range(0, len(rows), 500):
        r = httpx.post(f"{url}/{table}", headers=h, json=rows[i:i + 500], timeout=30)
        r.raise_for_status()
    print(table, len(rows), "rows sent")
post("cva_patients", pts)
post("cva_slots", slots)
