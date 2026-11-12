"""LiveKit voice worker. Thin wrapper: all rules live in ava.core (shared with the offline evals).

Start (after .env is filled):  python -m ava.voice.agent start        (production)
                               python -m ava.voice.agent dev          (local console/dev)

STATUS: imports and constructs in the sandbox with dummy keys. NOT yet run on a live call.
Every latency and cost number must come from real test calls (see docs/RUNBOOK.md).
"""
import asyncio
import json
import logging
import os
import re
import time
import uuid

from dotenv import load_dotenv
from livekit import api
from livekit.agents import (Agent, AgentServer, AgentSession, BackgroundAudioPlayer, AudioConfig, BuiltinAudioClip,
                            JobContext, RunContext, cli, function_tool, llm)
from livekit.plugins import anthropic, deepgram, elevenlabs, silero
from livekit.plugins import turn_detector  # noqa: F401  imported here so `download-files` also fetches its model

from ava import guards
from ava.backend.client import HttpTransport
from ava.core import AvaCore
from ava.output_guard import GOODBYE_WORDS, SentenceGuard, violations
from ava.release import load_release, pick_version, ROOT
from ava.state import Step
from ava.telemetry import CallLogger, TurnRecord

load_dotenv()
log = logging.getLogger("ava")
server = AgentServer()

GREETING = {"en": "Thank you for calling. This is Ava. How can I help you today?",
            "es": "Gracias por llamar. Soy Ava. ¿En qué le puedo ayudar?"}
FILLER = {"en": "One moment while I look that up.", "es": "Un momento mientras lo busco."}
# Spoken the moment a reply turns to a tool call and nothing has been said yet. The wait on live calls is the model
# (2 to 3 model calls per reply), not the tool, so a timer inside the tool never fires.
LOOKUP_FILLER = {"en": "One moment while I look that up.", "es": "Un momento mientras lo busco."}
BOOK_FILLER = {"en": "One moment while I book that.", "es": "Un momento mientras lo reservo."}
SHORT_FILLER = {"en": "One moment.", "es": "Un momento."}
FILLER_BY_TOOL = {"identify_patient": LOOKUP_FILLER, "find_slots": LOOKUP_FILLER, "find_provider": LOOKUP_FILLER,
                  "book_appointment": BOOK_FILLER, "set_party_size": SHORT_FILLER, "choose_slot": SHORT_FILLER,
                  "decline_patient": SHORT_FILLER}   # read_back_summary, end_call, request_human_transfer: no filler
SILENCE = {"en": "Are you still there?", "es": "¿Sigue ahí?"}
GOODBYE_LINE = {"en": "Thank you for calling. Goodbye.", "es": "Gracias por llamar. Adiós."}


def load_config(call_id):
    pointer_version = os.getenv("AGENT_RELEASE", "current")
    if pointer_version == "current":
        v = pick_version(call_id)
        if v is None:   # no release published yet: fall back to the working copy, and say so in the log
            cfg = json.load(open(os.path.join(ROOT, "config", "agent.json")))
            return cfg, open(os.path.join(ROOT, cfg["prompt_file"])).read(), {"version": cfg["version"] + "-unpublished", "hash": "none"}
    else:
        v = pointer_version
    return load_release(v)


def thinking_arg(model: str, mode: str):
    """The `thinking` request field that turns hidden thinking off for this model, or None to send nothing.

    Found on real phone calls: the Sonnet 5.5 API answers 400 to {"type": "disabled"} and asks for
    {"type": "between_tools"}, so every large-model turn silently fell back to the small model.
    Haiku 5.5 accepts "disabled". Checked against the live API for both models."""
    if mode != "disabled":
        return None
    if str(model).startswith("claude-sonnet-5"):
        return {"type": "between_tools"}
    return {"type": "disabled"}


class VoiceLLM(anthropic.LLM):
    """Anthropic LLM with adaptive thinking switched off per request.

    Measured on test calls: with thinking on (the Haiku 5.5 default) a one-line spoken reply came back with 190 to 220
    completion tokens, so most tokens were hidden thinking. That adds first-token delay and cost on every turn."""
    thinking_mode = "disabled"

    def chat(self, *args, extra_kwargs=None, **kwargs):
        kw = dict(extra_kwargs) if isinstance(extra_kwargs, dict) else {}
        arg = thinking_arg(self.model, self.thinking_mode)
        if arg is not None:
            kw.setdefault("thinking", arg)
        return super().chat(*args, extra_kwargs=kw, **kwargs)


