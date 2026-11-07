"""Simulated caller: talks to the LIVE worker over WebRTC, no phone network involved.

It joins a LiveKit room, dispatches the clinic-ava agent, speaks scripted lines (synthetic voice from
ElevenLabs), and measures what a caller would notice: how long after you stop speaking Ava starts to talk.

    python scripts/sim_call.py --list
    python scripts/sim_call.py family
    python scripts/sim_call.py family --runs 3

Needs LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET, ELEVENLABS_API_KEY in .env (never printed).
Install once:  pip install livekit livekit-api

HONESTY: results are SIMULATED-CALLER test calls over WebRTC (synthetic caller, synthetic patients). They are not
phone calls and must be labeled that way. Latency here is measured from the caller side and includes
end-of-turn detection, the model, tools and speech synthesis, but not the phone network.
"""
import argparse
import asyncio
import hashlib
import json
import os
import statistics
import sys
import time
import urllib.request
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SR = 16000
FRAME_MS = 20
SPF = SR * FRAME_MS // 1000          # samples per frame
SILENCE = b"\x00\x00" * SPF
AGENT_NAME = "clinic-ava"
STATE_KEY = "lk.agent.state"

SCENARIOS = {
    # Family of three: the main case (failures 5, 6, 7).
    "family": [
        "Hi, my name is Maria Alvarez and my date of birth is March fourteenth, nineteen eighty two.",
        "I also need appointments for my two children. Lucas, born June second, twenty thirteen, and Sofia, born September twenty first, twenty sixteen.",
        "A general checkup for all three of us, please.",
        "The first time works for me.",
        "The first one works for Lucas too.",
        "The first one works for Sofia too.",
        "Yes, that is all correct. Thank you.",
        "No, that is everything. Goodbye.",
    ],
    # One person, short.
    "simple": [
        "Hello, my name is Maria Alvarez, born March fourteenth, nineteen eighty two.",
        "A general checkup, please.",
        "The first time works.",
        "No, that is everything. Thank you, goodbye.",
    ],
    # Failure 4: a short Spanish word and a name inside an English call must not switch the language.
    "language_si": [
        "Hello, my name is Maria Alvarez.",
        "Sí.",
        "March fourteenth, nineteen eighty two.",
        "Sofia.",
        "A general checkup, please.",
        "The first time works. Goodbye.",
    ],
    # Failure 4: a clear request for Spanish should switch.
    "spanish_request": [
        "Hello, can we speak in Spanish please?",
        "Hola, me llamo Maria Alvarez, nací el catorce de marzo de mil novecientos ochenta y dos.",
        "Un chequeo general, por favor.",
        "El primer horario está bien. Gracias, adiós.",
    ],
    # Provider question (failure 9, retrieval).
    "provider": [
        "Hi, which doctors do you have for pediatrics, and where is the clinic?",
        "Thank you, that is all. Goodbye.",
    ],
}


def load_env():
    env = dict(os.environ)
    p = os.path.join(ROOT, ".env")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return env


def synth(text, env, cache_dir):
    """Caller voice as raw 16 kHz mono PCM. Cached on disk so repeat runs cost nothing."""
    voice = env.get("SIM_VOICE_ID") or env.get("ELEVENLABS_VOICE_ID_EN", "")
    key = hashlib.sha256(f"{voice}|{text}".encode()).hexdigest()[:20]
    path = os.path.join(cache_dir, key + ".pcm")
    if os.path.exists(path):
        return open(path, "rb").read()
    body = json.dumps({"text": text, "model_id": "eleven_turbo_v2_5"}).encode()
    req = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=pcm_16000", data=body,
        headers={"xi-api-key": env["ELEVENLABS_API_KEY"], "Content-Type": "application/json"})
    pcm = urllib.request.urlopen(req, timeout=60).read()
    os.makedirs(cache_dir, exist_ok=True)
    open(path, "wb").write(pcm)
    return pcm


