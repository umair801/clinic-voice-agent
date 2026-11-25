# Decision 001: LiveKit Agents or Pipecat

Date: 2026-11-05. Status: provisional, with a kill criterion (see the end).

## What the spike was

A documentation-based comparison against the four criteria. I read the current LiveKit and
Pipecat docs and integration pages. I did NOT run either framework on a live call. Hands-on
validation happens in Phase 2, and this decision can be reversed there.

## Decision

LiveKit Agents for the voice layer. Twilio stays as the phone carrier through an Elastic SIP trunk.

## Comparison

| Criterion | LiveKit Agents | Pipecat |
|-----------|----------------|---------|
| Twilio SIP | Native SIP. A Twilio Elastic SIP trunk points at the LiveKit SIP endpoint, an inbound trunk plus a dispatch rule puts each caller in their own room, and the agent joins that room. Call transfer works over SIP REFER (needs PSTN transfer enabled on the trunk, and TLS transport is incompatible with REFER). | Twilio is supported as a WebSocket media stream, which the Pipecat docs describe as basic call control with no transfers or reconnects. Full SIP control goes through Daily's transport, which adds a second vendor between Twilio and the agent. |
| Turn detection | Open-weights turn detector model on top of VAD, plus STT endpointing and manual modes. Endpointing delays are tunable. | Smart Turn v3 (open source, local CPU inference) is the default, on top of VAD. Several start and stop strategies. |
| Interruptions | Adaptive interruption mode (audio model separates real interruptions from backchannel), false-interruption recovery, and preemptive generation with a documented cap on retries per turn. Maps directly to failures 2 and 3. | Interruptions on by default (bot stops, pending audio cleared). Krisp-based interruption prediction is available for backchannel. |
| Call replay | LiveKit Cloud agent observability gives transcript, traces, logs and audio on one timeline. Requires LiveKit Cloud media servers. Python testing helpers assert on messages and tool calls turn by turn. | No built-in recording. AudioBufferProcessor plus a third-party tool (several exist, community maintained) gives replay. Pipecat Evals gives scripted conversation tests. |

## Why LiveKit

1. Failure 5 and the job's own mention of transfers need a human-transfer path. SIP REFER on a real Twilio trunk is the cleaner route. Pipecat's Twilio WebSocket route documents no transfers.
2. Failures 2 and 3 are turn-taking problems, and LiveKit documents the exact controls (preemptive generation, adaptive interruption, endpointing delay) that the Phase 2 before/after has to tune and measure.
3. Replay is a Phase 5 deliverable. LiveKit gives it without adding a vendor.

## Trade-offs I am accepting

1. Replay and insights depend on LiveKit Cloud. The job post lists both LiveKit and Pipecat, so the portfolio shows one framework deeply, not both.
2. Pipecat's Smart Turn v3 runs locally for free and has no Cloud dependency. If LiveKit Cloud costs matter for the cost-per-minute story, count them in Phase 5.
3. Twilio SIP trunking needs account setup (trunk, origination URI, inbound trunk, dispatch rule). More setup than a WebSocket webhook.

## Unknowns (not decided from docs)

1. Real end-to-end latency of each stack on a phone call. Measured in Phase 2.
2. Whether the open-weights turn detector handles Spanish-accented English well enough. Measured in Phase 2 test calls.
3. LiveKit Cloud agent observability pricing after the beta period. Check the pricing page before quoting any cost.

## Kill criterion

If by the end of Phase 2 the LiveKit inbound call over Twilio SIP cannot hold a stable two-way conversation, or median time from end of caller speech to first agent audio cannot get under 1.5 seconds in test calls, re-run the spike hands-on with Pipecat on the Twilio WebSocket route and record the result here.

## Reusing the parked healthcare voice project

Not reused yet. The parked project lives on Umair's computer, not in this workspace. Nothing is copied until Umair approves it.

## Addendum, Phase 2 build status
The worker is written against the verified LiveKit Agents 1.8.10 API and constructs in the sandbox with dummy keys. It has not carried a live call. The turn detector plugin (`livekit.plugins.turn_detector`) is marked deprecated in favor of `livekit.agents.inference.TurnDetector` (hosted, with local fallback); switch if the plugin is removed. The kill criterion above still applies.

# Decision 004: One engine, many agents (2026-11-25)

Status: phase 1 done (no behavior change for the clinic agent).

Goal: show several working voice agents to clients (clinic front desk, medical practice receptionist, more later) without
copying the code. Each agent is a profile: config, prompt and releases in the repo, selected by an `agent_id`.

- One LiveKit worker name (`clinic-ava`) serves every agent. The browser token carries `{"agent_id": ...}` in the dispatch
  metadata and the worker loads that profile. Phone calls carry no metadata and get the default agent, so the live number is unchanged.
  Renaming the worker would mean re-creating the SIP dispatch rule, so it is not renamed.
- `clinic` keeps its original paths (`config/agent.json`, `releases/`). New agents live in `agents/<id>/` with their own pointer.
- An unknown `agent_id` is rejected (404 on the web endpoint, warning event in the worker), and a draft agent is not callable from the public page.
- Done since: `agent_id` columns exist in Supabase (migration 001, run by Umair, existing rows tagged `clinic`) and per-agent evals. Not done yet: the app does not write `agent_id` into those columns, and there are no per-agent frontends.
- One phone number serves one agent. More agents are browser-only until another number exists.


# Decision 005: Receptionist agent and the EHR write policy (2026-11-25)

Status: built and tested offline against a mock EHR. Not yet carried a live call. Registry status is `draft`, so the public page cannot reach it.

- Same engine, new brain. `ava/receptionist/core.py` is to the receptionist what `ava/core.py` is to the clinic agent: the voice worker
  and the evals drive the same code, and every rule is enforced there, not in the prompt. The shared voice base class (`VoiceAgentBase`)
  holds the per-turn hook, the streaming output guard, filler, transfer and end_call. The clinic agent's tool set was checked unchanged.
- Verification first: name + date of birth, ZIP when two people match, 3 tries then locked, the same message for any miss. Not the patient:
  general information and a message only.
- Writes are confirmed by READING. Measured on the athenahealth preview sandbox: book and cancel can take 60 to 150+ seconds and still land after
  the client timed out. So a timeout is `pending`, the write is never resent on a guess, one change is allowed per caller turn, and the agent
  may say "booked", "cancelled" or "moved" only when a read confirmed it. At call end the core does one more read-only look and logs what is still pending.
- Reschedule = book the new time, then cancel the old one. If the new time is confirmed and the old cancel is not, the agent says exactly that.
- Urgent words stop scheduling and send the caller to emergency services. The word list is a safety net, not triage.
- Privacy: call logs are redacted, messages are kept in their own folder (never in call logs), the audit log is hash-chained and refuses personal fields.
- Honest limits: the sandbox is shared and slow on writes, so a live demo against it will often say "still confirming". SMS is not built (Twilio console locked).
  Encryption at rest is not built. Designed for HIPAA, not certified.
