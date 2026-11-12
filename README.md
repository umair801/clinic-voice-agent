# Inbound Clinic Phone Agent: No Silent Calls, No False Bookings, No Duplicate Appointments

Clinic phone agents lose callers to dead air, tell them a visit is booked when it is not, and create duplicate or missing appointments. This system answers inbound clinic calls on a real phone number, books every family member exactly once, and says "booked" only when the clinic record confirms it.

Built for community health centers and specialty practices. All patient data is synthetic.

## Safeguards (only what is built)

- A booking is reported as booked only when the clinic record confirms it; unknown outcomes are reconciled by idempotency key before any retry, and the database rejects duplicates.
- Call state (every person on the call, current step) lives outside the model, so a fallback model resumes at the same step.
- The call ends only after the caller says goodbye and nothing is pending; silence with pending work becomes a callback task.
- A false "booked" claim is replaced with a safe line before the caller hears it (tested offline with a fake model stream; not yet confirmed on a live call).
- A silent caller is reprompted twice, then the call closes with a goodbye (pending work becomes a callback task).
- A transfer to a person happens only when the caller asks for one or a tool has failed.
- Human transfer and callback logging. The transfer path needs your SIP trunk settings and has not been tested on a live line yet.
- Per-turn latency, token and cost logs, call replay, and error alerts to a webhook.
- Config and prompts as versioned code with an eval gate, staged rollout and rollback.

## Proof

Orchestration evals: 59 of 59 pass (run against a stub model; they test the safeguards, not the language model). See `results/evals.json`.

Simulated-caller test results (release 1.0.5; a scripted synthetic caller over WebRTC, not phone calls, not production, no real patients):
- With a lost booking response injected, the agent confirmed the booking from the clinic record, made one attempt, and the database holds one appointment.
- In a 4-call run, 8 of 8 appointments were booked exactly once each, with no duplicates and no language switch on "Sí".
- Final clean batch, 9 calls after the deployment settled: 5 of 5 simple booking calls completed and booked exactly once each (0 duplicates, 0 false "booked" claims, English throughout), 3 of 3 provider answers matched the knowledge base, and 1 silent caller was reprompted twice and then closed by the agent. Details: `results/final_batch_sim_release_1.0.5.json`.
- LLM cost across the test runs: about $0.0006 to $0.0012 per call minute, LLM only. Breakdown and what is not measured: `docs/COST.md`.

One real phone test call (synthetic patients): a parent and two children were each booked once, and the call ran 2 minutes 34 seconds. That is one call, so no stability claim is made.

Not claimed yet: reply latency (median 1.9s to 2.2s in simulated runs, 2.23s with p95 2.57s over 25 turns in the final batch, and 2.6s on the one phone call; the 1.5s target is not met), phone-call stability across many calls, and human transfer on a live line. Failure-by-failure table: `docs/BEFORE_AFTER.md`.

## Try it with a synthetic patient

The agent books only patients who exist in the clinic record, and every record is synthetic. Unknown names get "no matching record", never an invented one. Use a first name and date of birth from this list, and never real personal details:

```
Maria, born March 14, 1982 (parent)
Lucas, born June 2, 2013 (child)
Sofia, born September 21, 2016 (child)
```

The test phone line is shared with reviewers on request. A browser test button on the demo page lets a reviewer talk to Ava without a phone, with per-visitor and daily limits.

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

Runs the baseline failure replay, its control cases, the 59 orchestration evals, and the modeled cost.

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
docs/       decisions, runbook, before/after, cost per minute
```

## Honesty notes

1. Synthetic patients only. No real PHI.
2. The baseline numbers come from a simulated replay of one reported failing call, not a real call.
3. Offline evals use a stub model. Voice latency (failures 1 to 3) and real cost can only be measured on live calls.
4. Cost figures are modeled from real list prices and stated assumptions until live logs replace them. STT, TTS and telephony are not included.
5. Run live so far: the backend (Supabase store) and the voice worker on Railway, simulated-caller calls against them, and real phone calls to the test number from one personal phone (synthetic patients). Not yet run live: the SIP human transfer success path, the text-mode live eval, and many-call phone stability.
