"""Per-turn latency and cost telemetry, JSONL call logs, alerts, and replay summary (failure 10, Phase 5).

One JSONL file per call in logs/calls/. Every line is either a turn record or an event.
Cost comes from config/pricing.json (LLM tokens only; STT, TTS and telephony are priced separately).
"""
import json
import os
import statistics
import time
from dataclasses import dataclass, asdict, field

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRICING_PATH = os.path.join(ROOT, "config", "pricing.json")
STDOUT_LOGS = os.environ.get("CALLLOG_STDOUT") == "1" or any(
    os.environ.get(k) for k in ("RAILWAY_ENVIRONMENT_NAME", "RAILWAY_ENVIRONMENT", "RAILWAY_PROJECT_ID"))
LOG_DIR = os.environ.get("CALL_LOG_DIR", os.path.join(ROOT, "logs", "calls"))


class RemoteSink:
    """Sends call-log rows to the backend (which stores them in Supabase), so logs survive a redeploy.

    Runs in a background thread. It batches rows, retries a failed batch a few times, and NEVER raises or blocks
    into a live call: a logging problem must not hurt a caller. The worker needs only BACKEND_URL and
    BACKEND_API_KEY, which it already has for the booking tools.
    """
    MAX_QUEUE = 5000

    def __init__(self, base_url, api_key="", post=None, interval=2.0):
        import queue
        import threading
        self.url = base_url.rstrip("/") + "/v1/calllog"
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.q = queue.Queue(self.MAX_QUEUE)
        self.dropped = 0
        self.sent = 0
        self._post = post or self._http_post
        self.interval = interval
        self._t = threading.Thread(target=self._run, name="calllog-sink", daemon=True)
        self._t.start()

    def _http_post(self, rows):
        import httpx
        r = httpx.post(self.url, json={"rows": rows}, headers=self.headers, timeout=5)
        r.raise_for_status()

    def put(self, row):
        try:
            self.q.put_nowait(row)
        except Exception:
            self.dropped += 1

    def _drain(self):
        rows = []
        while len(rows) < 100:
            try:
                rows.append(self.q.get_nowait())
            except Exception:
                break
        return rows

    def _run(self):
        while True:
            time.sleep(self.interval)
            self.flush()

    def flush(self):
        rows = self._drain()
        while rows:
            for attempt in range(3):
                try:
                    self._post(rows)
                    self.sent += len(rows)
                    break
                except Exception:
                    time.sleep(0.5 * (attempt + 1))
            else:
                self.dropped += len(rows)
            rows = self._drain()


_SINK = None


def default_sink():
    """One shared sink for the process, only on Railway (or when CALLLOG_REMOTE=1) and only if the backend is configured."""
    global _SINK
    if _SINK is not None:
        return _SINK or None
    on = os.environ.get("CALLLOG_REMOTE") == "1" or STDOUT_LOGS
    base = os.environ.get("BACKEND_URL", "")
    if on and base and "localhost" not in base and os.environ.get("CALLLOG_REMOTE") != "0":
        _SINK = RemoteSink(base, os.environ.get("BACKEND_API_KEY", ""))
    else:
        _SINK = False
    return _SINK or None


def load_pricing(path=PRICING_PATH):
    with open(path) as f:
        return json.load(f)


def llm_cost_usd(tier, prompt_tokens, completion_tokens, cached_tokens=0, pricing=None):
    """tier is 'large' or 'small'. Cached tokens are billed at the cache-read rate."""
    p = pricing or load_pricing()
    uncached = max(0, prompt_tokens - cached_tokens)
    in_rate = p[f"{tier}_model_usd_per_1m_input_tokens"]
    out_rate = p[f"{tier}_model_usd_per_1m_output_tokens"]
    if tier == "small" and prompt_tokens > p.get("small_model_long_prompt_threshold_tokens", 10**9):
        in_rate = p["small_model_long_prompt_input_usd_per_1m"]
        out_rate = p["small_model_long_prompt_output_usd_per_1m"]
    cache_rate = p[f"{tier}_model_usd_per_1m_cache_read_tokens"]
    return (uncached * in_rate + cached_tokens * cache_rate + completion_tokens * out_rate) / 1e6


