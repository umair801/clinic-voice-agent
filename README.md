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

Every number below comes from test calls and offline checks with synthetic patients. None of it is production or real patient traffic.

| What was run | Result |
|---|---|
| 61 offline scenarios against the real orchestration code (stub model) | 61 of 61 pass. They test the safeguards, not the language model. `results/evals.json` |
| 23 scripted simulated-caller calls (release 1.0.5) | Every booking made exactly once, 0 duplicates, 0 false "booked" claims, "Si" did not switch the language, silent callers closed by the agent, 3 of 3 provider answers matched the knowledge base. Latest batch of 14: reply median 1.76s, p95 2.46s over 57 turns. `results/final_batch_sim_release_1.0.5.json`, `results/batch2_sim_current_code.json` |
| 6 real calls by the builder (3 browser, 3 phone) | All ended after the goodbye, bookings once per patient (1, 3, 1, 3), 0 duplicate keys in the database, a callback reference saved and read back. Reply median 1.9s, p95 2.78s over 43 turns. `results/real_test_calls_batch3.json` |
| 3 real phone calls after a model fix | A family of three booked once each; a call where one child was left for another time booked the other two and did not ask again; a call where the caller's name was misheard found no record, invented none and booked 0. Reply median 2.3s, p95 3.71s over 36 turns. `results/real_phone_calls_after_thinking_fix.json` |
| 1 real phone call on release 1.0.6 | Booked once, 0 model errors, small model only, reply median 2.27s over 6 turns. `results/real_phone_call_release_1.0.6.json` |

Cost per call minute: the model (LLM) costs about $0.0006 to $0.0031, measured from call logs. A partial all-in estimate, excluding telephony and the LiveKit service, is about $0.018 to $0.030 per minute, where speech-to-text uses the vendor list price and the voice part is an estimate, not a measurement. `docs/COST.md`

Found by testing real calls, not in the original task: a "leave it for another time" answer was asked again and again (fixed in call state), and the larger model was failing and silently replaced by the small one (fixed, and the log now records a model that errors). Both are in `docs/BEFORE_AFTER.md`.

Not claimed: the 1.5 second reply target (not met; the working target of 2.0 seconds is met in some batches and missed in others), phone-call stability (five phone calls dropped or were hung up by the caller's line mid-call and the cause is not found), human transfer on a live line (not tested), callers who are native English speakers (the tester is not, and a name was misheard), and the Twilio and LiveKit parts of the cost. Failure-by-failure table: `docs/BEFORE_AFTER.md`.

## Try it with a synthetic patient

The agent books only patients who exist in the clinic record, and every record is synthetic. Unknown names get "no matching record", never an invented one. Use a first name and date of birth from this list, and never real personal details:

```
Maria, born March 14, 1982 (parent)
Lucas, born June 2, 2013 (child)
Sofia, born September 21, 2016 (child)
Umair, born May 20, 1990 (adult, single visit)
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

Runs the baseline failure replay, its control cases, the 61 orchestration evals, and the modeled cost.

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
4. The model cost is measured from call logs. Speech-to-text uses the vendor list price, the voice cost is an estimate, and telephony and LiveKit are not included.
5. Run live so far: the backend (Supabase store) and the voice worker on Railway, simulated-caller calls, browser calls and real phone calls to the test number from one personal phone (synthetic patients). Not yet run live: the SIP human transfer success path, the text-mode live eval, and many-call phone stability.
