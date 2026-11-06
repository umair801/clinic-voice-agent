"""TEXT-MODE run with the REAL language models (no audio). Run by Umair with ANTHROPIC_API_KEY in .env.
STATUS: written, NOT YET RUN. It measures what the offline evals cannot: real model behavior and real token cost per turn.
It does NOT measure voice latency (STT, TTS, network). Those come from live phone calls.

  python -c "from evals.live_eval import main; main()"
Writes results/live_text_eval.json. Label any number from it 'text-mode run with real models, synthetic caller script'.
"""
import asyncio, json, os, tempfile, time
from dotenv import load_dotenv
import anthropic
from ava.backend.booking import BookingService, Fault
from ava.backend.client import InProcessTransport
from ava.backend.store import SQLiteStore
from ava.core import AvaCore
from ava.output_guard import violations
from ava.telemetry import CallLogger, llm_cost_usd

load_dotenv()
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CALLER = ["Hi, I need appointments for my kids.", "My name is Maria, born March 14 1982.",
          "Three of us: me, Lucas born June 2 2013, and Sofia born September 21 2016.", "Pediatrics please, earliest is fine.",
          "Yes, take the first ones.", "No, that's all. Goodbye."]
TOOLS = [
    {"name": "set_party_size", "description": "Record how many people need appointments.", "input_schema": {"type": "object", "properties": {"number_of_people": {"type": "integer"}}, "required": ["number_of_people"]}},
    {"name": "identify_patient", "description": "Look up a patient. key: self, child1, child2. dob YYYY-MM-DD.", "input_schema": {"type": "object", "properties": {"patient_key": {"type": "string"}, "first_name": {"type": "string"}, "date_of_birth": {"type": "string"}}, "required": ["patient_key", "first_name", "date_of_birth"]}},
    {"name": "find_slots", "description": "Next open slots for a specialty.", "input_schema": {"type": "object", "properties": {"specialty_or_reason": {"type": "string"}}}},
    {"name": "choose_slot", "description": "Record a chosen slot for one person.", "input_schema": {"type": "object", "properties": {"patient_key": {"type": "string"}, "slot_id": {"type": "string"}}, "required": ["patient_key", "slot_id"]}},
    {"name": "book_appointment", "description": "Book the chosen slot for ONE person.", "input_schema": {"type": "object", "properties": {"patient_key": {"type": "string"}}, "required": ["patient_key"]}},
    {"name": "read_back_summary", "description": "Confirmed summary to read back.", "input_schema": {"type": "object", "properties": {}}},
]


async def call_tool(core, name, a):
    if name == "set_party_size": return await core.set_party_size(a["number_of_people"])
    if name == "identify_patient": return await core.identify_patient(a["patient_key"], a["first_name"], a["date_of_birth"])
    if name == "find_slots": return await core.find_slots(a.get("specialty_or_reason", ""), 3)
    if name == "choose_slot": return await core.choose_slot(a["patient_key"], a["slot_id"])
    if name == "book_appointment": return await core.book_appointment(a["patient_key"])
    if name == "read_back_summary": return await core.read_back_summary()
    return {"error": "unknown tool"}


async def run(fault=None):
    cfg = json.load(open(os.path.join(ROOT, "config", "agent.json")))
    prompt = open(os.path.join(ROOT, cfg["prompt_file"])).read()
    st = SQLiteStore()
    core = AvaCore("live_eval", InProcessTransport(BookingService(st, fault or Fault())), logger=CallLogger("live_eval", log_dir=tempfile.mkdtemp(), alert_webhook=""))
    core.mark_greeted()
    client = anthropic.AsyncAnthropic()
    msgs, turns, total_cost, claims = [], [], 0.0, []
    for text in CALLER:
        d = core.on_user_turn(text)
        model = cfg["models"][d["tier"]]
        msgs.append({"role": "user", "content": text})
        for _ in range(6):
            t0 = time.time()
            r = await client.messages.create(model=model, max_tokens=300, system=prompt + "\n\n" + core.state.instruction(), tools=TOOLS, messages=msgs)
            cost = llm_cost_usd(d["tier"], r.usage.input_tokens, r.usage.output_tokens, getattr(r.usage, "cache_read_input_tokens", 0) or 0)
            total_cost += cost
            turns.append({"tier": d["tier"], "model": model, "in": r.usage.input_tokens, "out": r.usage.output_tokens, "s": round(time.time() - t0, 2), "usd": cost})
            msgs.append({"role": "assistant", "content": r.content})
            uses = [b for b in r.content if b.type == "tool_use"]
            for b in r.content:
                if b.type == "text":
                    claims += violations(core.state, b.text)
            if not uses:
                break
            msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": b.id, "content": json.dumps(await call_tool(core, b.name, b.input))} for b in uses]})
    rows = st.all_appointments()
    return {"appointments": len(rows), "patients": sorted({x["patient_id"] for x in rows}), "false_booking_claims": claims,
            "llm_calls": len(turns), "total_usd": round(total_cost, 5), "calls": turns}


def main():
    out = {"label": "text-mode run with real models, synthetic caller script, NOT a phone call", "clean": asyncio.run(run()),
           "lost_response_on_first_booking": asyncio.run(run(Fault(commit_then_timeout=1)))}
    json.dump(out, open(os.path.join(ROOT, "results", "live_text_eval.json"), "w"), indent=2)
    for k in ("clean", "lost_response_on_first_booking"):
        o = out[k]; print(k, "appointments", o["appointments"], o["patients"], "false claims", o["false_booking_claims"], "usd", o["total_usd"])