@dataclass
class TurnRecord:
    call_id: str
    turn: int
    ts: float = field(default_factory=time.time)
    kind: str = "turn"
    tier: str = ""
    model: str = ""
    route_reason: str = ""
    prompt_version: str = ""
    step: str = ""
    language: str = ""
    stt_delay_s: float = 0.0        # end of caller speech to final transcript
    eou_delay_s: float = 0.0        # end of caller speech to turn committed
    hook_s: float = 0.0             # time inside on_user_turn_completed (our own code)
    llm_ttft_s: float = 0.0
    tts_ttfb_s: float = 0.0
    response_latency_s: float = 0.0  # end of caller speech to first agent audio (measured on live calls)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float = 0.0
    tools: list = field(default_factory=list)
    tool_ms: float = 0.0
    llm_calls: int = 0              # model calls behind this reply (2+ when a tool ran)
    llm_total_s: float = 0.0        # summed model time for those calls
    part: int = 0                   # nth reply item for this caller turn (tool turns make several)
    interrupted: bool = False
    filler_played: bool = False
    fallback_used: bool = False


class CallLogger:
    def __init__(self, call_id, log_dir=None, alert_webhook=None, sink=None):
        self.call_id = call_id
        self.sink = sink if sink is not None else default_sink()
        self.dir = log_dir or LOG_DIR
        os.makedirs(self.dir, exist_ok=True)
        self.path = os.path.join(self.dir, f"{call_id}.jsonl")
        self.alert_webhook = alert_webhook if alert_webhook is not None else os.environ.get("ALERT_WEBHOOK_URL", "")

    def _write(self, obj):
        line = json.dumps(obj)
        with open(self.path, "a") as f:
            f.write(line + "\n")
        if STDOUT_LOGS:
            # On Railway the disk is wiped on redeploy, so each row is also printed. Download the service logs
            # and rebuild the call files with scripts/import_railway_logs.py.
            print("CALLLOG " + line, flush=True)
        if self.sink:
            self.sink.put(obj)

    def turn(self, rec: TurnRecord):
        if not rec.cost_usd and rec.tier and (rec.prompt_tokens or rec.completion_tokens):
            rec.cost_usd = llm_cost_usd(rec.tier, rec.prompt_tokens, rec.completion_tokens, rec.cached_tokens)
        self._write(asdict(rec))

    def event(self, name, level="info", **data):
        row = {"kind": "event", "call_id": self.call_id, "ts": time.time(), "event": name, "level": level, **data}
        self._write(row)
        if level == "error":
            self.alert(name, data)

    def alert(self, name, data):
        """Best effort error alert. Never raises into the call."""
        if not self.alert_webhook:
            return
        try:
            import httpx
            httpx.post(self.alert_webhook, json={"text": f"[clinic-voice] {self.call_id}: {name} {json.dumps(data)[:500]}"}, timeout=3)
        except Exception:
            pass


def read_call(path):
    with open(path) as f:
        return [json.loads(x) for x in f if x.strip()]


TOOL_ERROR_EVENTS = {"identify_error", "book_failed", "book_pending", "transfer_log_failed", "callback_log_failed", "transfer_failed"}


def _pct(vals, q):
    if not vals:
        return None
    vals = sorted(vals)
    i = min(len(vals) - 1, int(round(q * (len(vals) - 1))))
    return vals[i]


