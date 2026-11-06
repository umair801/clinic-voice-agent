"""Baseline "Ava": a deterministic simulation that reproduces the failures in the task.

This is NOT a real phone call and NOT a real model. It is a rule-based replica of the
behaviors the task describes (config excerpt plus the one call from the logs), so every
failure can be reproduced on demand and checked by a test. Timestamps marked "log" come
straight from the task PDF. Timestamps marked "harness" were added by us because the log
is silent there (see harness/scenario.py).

Each buggy mechanism below is labeled with the failure number it reproduces.
"""
import json
from dataclasses import dataclass, field

from clinic.data import FAMILY, approx_tokens, build_giant_prompt

SPANISH_WORDS = {
    "si", "sí", "hola", "gracias", "por", "favor", "mi", "nombre", "es", "correcto",
    "bueno", "buenos", "días", "dias", "hijo", "hija", "cita",
}
STEPS = ["greet", "identify", "verify_dob", "collect_patients", "choose_slot", "book", "confirm", "wrap_up"]


@dataclass
class Trace:
    events: list = field(default_factory=list)       # chronological dicts
    appointments: list = field(default_factory=list)  # what ended up in the clinic records
    language_by_time: list = field(default_factory=list)
    models_used: list = field(default_factory=list)
    steps_done: list = field(default_factory=list)
    turns: list = field(default_factory=list)         # per agent turn token accounting
    config: dict = field(default_factory=dict)
    duration_s: float = 0.0

    def add(self, **kw):
        self.events.append(kw)


class BaselineBackend:
    """Clinic records backend as described: slow identify, book times out, silent retry."""

    IDENTIFY_LATENCY_S = 7.8  # log: identify_patient returns after 7.8s

    def __init__(self):
        self.appointments = []
        self.book_attempts = 0
        self.background_retries = 0

    def identify_patient(self, first, dob):
        for key, p in FAMILY.items():
            if p["first"].lower() == first.lower() and p["dob"] == dob:
                return {"found": True, "patient_id": p["id"]}
        return {"found": False}

    def book_appointment(self, patient_id, slot, first_attempt_times_out):
        """FAILURE 5/6: server writes the row, but the response is lost (timeout).
        No idempotency key, so the background retry writes a second identical row."""
        self.book_attempts += 1
        self.appointments.append({"patient_id": patient_id, "slot": slot, "attempt": self.book_attempts})
        if first_attempt_times_out:
            return {"success": False, "error": "timeout"}
        return {"success": True}

    def background_retry(self, patient_id, slot):
        self.background_retries += 1
        self.book_attempts += 1
        self.appointments.append({"patient_id": patient_id, "slot": slot, "attempt": self.book_attempts})


def detect_language(text):
    """FAILURE 4: the rule from the config. Any non-English token flips the language."""
    words = {w.strip(".,!?¿¡").lower() for w in text.split()}
    if words & SPANISH_WORDS:
        return "es"
    if any(ord(c) > 127 for c in text):
        return "es"
    return "en"


