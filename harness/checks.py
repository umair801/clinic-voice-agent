"""One repro check per failure. Each check returns a CheckResult.

Convention: passed=True means the behavior is CORRECT. The baseline must FAIL checks 1 to 9
and 11. Check 10 (cost) has no pass/fail because the task gives no budget; it records a baseline.

Thresholds below are targets WE chose, not numbers from the task. They are named constants so
they can be argued with and changed in one place.
"""
import re
from dataclasses import dataclass, field

# Targets chosen by us (adjustable). The task gives no numeric targets.
MAX_SILENCE_DURING_TOOL_S = 2.0     # no more than 2s of dead air while a tool runs
MAX_DEAD_AIR_AFTER_CALLER_S = 3.0   # agent audio should start within 3s of the caller finishing
MAX_PROMPT_CHARS = 20_000           # small prompt plus retrieval, instead of 160K pasted
HELLO_RE = re.compile(r"\b(hello|are you (still )?there|still there)\b", re.I)


@dataclass
class CheckResult:
    id: int
    title: str
    passed: object                 # True, False, or None (no pass/fail defined)
    metric: dict = field(default_factory=dict)
    evidence: str = ""
    where_it_lives: str = ""       # prompt, agent config, backend tool, or phone layer
    data_source: str = "harness replay of the task log (simulation, not a real call)"


def _agent_speech(tr):
    return sorted((e for e in tr.events if e["kind"] == "agent_speech"), key=lambda e: e["start"])


def _caller_turns(tr):
    return sorted((e for e in tr.events if e["kind"] == "caller"), key=lambda e: e["t"])


def check_1_silent_tool(tr):
    audio_starts = [e["start"] for e in _agent_speech(tr)] + [e["t"] for e in tr.events if e["kind"] == "filler"]
    tools = [e for e in tr.events if e["kind"] == "tool_call" and e["name"] == "identify_patient"]
    t = tools[0]
    inside = [a for a in audio_starts if t["start"] <= a <= t["end"]]
    silence = (min(inside) - t["start"]) if inside else (t["end"] - t["start"])
    return CheckResult(
        1, "identify_patient: dead silence while the tool runs", silence <= MAX_SILENCE_DURING_TOOL_S,
        {"identify_patient_s": round(t["end"] - t["start"], 2), "silence_s": round(silence, 2),
         "limit_s": MAX_SILENCE_DURING_TOOL_S, "filler_configured_after_s": tr.config["soft_timeout_s"],
         "tool_call_sound": tr.config["tool_call_sound"]},
        f"Tool ran {t['end'] - t['start']:.1f}s with no audio. soft_timeout_s is {tr.config['soft_timeout_s']}s, "
        f"so the filler never fires for a 7.8s tool, and tool_call_sound is none.",
        "agent config (filler threshold, tool sound) plus backend tool (latency)")


def check_2_hello(tr):
    caller = _caller_turns(tr)
    speech = _agent_speech(tr)
    hellos = [c for c in caller if HELLO_RE.search(c["text"])]
    dead = []
    for c in caller:
        if c.get("src") == "harness":
            continue
        end = c["t"] + c.get("dur", 2.0)
        nxt = [s["start"] for s in speech if s["start"] >= end]
        if nxt:
            dead.append(round(min(nxt) - end, 2))
    long_dead = [d for d in dead if d > MAX_DEAD_AIR_AFTER_CALLER_S]
    return CheckResult(
        2, 'Callers say "hello?" because of dead air', (len(hellos) == 0 and not long_dead),
        {"hello_turns": len(hellos), "dead_air_after_caller_s": dead, "turns_over_limit": len(long_dead),
         "limit_s": MAX_DEAD_AIR_AFTER_CALLER_S},
        f"{len(hellos)} 'hello?' turn(s); longest gap from end of caller speech to agent audio was {max(dead):.1f}s.",
        "phone layer (turn detection, response latency) plus agent config")


