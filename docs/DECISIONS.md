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