class BaselineAva:
    def __init__(self, config_path="config/baseline_agent.json"):
        with open(config_path) as f:
            self.cfg = json.load(f)
        self.backend = BaselineBackend()
        self.prompt = build_giant_prompt(self.cfg["system_prompt_chars"])  # FAILURE 9
        self.language = "en"
        self.current_patient = None   # FAILURE 6: only ONE patient held in state
        self.trace = Trace(config=self.cfg)
        self.last_agent_audio_end = 0.0
        self.tool_pending_until = None
        self.ended = False

    # ---- helpers ---------------------------------------------------------
    def _speak(self, start, text, dur, source="final"):
        self.trace.add(t=start, kind="agent_speech", start=start, end=start + dur, text=text,
                       source=source, lang=self.language)
        self.last_agent_audio_end = max(self.last_agent_audio_end, start + dur)
        self._account_turn(start, text)

    def _account_turn(self, t, out_text, model=None):
        model = model or "large"
        # FAILURE 9/10: the whole 160K-char prompt is sent on every turn
        self.trace.turns.append({
            "t": t, "model": model,
            "input_tokens": approx_tokens(self.prompt) + approx_tokens(sum(len(e.get("text", "")) for e in self.trace.events)),
            "output_tokens": approx_tokens(out_text),
        })

    def _tool(self, name, start, latency, args, result):
        end = start + latency
        self.trace.add(t=start, kind="tool_call", name=name, start=start, end=end, args=args, result=result)
        # FAILURE 1: filler only fires if the tool runs longer than soft_timeout_s (8s). 7.8s < 8s.
        filler = latency > self.cfg["soft_timeout_s"] and self.cfg["tool_call_sound"] == "none"
        if filler:
            self.trace.add(t=start + self.cfg["soft_timeout_s"], kind="filler", text="One moment please")
        self.trace.add(t=end, kind="tool_result", name=name, filler_played=filler)
        return end

    # ---- the call --------------------------------------------------------
    def run(self, script):
        for ev in script:
            if self.ended:
                break
            self._handle(ev)
        # A hanging tool call has end=None (it never returned), so fall back to its start time.
        self.trace.duration_s = max(
            [(e["end"] if e.get("end") is not None else e.get("t", 0)) for e in self.trace.events] + [0.0])
        self.trace.appointments = list(self.backend.appointments)
        return self.trace

    def _handle(self, ev):
        t, kind = ev["t"], ev["kind"]
        if kind == "agent_greet":
            self._speak(t, ev["text"], ev["dur"])
            self.trace.steps_done.append("greet")
            self.trace.models_used.append("large")
        elif kind == "caller":
            self.trace.add(t=t, kind="caller", text=ev["text"], dur=ev.get("dur", 2.0), src=ev.get("src"))
            self._on_caller(ev)
        elif kind == "caller_partial":
            # FAILURE 3: speculative_turn starts a reply on the partial transcript...
            self.trace.add(t=t, kind="caller_partial", text=ev["text"])
            if self.cfg["speculative_turn"]:
                self._speak(ev["reply_at"], ev["reply"], ev["reply_dur"], source="speculative")
        elif kind == "caller_final":
            # ...and the final transcript starts a second reply WITHOUT cancelling the first.
            self.trace.add(t=t, kind="caller", text=ev["text"], dur=ev.get("dur", 2.0), src=ev.get("src"))
            self._speak(ev["reply_at"], ev["reply"], ev["reply_dur"], source="final")
            self.trace.steps_done.append("verify_dob")
        elif kind == "tool":
            self._run_scripted_tool(ev)
        elif kind == "agent_say":
            self._speak(t, ev["text"], ev["dur"])
        elif kind == "silence_timer_check":
            self._silence_check(ev)
        elif kind == "fallback_probe":
            self._fallback(ev)

    def _on_caller(self, ev):
        text = ev["text"]
        new_lang = detect_language(text)
        # FAILURE 4: switches on any non-English token; only ever switches TO Spanish.
        if new_lang == "es" and self.language == "en":
            self.language = "es"
            self.trace.language_by_time.append({"t": ev["t"], "lang": "es", "trigger": text})
        # No branch ever switches back, even when the caller asks for English.
        if ev.get("reply"):
            self._speak(ev["reply_at"], ev["reply"], ev["reply_dur"])

    def _run_scripted_tool(self, ev):
        name, t = ev["name"], ev["t"]
        if name == "identify_patient":
            p = FAMILY["parent"]
            res = self.backend.identify_patient(p["first"], p["dob"])
            end = self._tool(name, t, BaselineBackend.IDENTIFY_LATENCY_S, {"first": p["first"]}, res)
            self.current_patient = res.get("patient_id")
            self.trace.steps_done.append("identify")
        elif name == "book_appointment":
            who = ev["who"]
            pid = FAMILY[who]["id"]
            if ev.get("hangs"):
                # tool never returns before the silence timer fires (FAILURE 7 repro)
                self.trace.add(t=t, kind="tool_call", name=name, start=t, end=None, args={"patient_id": pid}, result=None)
                self.tool_pending_until = t + ev["hang_s"]
                return
            res = self.backend.book_appointment(pid, ev["slot"], ev["times_out"])
            end = t + ev["latency"]
            self.trace.add(t=t, kind="tool_call", name=name, start=t, end=end, args={"patient_id": pid}, result=res)
            self.trace.add(t=end, kind="tool_result", name=name, filler_played=False)
            if not res["success"]:
                # FAILURE 5: agent claims success anyway; backend retries in the background
                self._speak(end + 1.0, "You're all booked.", 1.5)
                self.backend.background_retry(pid, ev["slot"])
                self.trace.add(t=end + 1.0, kind="background_retry", name=name, patient_id=pid)
            else:
                self._speak(end + 0.5, "You're all booked.", 1.5)
            self.trace.steps_done.append("book")

    def _silence_check(self, ev):
        """FAILURE 7: silence_end_call_s ends the call even though a tool is still pending."""
        limit = self.cfg["silence_end_call_s"]
        fire_at = self.last_agent_audio_end + limit
        if self.tool_pending_until is not None and fire_at < self.tool_pending_until:
            self._speak(fire_at, "Thank you for calling. Goodbye.", 2.0)
            self.trace.add(t=fire_at + 2.0, kind="end_call", reason="silence_end_call_s",
                           pending_tool=True, caller_said_goodbye=False)
            self.ended = True

    def _fallback(self, ev):
        """FAILURE 8: workflow state lives only in the model's context. When the large model
        times out, the small fallback model takes over and (modeled per the task's description)
        skips multi-instruction steps."""
        self.trace.add(t=ev["t"], kind="model_timeout", model="large")
        self.trace.models_used.append("small")
        skipped = {"verify_dob", "collect_patients"}
        remaining = [s for s in STEPS[STEPS.index(ev["resume_after"]) + 1:] if s not in skipped]
        self.trace.add(t=ev["t"], kind="fallback_resume", model="small", steps_executed=remaining,
                       skipped=sorted(skipped & set(STEPS[STEPS.index(ev["resume_after"]) + 1:])))
        self.trace.steps_done.extend(remaining)