def check_3_overlap(tr):
    sp = _agent_speech(tr)
    total, pairs = 0.0, []
    for i, a in enumerate(sp):
        for b in sp[i + 1:]:
            ov = min(a["end"], b["end"]) - max(a["start"], b["start"])
            if ov > 0:
                total += ov
                pairs.append({"a": a["text"], "b": b["text"], "overlap_s": round(ov, 2),
                              "a_source": a["source"], "b_source": b["source"]})
    return CheckResult(
        3, "Two half-answers stitched together (overlapping agent speech)", total == 0,
        {"overlap_total_s": round(total, 2), "overlapping_pairs": pairs},
        f"{total:.1f}s where two agent replies play at once: a speculative reply was started on a partial "
        f"transcript and not cancelled when the final transcript produced a second reply.",
        "phone layer (speculative turn handling, interruption and cancel logic)")


def check_4_language(tr):
    switches = tr.language_by_time
    final_lang = "es" if (switches and switches[-1]["lang"] == "es") else "en"
    switched_on_si = any("Sí" in s["trigger"] for s in switches)
    return CheckResult(
        4, "Whole call switches to Spanish on a single non-English word", (not switched_on_si and final_lang == "en"),
        {"switches": switches, "final_language": final_lang, "asked_for_english_at_s": 80.0,
         "agent_replied_in_spanish_after_request": any(
             e["kind"] == "agent_speech" and e["start"] >= 80 and e["lang"] == "es" for e in tr.events)},
        'language_rule flips to Spanish on "Sí, correct" and has no path back, so the caller\'s "English, please" is ignored.',
        "agent config (language_rule) plus prompt")


def check_5_false_booked(tr):
    claims = []
    for e in _agent_speech(tr):
        if "booked" in e["text"].lower():
            prior = [x for x in tr.events if x["kind"] == "tool_result" and x["name"] == "book_appointment" and x["t"] <= e["start"]]
            last_call = [x for x in tr.events if x["kind"] == "tool_call" and x["name"] == "book_appointment" and x.get("end") is not None and x["end"] <= e["start"]]
            ok = bool(last_call) and last_call[-1]["result"] and last_call[-1]["result"].get("success") is True
            claims.append({"t": e["start"], "said": e["text"], "confirmed": bool(ok),
                           "tool_result": last_call[-1]["result"] if last_call else None})
    false_claims = [c for c in claims if not c["confirmed"]]
    retries = [e for e in tr.events if e["kind"] == "background_retry"]
    return CheckResult(
        5, 'Says "You\'re all booked" after a timeout, then retries silently', (not false_claims and not retries),
        {"claims": claims, "false_claims": len(false_claims), "silent_background_retries": len(retries),
         "idempotency_key_used": False},
        'book_appointment returned {"success": false, "error": "timeout"} and the agent still said "booked". '
        "The server had already written the row, and the background retry (no idempotency key) wrote it again.",
        "backend tool (idempotency, explicit status) plus prompt (only say booked on confirmed status)")


def check_6_multi_patient(tr):
    from clinic.data import FAMILY
    counts = {k: sum(1 for a in tr.appointments if a["patient_id"] == v["id"]) for k, v in FAMILY.items()}
    ok = all(v == 1 for v in counts.values())
    return CheckResult(
        6, "Duplicate parent appointments, none for the children", ok,
        {"appointments_per_patient": counts, "expected_each": 1},
        f"Records after the call: {counts}. The parent has a duplicate from failure 5, and the call ended before either child was booked.",
        "agent workflow state (one patient held, no booking list) plus backend tool")


def check_7_end_call(tr):
    ends = [e for e in tr.events if e["kind"] == "end_call"]
    bad = [e for e in ends if e.get("pending_tool") or not e.get("caller_said_goodbye")]
    return CheckResult(
        7, "Agent ends the call mid-booking", not bad,
        {"end_call_events": ends, "violations": len(bad)},
        "end_call fired from the silence timer while book_appointment for the second child was still pending, with no goodbye from the caller. "
        "Candidate cause A (proven in this replay): silence_end_call_s. Candidate cause B (not replayed): the wrap_up->end_call edge firing on the word 'No' in 'No, wait, my son too'. "
        "Real logs must show which one fired (end_call reason field).",
        "agent config (silence_end_call_s) and workflow edges (end_call guard)")