def build_llms(cfg):
    key = os.getenv("ANTHROPIC_API_KEY", "")
    VoiceLLM.thinking_mode = (cfg.get("llm") or {}).get("thinking", "disabled")
    small = VoiceLLM(model=cfg["models"]["small"], api_key=key, caching="ephemeral")
    large = VoiceLLM(model=cfg["models"]["large"], api_key=key, caching="ephemeral")
    # Fallback keeps the call alive on a model error; the step instruction is rebuilt from external state, so nothing is skipped.
    return {"small": llm.FallbackAdapter([small, large], attempt_timeout=5.0),
            "large": llm.FallbackAdapter([large, small], attempt_timeout=8.0)}


class AvaAgent(Agent):
    def __init__(self, core: AvaCore, prompt: str, llms: dict, cfg: dict, manifest: dict, ctx: JobContext):
        super().__init__(instructions=prompt + "\n\n" + core.state.instruction(), llm=llms["small"])
        self.core, self.base_prompt, self.llms, self.cfg, self.manifest, self.ctx = core, prompt, llms, cfg, manifest, ctx
        self.cur_tier, self.cur_reason = "small", "routine turn"
        self.turn_started = 0.0
        self.last_tools = []
        self._acc = {"p": 0, "c": 0, "k": 0, "n": 0, "dur": 0.0, "tool_ms": 0.0}
        self._part, self._part_turn = 0, -1
        self.end_reason = ""
        self._reset_reply()

    def _reset_reply(self):
        self._reply_spoke = False       # any text (model or filler) already heading to the caller in this reply
        self._reply_text = ""           # the model's own words in this reply (not the filler)
        self._filler_played = False

    # ---- runs after the caller finishes a turn, BEFORE the model replies ----
    async def on_user_turn_completed(self, turn_ctx, new_message):
        text = new_message.text_content or ""
        self.turn_started = time.time()
        self._reset_reply()
        d = self.core.on_user_turn(text)
        self.cur_tier, self.cur_reason = d["tier"], d["route_reason"]
        await self.update_instructions(self.base_prompt + "\n\n" + d["instruction"])
        self.update_options(llm=self.llms[d["tier"]])
        if d["language_switched"]:
            self.session.tts.update_options(voice_id=os.getenv("ELEVENLABS_VOICE_ID_ES" if d["language"] == "es" else "ELEVENLABS_VOICE_ID_EN"),
                                            language=d["language"])

    # ---- output guard: a false "booked" claim is replaced BEFORE the caller hears it ----
    async def llm_node(self, chat_ctx, tools, model_settings):
        guard = SentenceGuard(self.core.state, on_block=lambda v, s: self.core.log.event(
            "claim_blocked", level="warn", detail=v, text=s[:200]))
        async for chunk in Agent.default.llm_node(self, chat_ctx, tools, model_settings):
            delta = getattr(chunk, "delta", None)
            if isinstance(chunk, str):
                out = guard.feed(chunk)
                if out:
                    self._reply_spoke, self._reply_text = True, self._reply_text + out
                    yield out
            elif delta is not None and getattr(delta, "tool_calls", None):
                tail = guard.flush()
                if tail:
                    self._reply_spoke, self._reply_text = True, self._reply_text + tail
                    yield tail
                filler = self._pick_filler(delta.tool_calls)
                if filler:
                    self._reply_spoke = self._filler_played = True
                    self.core.log.event("filler_spoken", text=filler.strip())
                    yield filler
                yield chunk
            elif delta is not None and getattr(delta, "content", None):
                out = guard.feed(delta.content)
                if out:
                    self._reply_spoke, self._reply_text = True, self._reply_text + out
                    yield out
            else:
                yield chunk
        tail = guard.flush()
        if tail:
            self._reply_spoke, self._reply_text = True, self._reply_text + tail
            yield tail

    def _pick_filler(self, tool_calls):
        """Filler line for the first tool call of a reply, only when the model has said nothing yet."""
        if self._reply_spoke:
            return ""
        names = [getattr(tc, "name", "") for tc in tool_calls]
        if any(n in ("end_call", "request_human_transfer") for n in names):
            return ""
        for n in names:                                  # prefer the most informative line
            if n in FILLER_BY_TOOL and FILLER_BY_TOOL[n] is not SHORT_FILLER:
                return FILLER_BY_TOOL[n][self.core.state.language] + " "
        for n in names:
            if n in FILLER_BY_TOOL:
                return FILLER_BY_TOOL[n][self.core.state.language] + " "
        return ""

    def _logged_filler(self, text, tool):
        """Filler source that writes a filler_spoken event each time the line is actually scheduled to play."""
        def source(step):
            self.core.log.event("filler_spoken", text=text, tool=tool, step=step)
            return text
        return source

    def _record(self, name):
        self.last_tools.append(name)

    # ---- tools ------------------------------------------------------------
    @function_tool
    async def set_party_size(self, context: RunContext, number_of_people: int):
        """Record how many people need an appointment on this call (the caller included)."""
        self._record("set_party_size")
        return await self.core.set_party_size(number_of_people)

    @function_tool
    async def identify_patient(self, context: RunContext, patient_key: str, first_name: str, date_of_birth: str):
        """Look up a patient. patient_key is 'self' for the caller, then 'child1', 'child2', or 'person2'. date_of_birth is YYYY-MM-DD."""
        self._record("identify_patient")
        lang = self.core.state.language
        async with context.with_filler(self._logged_filler(FILLER[lang], "identify_patient"), delay=1.0, interval=4.0, max_steps=3):
            return await self.core.identify_patient(patient_key, first_name, date_of_birth)

    @function_tool
    async def decline_patient(self, context: RunContext, patient_key: str):
        """The caller says this person does not need an appointment after all."""
        self._record("decline_patient")
        return await self.core.decline_patient(patient_key)

    @function_tool
    async def find_slots(self, context: RunContext, specialty_or_reason: str = ""):
        """Find the next open appointment slots. Give the specialty or reason, for example 'pediatrics'."""
        self._record("find_slots")
        return await self.core.find_slots(specialty_or_reason, 3)

    @function_tool
    async def choose_slot(self, context: RunContext, patient_key: str, slot_id: str):
        """Record the slot the caller chose for one person."""
        self._record("choose_slot")
        return await self.core.choose_slot(patient_key, slot_id)

    @function_tool
    async def find_provider(self, context: RunContext, query: str, city: str = ""):
        """Look up providers, locations, hours, or whether they accept new patients."""
        self._record("find_provider")
        return await self.core.find_provider(query, city)

    @function_tool
    async def book_appointment(self, context: RunContext, patient_key: str):
        """Book the chosen slot for ONE person. Say 'booked' only if the returned status is 'booked'."""
        self._record("book_appointment")
        lang = self.core.state.language
        async with context.with_filler(FILLER[lang], delay=1.0, interval=4.0, max_steps=3):
            return await self.core.book_appointment(patient_key)

    @function_tool
    async def read_back_summary(self, context: RunContext):
        """Get the confirmed summary of every person's booking to read back to the caller."""
        self._record("read_back_summary")
        return await self.core.read_back_summary()

    @function_tool
    async def request_human_transfer(self, context: RunContext, reason: str):
        """Transfer the caller to a person at the clinic."""
        self._record("request_human_transfer")
        res = await self.core.request_human_transfer(reason)
        if not res.get("ok"):
            return {"ok": False, "say": res["reason"]}
        number = os.getenv("HUMAN_TRANSFER_NUMBER", "")
        if any(str(x.identity).startswith("web-caller") for x in self.ctx.room.remote_participants.values()):
            # Browser test line: there is no phone leg to transfer, so take a callback instead.
            self.core.transfer_failed()
            res = await self.core.leave_callback(f"web test line, no phone transfer: {reason}")
            return {"ok": False, "say": callback_say(res)}
        if not number:
            self.core.transfer_failed()
            res = await self.core.leave_callback(f"transfer requested, no transfer number configured: {reason}")
            return {"ok": False, "say": callback_say(res)}
        try:
            await context.session.say("One moment, I am connecting you to a person.")
            identity = next(iter(self.ctx.room.remote_participants.values())).identity
            await self.ctx.api.sip.transfer_sip_participant(api.TransferSIPParticipantRequest(
                participant_identity=identity, room_name=self.ctx.room.name, transfer_to=f"tel:{number}", play_dialtone=False))
            return {"ok": True}
        except Exception as e:
            self.core.log.event("transfer_failed", level="error", error=type(e).__name__)
            self.core.transfer_failed()
            res = await self.core.leave_callback(f"transfer failed: {reason}")
            return {"ok": False, "say": "The transfer did not work. " + callback_say(res)}

    @function_tool
    async def end_call(self, context: RunContext):
        """End the call. Only allowed after the caller said goodbye and nothing is pending."""
        self._record("end_call")
        self.core.agent_said_goodbye()
        d = self.core.end_call_check()
        if not d.allowed:
            self.core.state.agent_goodbye_said = False
            return {"ended": False, "reason": d.reason, "say": "Do not end the call. Continue helping."}
        self.end_reason = "agent_ended_call_after_goodbye"
        if GOODBYE_WORDS.search(self._reply_text):
            await context.wait_for_playout()          # let the model's own goodbye finish; do not say a second one
        else:
            await context.session.say(GOODBYE_LINE[self.core.state.language], allow_interruptions=False)
        await self.ctx.api.room.delete_room(api.DeleteRoomRequest(room=self.ctx.room.name))
        return {"ended": True}