def summarize(rows):
    turns = [r for r in rows if r.get("kind") == "turn"]
    events = [r for r in rows if r.get("kind") == "event"]
    if not rows:
        return {}
    t0, t1 = min(r["ts"] for r in rows), max(r["ts"] for r in rows)
    # The call ends when the line drops, not when the worker finishes shutting down (that can be 20s+ later).
    ends = [e["ts"] for e in events if e.get("event") in ("participant_left", "session_close", "room_disconnected", "silence_close")]
    if ends:
        t1 = min(t1, max(min(ends), max([r["ts"] for r in turns] or [t0])))
    minutes = max((t1 - t0) / 60.0, 1e-9)
    cost = sum(r.get("cost_usd", 0.0) for r in turns)
    lat = [r["response_latency_s"] for r in turns if r.get("response_latency_s")]
    tier_counts = {}
    for r in turns:
        tier_counts[r["tier"]] = tier_counts.get(r["tier"], 0) + 1
    return {
        "call_id": rows[0].get("call_id"),
        "turns": len(turns),
        "duration_min": round(minutes, 2),
        "llm_cost_usd": round(cost, 5),
        "llm_cost_per_min_usd": round(cost / minutes, 5),
        "latency_p50_s": _pct(lat, 0.5),
        "latency_p95_s": _pct(lat, 0.95),
        "latency_measured_turns": len(lat),
        "tier_counts": tier_counts,
        "interrupted_turns": sum(1 for r in turns if r.get("interrupted")),
        "fallback_turns": sum(1 for r in turns if r.get("fallback_used")),
        "stage_p50_s": {k: _pct([r[k] for r in turns if r.get(k)], 0.5)
                        for k in ("stt_delay_s", "eou_delay_s", "hook_s", "llm_ttft_s", "tts_ttfb_s")},
        "llm_calls_per_reply_p50": _pct([r["llm_calls"] for r in turns if r.get("llm_calls")], 0.5),
        "latency_p50_s_tool_replies": _pct([r["response_latency_s"] for r in turns if r.get("response_latency_s") and r.get("tools")], 0.5),
        "latency_p50_s_no_tool_replies": _pct([r["response_latency_s"] for r in turns if r.get("response_latency_s") and not r.get("tools")], 0.5),
        "error_events": sum(1 for e in events if e.get("level") == "error"),
        "tool_errors": sum(1 for e in events if e.get("event") in TOOL_ERROR_EVENTS),
        "false_booking_claims_spoken": sum(1 for e in events if e.get("event") == "false_booking_claim"),
        "claims_blocked": sum(1 for e in events if e.get("event") == "claim_blocked"),
        "events": sorted({e["event"] for e in events}),
    }


def _stages(r):
    parts = [f"{k}={r[k]:.2f}" for k in ("stt_delay_s", "eou_delay_s", "hook_s", "llm_ttft_s", "tts_ttfb_s") if r.get(k)]
    return f" [{' '.join(parts)}]" if parts else ""


def _llm(r):
    return f" [llm_calls={r['llm_calls']} llm_total_s={r.get('llm_total_s', 0):.2f} part={r.get('part', 0)}]" if r.get("llm_calls") else ""


def replay(path, out=print):
    """Readable timeline of one call from its log. CLI: python -c "from ava.telemetry import replay; replay('logs/calls/<id>.jsonl')" """
    rows = read_call(path)
    t0 = rows[0]["ts"] if rows else 0
    for r in rows:
        dt = r["ts"] - t0
        if r.get("kind") == "event":
            extra = {k: v for k, v in r.items() if k not in ("kind", "call_id", "ts", "event", "level")}
            out(f"{dt:7.1f}s  EVENT[{r['level']}] {r['event']} {json.dumps(extra)}")
        else:
            out(f"{dt:7.1f}s  TURN {r['turn']:>2} step={r['step']} lang={r['language']} tier={r['tier']} ({r['route_reason']}) "
                f"lat={r['response_latency_s']:.2f}s"
                f"{_stages(r)}{_llm(r)} tokens={r['prompt_tokens']}+{r['completion_tokens']} cost=${r['cost_usd']:.5f} "
                f"tools={r['tools']}{' INTERRUPTED' if r['interrupted'] else ''}{' FALLBACK' if r['fallback_used'] else ''}")
    out(json.dumps(summarize(rows), indent=2))
