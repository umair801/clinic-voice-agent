# Cost per minute

Scope first: only the LLM part is measured. STT, TTS, telephony and LiveKit Cloud are NOT measured and are NOT in any number below.

## What was measured (LLM only)

| Source | Evidence class | LLM cost per call minute | Basis |
|---|---|---|---|
| Earlier simulated-caller batch | SIMULATED CALLER | about $0.0008 | 10.2 call minutes (`results/sim_leave_and_si_release_1.0.5.json`) |
| Final clean batch, simple booking | SIMULATED CALLER | about $0.00059 | 5 calls (`results/final_batch_sim_release_1.0.5.json`) |
| Latest batch, 14 calls | SIMULATED CALLER | about $0.00049 | 15.9 call minutes (`results/batch2_sim_current_code.json`) |
| One real phone call | PHONE | about $0.0012 | 154 seconds (`results/phone_test_calls.json`) |

Range across test runs: about $0.0006 to $0.0012 per minute. These are test-call results with synthetic patients, not production. The three numbers differ because the calls differ (family bookings take more turns), so do not average them into one claim.

## Baseline for comparison (modeled, not measured)

The reported failing setup, replayed with the same token method, models to about $0.135 per minute, LLM only (`results/cost_model.json`). That is a model of one reported call, not a measurement, and the measured numbers above are not like for like with it. The honest statement is: the small prompt, retrieval, prompt caching and small-model routing cut modeled LLM cost by a large factor, and the measured LLM cost is now a fraction of a cent per minute.

## Where the saving comes from

- Prompt: 160,000 characters down to about 2,000 characters plus retrieval of provider and location data.
- Cache: about 80 percent of prompt tokens were served from cache in the simulated runs.
- Routing: the small model (`claude-haiku-5-5`) runs routine turns; the large model (`claude-sonnet-5-5`) runs only when routing says so. Each turn logs tier, route reason, tokens and cost.
- Prices used: `config/pricing.json`, list prices fetched 2026-11-05. Re-check before quoting.

## Partial all-in estimate (2026-11-13, test traffic, not production)

| Part | Per call minute | How it was obtained |
|---|---|---|
| LLM (Anthropic) | about $0.0006 to $0.0031 | measured from call logs; $0.00087 on the release 1.0.6 phone call; the higher end was with the larger model answering family turns |
| Speech to text (Deepgram Nova-3, multilingual, streaming) | $0.0092 | LIST price; the console shows 4.94 hours (296.7 minutes) of audio from 172 connections between Nov 6 and Nov 12, which is about $2.73 at list price. The billed amount is not shown and may be covered by free credit |
| Voice (ElevenLabs Turbo/Flash) | about $0.008 to $0.016 | ESTIMATE, not measured: list price $0.04 per 1,000 characters, assuming 200 to 400 spoken characters per call minute. The agent text per call is not logged yet |
| Telephony (Twilio) and LiveKit Cloud | not measured | Twilio console is locked; LiveKit usage page not read yet |

Rough all-in range excluding telephony and LiveKit: about $0.018 to $0.030 per call minute. Speech to text and voice are therefore most of the cost, as expected. The ElevenLabs dashboard showed 28,168 of 90,000 workspace credits used, but that covers the whole workspace and billing period, so it cannot be split by call.

To turn the voice estimate into a measurement: read ElevenLabs credits used per day for a day that only had test calls, and divide by the call minutes in the logs for that day. Or log the characters sent to the voice in each turn (a small code change, not done).

## What is NOT measured yet, and how to find out

Once the LLM bill is this small, STT, TTS and telephony probably dominate the real cost per minute. That is an expectation, not a measurement.

1. Deepgram: usage in its console for the test period, divided by call minutes.
2. ElevenLabs: characters used in its usage page for the test period, divided by call minutes.
3. Twilio SIP trunk and number: usage and rental in its console (blocked at the moment by the account lockout and support ticket).
4. LiveKit Cloud: agent session and SIP minutes in its usage page.

Add each vendor's usage for a known batch of test calls, divide by the same call minutes, and put the sum next to the LLM figure. Until the voice and telephony parts are measured, the all-in figure above is an estimate with a range, not a measured result.