def build_session(cfg):
    t = cfg["turn"]
    from livekit.plugins.turn_detector.multilingual import MultilingualModel
    return AgentSession(
        stt=deepgram.STT(api_key=os.getenv("DEEPGRAM_API_KEY", ""), model=cfg["stt"]["model"], language=cfg["stt"]["language"]),
        tts=elevenlabs.TTS(api_key=os.getenv("ELEVENLABS_API_KEY", ""), voice_id=os.getenv("ELEVENLABS_VOICE_ID_EN", ""), model=cfg["tts"]["model"], language="en"),
        vad=silero.VAD.load(),
        turn_handling={
            "turn_detection": MultilingualModel(),
            "endpointing": {"mode": "dynamic", "min_delay": t["endpointing_min_delay"], "max_delay": t["endpointing_max_delay"]},
            "interruption": {"enabled": True, "mode": t["interruption_mode"], "min_words": t["min_interruption_words"],
                             "resume_false_interruption": True, "false_interruption_timeout": 1.5},
            "preemptive_generation": {"enabled": t["preemptive_generation"], "preemptive_tts": False},
        },
        user_away_timeout=float(cfg["silence"]["reprompt_after_s"]),
    )


def callback_say(res):
    """Instruction for the model after a callback was (or was not) saved. Never promise what was not saved."""
    if not res.get("ok"):
        return "Say you could not save a callback request just now, and suggest calling the clinic again."
    ref = res.get("ref")
    if not ref:
        return "Say a person will call them back soon."
    return ("Say a person will call them back soon. Their callback reference is " + " ".join(ref)
            + ". Read the reference slowly, one character at a time, once.")