class Sim:
    def __init__(self, env, scenario, verbose=True):
        self.env, self.scenario, self.verbose = env, scenario, verbose
        self.t0 = time.monotonic()
        self.pending = bytearray()
        self.states = []              # (t, state)
        self.agent_state = "unknown"
        self.agent_identity = None
        self.transcripts = {}         # segment id -> text
        self.ended = None
        self.speech_frames_sent_end = None

    def log(self, msg):
        if self.verbose:
            print(f"{time.monotonic() - self.t0:7.2f}s  {msg}", flush=True)

    # ---- microphone: always sends frames (silence when idle) so the agent's VAD behaves like on a phone ----
    async def mic_loop(self, source, rtc):
        start, n = time.monotonic(), 0
        while self.ended is None:
            if self.pending:
                chunk = bytes(self.pending[:SPF * 2])
                del self.pending[:SPF * 2]
                if len(chunk) < SPF * 2:
                    chunk += b"\x00" * (SPF * 2 - len(chunk))
                if not self.pending:
                    self.speech_frames_sent_end = time.monotonic()
            else:
                chunk = SILENCE
            await source.capture_frame(rtc.AudioFrame(chunk, SR, 1, SPF))
            n += 1
            await asyncio.sleep(max(0.0, start + n * FRAME_MS / 1000 - time.monotonic()))

    async def wait_state(self, want, timeout, since=0.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end and self.ended is None:
            for t, s in self.states:
                if t >= since and s == want:
                    return t
            await asyncio.sleep(0.02)
        return None

    async def wait_calm(self, timeout, calm_s=2.0):
        """Wait until the agent has been 'listening' for calm_s straight."""
        end = time.monotonic() + timeout
        while time.monotonic() < end and self.ended is None:
            if self.agent_state == "listening" and self.states and time.monotonic() - self.states[-1][0] >= calm_s:
                return True
            await asyncio.sleep(0.05)
        return False

    async def run(self):
        from livekit import api, rtc
        url, key, secret = self.env["LIVEKIT_URL"], self.env["LIVEKIT_API_KEY"], self.env["LIVEKIT_API_SECRET"]
        room_name = f"sim-{self.scenario}-{uuid.uuid4().hex[:6]}"
        cache = os.path.join(ROOT, "logs", "sim_audio")
        lines = SCENARIOS[self.scenario]
        self.log("preparing caller audio")
        pcms = [synth(t, self.env, cache) for t in lines]

        token = (api.AccessToken(key, secret).with_identity("sim-caller").with_name("Sim Caller")
                 .with_grants(api.VideoGrants(room_join=True, room=room_name)).to_jwt())
        room = rtc.Room()

        @room.on("participant_attributes_changed")
        def _attrs(changed, participant):
            if STATE_KEY in changed and participant.identity != "sim-caller":
                self.agent_identity = participant.identity
                self.agent_state = changed[STATE_KEY]
                self.states.append((time.monotonic(), self.agent_state))
                self.log(f"agent state -> {self.agent_state}")

        @room.on("participant_disconnected")
        def _gone(participant):
            if participant.identity == self.agent_identity and self.ended is None:
                self.ended = "agent_left"
                self.log("agent left the room (call ended by Ava)")

        @room.on("disconnected")
        def _disc(*a):
            if self.ended is None:
                self.ended = "room_disconnected"
                self.log("room disconnected")

        def _on_transcript(reader, identity):
            async def go():
                try:
                    text = await reader.read_all()
                    attrs = dict(reader.info.attributes or {})
                    if identity != "sim-caller" and text.strip():
                        seg = attrs.get("lk.segment_id", str(len(self.transcripts)))
                        self.transcripts[seg] = text
                        if attrs.get("lk.transcription_final") in ("true", True):
                            self.log(f"AVA: {text}")
                except Exception:
                    pass
            asyncio.create_task(go())

        try:
            room.register_text_stream_handler("lk.transcription", _on_transcript)
        except Exception:
            pass

        await room.connect(url, token)
        source = rtc.AudioSource(SR, 1)
        track = rtc.LocalAudioTrack.create_audio_track("sim-mic", source)
        await room.local_participant.publish_track(track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
        mic = asyncio.create_task(self.mic_loop(source, rtc))
        lkapi = api.LiveKitAPI(url, key, secret)
        await lkapi.agent_dispatch.create_dispatch(api.CreateAgentDispatchRequest(agent_name=AGENT_NAME, room=room_name))
        self.log(f"room {room_name}: agent dispatched")

        turns = []
        if await self.wait_state("speaking", 30) is None:
            self.log("agent never started (is the worker registered?)")
            self.ended = self.ended or "no_agent"
        else:
            await self.wait_calm(30)                                   # greeting finished
        for i, (text, pcm) in enumerate(zip(lines, pcms)):
            if self.ended:
                break
            await asyncio.sleep(0.9)                                   # natural pause before talking
            self.log(f"CALLER {i + 1}: {text}")
            self.speech_frames_sent_end = None
            self.pending.extend(pcm)
            while self.speech_frames_sent_end is None and not self.ended:
                await asyncio.sleep(0.02)
            t_end = self.speech_frames_sent_end or time.monotonic()
            t_think = await self.wait_state("thinking", 20, since=t_end - 0.2)
            t_speak = await self.wait_state("speaking", 40, since=t_end)
            rec = {"turn": i + 1, "caller": text,
                   "reply_latency_s": round(t_speak - t_end, 3) if t_speak else None,
                   "thinking_started_s": round(t_think - t_end, 3) if t_think else None}
            await self.wait_calm(45)
            rec["reply_done_s"] = round((self.states[-1][0] if self.states else time.monotonic()) - t_end, 3)
            turns.append(rec)
            self.log(f"turn {i + 1}: reply latency {rec['reply_latency_s']}s")
        if not self.ended:
            await asyncio.sleep(3.0)
        result = {"scenario": self.scenario, "room": room_name, "kind": "simulated_caller_webrtc",
                  "ended_by": self.ended or "script_finished", "turns": turns,
                  "ava_said": list(self.transcripts.values())}
        lat = [t["reply_latency_s"] for t in turns if t["reply_latency_s"] is not None]
        result["latency_p50_s"] = round(statistics.median(lat), 3) if lat else None
        result["latency_max_s"] = max(lat) if lat else None
        result["no_reply_turns"] = sum(1 for t in turns if t["reply_latency_s"] is None)
        self.ended = self.ended or "done"
        await asyncio.sleep(0.1)
        mic.cancel()
        await room.disconnect()
        try:
            await lkapi.room.delete_room(api.DeleteRoomRequest(room=room_name))
        except Exception:
            pass
        await lkapi.aclose()
        return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", nargs="?", default="family")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    if a.list:
        for k, v in SCENARIOS.items():
            print(f"{k:16s} {len(v)} caller lines")
        return
    if a.scenario not in SCENARIOS:
        sys.exit(f"unknown scenario. choose one of: {', '.join(SCENARIOS)}")
    env = load_env()
    for k in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "ELEVENLABS_API_KEY"):
        if not env.get(k):
            sys.exit(f"{k} is missing from .env")
    outdir = os.path.join(ROOT, "logs", "sim")
    os.makedirs(outdir, exist_ok=True)
    for n in range(a.runs):
        res = asyncio.run(Sim(env, a.scenario, verbose=not a.quiet).run())
        path = os.path.join(outdir, f"{a.scenario}_{int(time.time())}.json")
        json.dump(res, open(path, "w"), indent=2)
        print(f"\nRUN {n + 1}/{a.runs}: ended_by={res['ended_by']} latency p50={res['latency_p50_s']}s max={res['latency_max_s']}s "
              f"no_reply_turns={res['no_reply_turns']}  saved {os.path.relpath(path, ROOT)}")


if __name__ == "__main__":
    main()