def check_8_fallback(fb_tr):
    resume = [e for e in fb_tr.events if e["kind"] == "fallback_resume"][0]
    required = {"verify_dob", "collect_patients"}
    missed = sorted(required - set(resume["steps_executed"]))
    return CheckResult(
        8, "Fallback to the small model skips steps", not missed,
        {"steps_skipped": missed, "steps_executed_after_fallback": resume["steps_executed"]},
        f"After the large model timed out, the fallback ran {resume['steps_executed']} and skipped {missed}. "
        "Workflow state exists only in the model context, so the fallback cannot know where to resume. "
        "NOTE: which steps a real small model skips is MODELED here, per the task text; measure it in Phase 4.",
        "agent architecture (workflow state outside the model)",
        "modeled behavior of the fallback model (needs real measurement in Phase 4)")


def check_9_prompt(tr, prompt_chars, tokens):
    ok = prompt_chars <= MAX_PROMPT_CHARS and tr.config["knowledge_base"] != "off"
    return CheckResult(
        9, "160K-character prompt with the knowledge base off", ok,
        {"prompt_chars": prompt_chars, "est_input_tokens_per_turn_from_prompt": tokens,
         "knowledge_base": tr.config["knowledge_base"], "target_max_chars": MAX_PROMPT_CHARS},
        f"Every provider and location document is in the system prompt ({prompt_chars:,} chars, about {tokens:,} tokens "
        "by a 4-chars-per-token estimate) and is sent on every turn.",
        "prompt plus agent config (knowledge base off)",
        "harness measurement of a synthetic prompt built to the 160,000-char size in the task; tokens are estimated")


def check_10_cost(tr, pricing):
    turns = tr.turns
    inp = sum(t["input_tokens"] for t in turns)
    out = sum(t["output_tokens"] for t in turns)
    minutes = tr.duration_s / 60.0
    usd = (inp * pricing["large_model_usd_per_1m_input_tokens"] + out * pricing["large_model_usd_per_1m_output_tokens"]) / 1e6
    return CheckResult(
        10, "Cost per minute over budget (baseline recorded, no budget in the task)", None,
        {"agent_turns": len(turns), "call_minutes": round(minutes, 2),
         "input_tokens_total_est": inp, "output_tokens_total_est": out,
         "input_tokens_per_call_minute_est": round(inp / minutes),
         "llm_usd_per_minute_modeled": round(usd / minutes, 4),
         "pricing_basis": "Claude Sonnet 5.5 list price, no caching (see config/pricing.json)", "scope": "LLM tokens only (no STT, TTS, telephony)"},
        "Token volume is a harness measurement (estimated at 4 chars per token). The dollar figure applies real list prices from "
        "config/pricing.json, but the baseline model is ASSUMED to be priced like Sonnet 5.5 (the task does not name it) and caching is ignored, "
        "so it is a modeled figure. The task gives no budget, so this records a baseline instead of pass or fail.",
        "prompt (size) plus agent config (model routing)",
        "tokens: harness measurement (estimated); dollars: modeled with real list prices and an assumed model")


def check_11_deploy(deploy):
    deploy.dashboard_edit("You are Ava. Book appointments. Never read DOB back twice. [new dashboard edit]")
    res = deploy.deploy_from_repo()
    lost = res["live_hash_before"] != res["live_hash_after"]
    return CheckResult(
        11, "Dashboard edits vanish on deploy", not lost,
        {"live_prompt_hash_before_deploy": res["live_hash_before"], "live_prompt_hash_after_deploy": res["live_hash_after"],
         "edit_survived": not lost},
        "A dashboard edit was made, then a deploy from the repo replaced the live prompt with the older repo copy. "
        "The edit had no version, no review and no eval, and was never in the repo.",
        "deployment process (config and prompt are not code)")