@server.rtc_session(agent_name="clinic-ava")
async def entrypoint(ctx: JobContext):
    await ctx.connect()
    call_id = f"call_{uuid.uuid4().hex[:10]}"
    cfg, prompt, manifest = load_config(call_id)
    logger = CallLogger(call_id)
    logger.event("call_start", release=manifest["version"], release_hash=manifest["hash"])
    core = AvaCore(call_id, HttpTransport(), logger=logger)
    llms = build_llms(cfg)
    for _tier, _adapter in llms.items():   # a model that errors is skipped silently by the adapter, so log it
        _adapter.on("llm_availability_changed", lambda ev, _t=_tier: logger.event(
            "llm_unavailable" if not ev.available else "llm_recovered", level="warn" if not ev.available else "info",
            tier=_t, model=getattr(ev.llm, "model", "?")))
    session = build_session(cfg)
    agent = AvaAgent(core, prompt, llms, cfg, manifest, ctx)

    async def _call_end(reason: str = ""):  # LiveKit awaits shutdown callbacks, so this must be async
        logger.event("call_end", turns=core.state.turn_count, booked=len(core.state.booked()),
                     reason=agent.end_reason or reason or "unknown")

    ctx.add_shutdown_callback(_call_end)

    @ctx.room.on("participant_disconnected")
    def _on_left(p):
        agent.end_reason = agent.end_reason or "caller_disconnected"
        attrs = {k: str(v)[:80] for k, v in dict(getattr(p, "attributes", None) or {}).items() if "phone" not in k.lower() and "number" not in k.lower()}
        logger.event("participant_left", identity=str(p.identity), reason=str(getattr(p, "disconnect_reason", "")),
                     agent_state=str(getattr(session, "agent_state", "")), user_state=str(getattr(session, "user_state", "")),
                     sip=attrs)

    @ctx.room.on("participant_attributes_changed")
    def _on_attrs(changed, p):
        # SIP call status changes (for example active to hangup) show which leg ended the call.
        keep = {k: str(v)[:80] for k, v in dict(changed).items() if "phone" not in k.lower() and "number" not in k.lower()}
        if keep:
            logger.event("sip_attributes_changed", identity=str(p.identity), changed=keep)

    @ctx.room.on("disconnected")
    def _on_room_disc(reason=None):
        logger.event("room_disconnected", reason=str(reason))

    @session.on("conversation_item_added")
    def _on_item(ev):
        item = ev.item
        if getattr(item, "role", "") == "user":
            agent._user_metrics = dict(getattr(item, "metrics", None) or {})
            return
        if getattr(item, "role", "") != "assistant":
            return
        text = item.text_content or ""
        acc = getattr(agent, "_acc", None) or {"p": 0, "c": 0, "k": 0, "n": 0, "dur": 0.0, "tool_ms": 0.0}
        agent._part = agent._part + 1 if agent._part_turn == core.state.turn_count else 1
        agent._part_turn = core.state.turn_count
        um, am = getattr(agent, "_user_metrics", {}), dict(getattr(item, "metrics", None) or {})
        for v in violations(core.state, text):
            logger.event("false_booking_claim", level="error", detail=v, text=text[:200])
        # LiveKit's own end-to-end latency: caller stops speaking -> agent audio starts.
        # (This event fires AFTER the reply finishes playing, so wall-clock here would include speaking time.)
        lat = float((getattr(item, "metrics", None) or {}).get("e2e_latency") or 0.0)
        logger.turn(TurnRecord(call_id=call_id, turn=core.state.turn_count, tier=agent.cur_tier,
                               model=cfg["models"][agent.cur_tier], route_reason=agent.cur_reason,
                               prompt_version=manifest["version"], step=core.state.step.value, language=core.state.language,
                               response_latency_s=round(lat, 3), tools=list(agent.last_tools),
                               stt_delay_s=round(float(um.get("transcription_delay") or 0.0), 3),
                               eou_delay_s=round(float(um.get("end_of_turn_delay") or 0.0), 3),
                               hook_s=round(float(um.get("on_user_turn_completed_delay") or 0.0), 3),
                               llm_ttft_s=round(float(am.get("llm_node_ttft") or 0.0), 3),
                               tts_ttfb_s=round(float(am.get("tts_node_ttfb") or 0.0), 3),
                               filler_played=bool(getattr(agent, "_filler_played", False)),
                               prompt_tokens=int(acc["p"]), completion_tokens=int(acc["c"]), cached_tokens=int(acc["k"]),
                               tool_ms=round(float(core.tool_ms), 1), llm_calls=int(acc["n"]), llm_total_s=round(float(acc["dur"]), 3), part=agent._part))
        agent.last_tools = []
        core.tool_ms = 0.0
        agent._acc = {"p": 0, "c": 0, "k": 0, "n": 0, "dur": 0.0, "tool_ms": 0.0}

    @session.on("close")
    def _on_close(ev):
        logger.event("session_close", level="warn" if ev.error else "info", reason=str(ev.reason), error=str(ev.error)[:200] if ev.error else "")

    @session.on("metrics_collected")
    def _on_metrics(ev):
        m = ev.metrics
        if getattr(m, "type", "") == "llm_metrics":
            # A reply with tool calls makes 2+ model calls. Add them all up so cost and timing are not undercounted.
            a = agent._acc
            a["p"] += m.prompt_tokens
            a["c"] += m.completion_tokens
            a["k"] += getattr(m, "prompt_cached_tokens", 0) or 0
            a["n"] += 1
            a["dur"] += float(getattr(m, "duration", 0.0) or 0.0)

    silence = {"task": None}

    async def _silence_loop():
        """Runs while the caller is silent: reprompt at 8s and 16s, then close at 44s (callback if work is pending)."""
        start = time.time()
        base = float(cfg["silence"]["reprompt_after_s"])      # silence already elapsed when 'away' fired
        while True:
            act = guards.silence_step(core.state, base + (time.time() - start))
            lang = core.state.language
            if act == "reprompt":
                core.state.silence_prompts_sent += 1
                logger.event("silence_reprompt", n=core.state.silence_prompts_sent)
                session.say(SILENCE[lang])
            elif act in ("handoff", "end"):
                if act == "handoff":
                    await core.leave_callback("caller went silent with work pending")
                agent.end_reason = "silence_" + act
                logger.event("silence_close", action=act, level="warn" if act == "handoff" else "info")
                try:
                    await session.say(GOODBYE_LINE[lang], allow_interruptions=False)
                finally:
                    await ctx.api.room.delete_room(api.DeleteRoomRequest(room=ctx.room.name))
                return
            await asyncio.sleep(1.0)

    @session.on("user_state_changed")
    def _on_user_state(ev):
        t = silence["task"]
        if ev.new_state != "away":
            if t is not None:
                t.cancel()
                silence["task"] = None
            return
        if t is None or t.done():
            silence["task"] = asyncio.create_task(_silence_loop())

    await session.start(agent=agent, room=ctx.room)
    bg = BackgroundAudioPlayer(thinking_sound=[AudioConfig(BuiltinAudioClip.KEYBOARD_TYPING, volume=0.5)])
    await bg.start(room=ctx.room, agent_session=session)
    core.mark_greeted()
    await session.say(GREETING[core.state.language], allow_interruptions=True)


if __name__ == "__main__":
    cli.run_app(server)
