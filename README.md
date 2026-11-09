# Inbound Clinic Phone Agent: No Silent Calls, No False Bookings, No Duplicate Appointments

Clinic phone agents lose callers to dead air, tell them a visit is booked when it is not, and create duplicate or missing appointments. This system answers inbound clinic calls on a real phone number, books every family member exactly once, and says "booked" only when the clinic record confirms it.

Built for community health centers and specialty practices. All patient data is synthetic.

## Safeguards (only what is built)

- A booking is reported as booked only when the clinic record confirms it; unknown outcomes are reconciled by idempotency key before any retry, and the database rejects duplicates.
- Call state (every person on the call, current step) lives outside the model, so a fallback model resumes at the same step.
- The call ends only after the caller says goodbye and nothing is pending; silence with pending work becomes a callback task.
- A false "booked" claim is replaced with a safe line before the caller hears it (tested offline with a fake model stream; not yet confirmed on a live call).
- A transfer to a person happens only when the caller asks for one or a tool has failed.
- Human transfer and callback logging. The transfer path needs your SIP trunk settings and has not been tested on a live line yet.
- Per-turn latency, token and cost logs, call replay, and error alerts to a webhook.
- Config and prompts as versioned code with an eval gate, staged rollout and rollback.

## Proof

Orchestration evals: 51 of 51 pass (run against a stub model; they test the safeguards, not the language model). See `results/evals.json`.

Test-call results: PENDING. Development calls so far ran from a laptop, so no latency or cost number is claimed until the 20+ synthetic test calls run on the deployed service. Failure-by-failure table: `docs/BEFORE_AFTER.md`.

## Try it with a synthetic patient

The agent books only patients who exist in the clinic record, and every record is synthetic. Unknown names get "no matching record", never an invented one. Use a first name and date of birth from this list, and never real personal details:

```
Maria, born March 14, 1982 (parent)
Lucas, born June 2, 2013 (child)
Sofia, born September 21, 2016 (child)
```

The test phone line is shared with reviewers on request.

## Links

Live demo: https://clinic.datawebify.com
GitHub: https://github.com/umair801/clinic-voice-agent
Loom: TO ADD

## How it is built

```
caller -> Twilio number -> SIP trunk -> LiveKit room -> voice worker (ava/voice/agent.py)
                                                          |  STT: Deepgram   TTS: ElevenLabs
                                                          v
                          AvaCore (ava/core.py): state, language, guards, routing, retrieval, logs
                                                          v
                          backend tools (ava/backend/): identify, atomic idempotent booking, slots, transfer log
```

Stack: Python, FastAPI, LiveKit Agents, Twilio SIP, Deepgram, ElevenLabs, Anthropic models, Supabase (SQLite for local tests), Docker, Railway. Why LiveKit: `docs/DECISIONS.md`.

## Run the offline checks

```
python -c "from tests.run_all import main; main()"
```

Runs the baseline failure replay, its control cases, the 51 orchestration evals, and the modeled cost.

## Layout

```
ava/        core, state, language, guards, routing, retrieval, telemetry, release manager
ava/backend FastAPI tools, store (SQLite and Supabase), idempotent booking
ava/voice   LiveKit worker
clinic/     synthetic data and the baseline agent that fails on purpose
harness/    replay of the reported call, one check per failure, controls, modeled cost
evals/      orchestration evals (stub model) and a text-mode live eval (real models, not yet run)
config/ prompts/ releases/   versioned configuration, prompt, immutable snapshots
scripts/    SQL schema, seed, SIP setup, call summary, pre-push safety check
docs/       decisions, runbook, before/after
```

## Honesty notes

1. Synthetic patients only. No real PHI.
2. The baseline numbers come from a simulated replay of one reported failing call, not a real call.
3. Offline evals use a stub model. Voice latency (failures 1 to 3) and real cost can only be measured on live calls.
4. Cost figures are modeled from real list prices and stated assumptions until live logs replace them. STT, TTS and telephony are not included.
5. Run on real phone calls so far: the voice worker, from a laptop (development test calls, one caller, synthetic patients). Not yet run live: the Supabase store, the SIP human transfer, the text-mode live eval, and any measurement from the deployed service.
