"""LiveKit voice worker. Thin wrapper: all rules live in ava.core (shared with the offline evals).

Start (after .env is filled):  python -m ava.voice.agent start        (production)
                               python -m ava.voice.agent dev          (local console/dev)

STATUS: imports and constructs in the sandbox with dummy keys. NOT yet run on a live call.
Every latency and cost number must come from real test calls (see docs/RUNBOOK.md).
"""
import json
import logging
import os
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
from ava.output_guard import SentenceGuard, violations
from ava.release import load_release, pick_version, ROOT
from ava.state import Step
from ava.telemetry import CallLogger, TurnRecord

load_dotenv()
log = logging.getLogger("ava")
server = AgentServer()

GREETING = {"en": "Thank you for calling. This is Ava. How can I help you today?",
            "es": "Gracias por llamar. Soy Ava. ¿En qué le puedo ayudar?"}
FILLER = {"en": "One moment while I look that up.", "es": "Un momento mientras lo busco."}
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


def build_llms(cfg):
    key = os.getenv("ANTHROPIC_API_KEY", "")
    small = anthropic.LLM(model=cfg["models"]["small"], api_key=key, caching="ephemeral")
    large = anthropic.LLM(model=cfg["models"]["large"], api_key=key, caching="ephemeral")
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

    # ---- runs after the caller finishes a turn, BEFORE the model replies ----
    async def on_user_turn_completed(self, turn_ctx, new_message):
        text = new_message.text_content or ""
        self.turn_started = time.time()
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
                    yield out
            elif delta is not None and getattr(delta, "tool_calls", None):
                tail = guard.flush()
                if tail:
                    yield tail
                yield chunk
            elif delta is not None and getattr(delta, "content", None):
                out = guard.feed(delta.content)
                if out:
                    yield out
            else:
                yield chunk
        tail = guard.flush()
        if tail:
            yield tail

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
        async with context.with_filler(FILLER[lang], delay=1.0, interval=4.0, max_steps=2):
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
        if not number:
            self.core.transfer_failed()
            await self.core.leave_callback(f"transfer requested, no transfer number configured: {reason}")
            return {"ok": False, "say": "Say a person will call them back soon."}
        try:
            await context.session.say("One moment, I am connecting you to a person.")
            identity = next(iter(self.ctx.room.remote_participants.values())).identity
            await self.ctx.api.sip.transfer_sip_participant(api.TransferSIPParticipantRequest(
                participant_identity=identity, room_name=self.ctx.room.name, transfer_to=f"tel:{number}", play_dialtone=False))
            return {"ok": True}
        except Exception as e:
            self.core.log.event("transfer_failed", level="error", error=type(e).__name__)
            self.core.transfer_failed()
            await self.core.leave_callback(f"transfer failed: {reason}")
            return {"ok": False, "say": "The transfer did not work. Say a person will call them back soon."}

    @function_tool
    async def end_call(self, context: RunContext):
        """End the call. Only allowed after the caller said goodbye and nothing is pending."""
        self._record("end_call")
        self.core.agent_said_goodbye()
        d = self.core.end_call_check()
        if not d.allowed:
            self.core.state.agent_goodbye_said = False
            return {"ended": False, "reason": d.reason, "say": "Do not end the call. Continue helping."}
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


@server.rtc_session(agent_name="clinic-ava")
async def entrypoint(ctx: JobContext):
    await ctx.connect()
    call_id = f"call_{uuid.uuid4().hex[:10]}"
    cfg, prompt, manifest = load_config(call_id)
    logger = CallLogger(call_id)
    logger.event("call_start", release=manifest["version"], release_hash=manifest["hash"])
    core = AvaCore(call_id, HttpTransport(), logger=logger)
    llms = build_llms(cfg)
    session = build_session(cfg)
    agent = AvaAgent(core, prompt, llms, cfg, manifest, ctx)

    async def _call_end(reason: str = ""):  # LiveKit awaits shutdown callbacks, so this must be async
        logger.event("call_end", turns=core.state.turn_count, booked=len(core.state.booked()), reason=reason)

    ctx.add_shutdown_callback(_call_end)

    @ctx.room.on("participant_disconnected")
    def _on_left(p):
        logger.event("participant_left", identity=str(p.identity), reason=str(getattr(p, "disconnect_reason", "")))

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
                               prompt_tokens=int(getattr(agent, "_last_prompt_tokens", 0)),
                               completion_tokens=int(getattr(agent, "_last_completion_tokens", 0)),
                               cached_tokens=int(getattr(agent, "_last_cached_tokens", 0))))
        agent.last_tools = []

    @session.on("close")
    def _on_close(ev):
        logger.event("session_close", level="warn" if ev.error else "info", reason=str(ev.reason), error=str(ev.error)[:200] if ev.error else "")

    @session.on("metrics_collected")
    def _on_metrics(ev):
        m = ev.metrics
        if getattr(m, "type", "") == "llm_metrics":
            agent._last_prompt_tokens = m.prompt_tokens
            agent._last_completion_tokens = m.completion_tokens
            agent._last_cached_tokens = getattr(m, "prompt_cached_tokens", 0) or 0

    @session.on("user_state_changed")
    def _on_user_state(ev):
        if ev.new_state != "away":
            return
        act = guards.silence_action(core.state, float(cfg["silence"]["reprompt_after_s"]) * (core.state.silence_prompts_sent + 1))
        lang = core.state.language
        if act == "reprompt":
            core.state.silence_prompts_sent += 1
            session.say(SILENCE[lang])
        elif act in ("handoff", "end"):
            async def finish():
                if act == "handoff":
                    await core.leave_callback("caller went silent with work pending")
                logger.event("silence_close", action=act, level="warn" if act == "handoff" else "info")
                await ctx.api.room.delete_room(api.DeleteRoomRequest(room=ctx.room.name))
            import asyncio
            asyncio.create_task(finish())

    await session.start(agent=agent, room=ctx.room)
    bg = BackgroundAudioPlayer(thinking_sound=[AudioConfig(BuiltinAudioClip.KEYBOARD_TYPING, volume=0.5)])
    await bg.start(room=ctx.room, agent_session=session)
    core.mark_greeted()
    await session.say(GREETING[core.state.language], allow_interruptions=True)


if __name__ == "__main__":
    cli.run_app(server)
