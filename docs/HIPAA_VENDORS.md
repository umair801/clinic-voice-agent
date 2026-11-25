# Vendor BAA status (checked 2026-11-25)

Purpose: before any real patient data touches this system, every vendor that would see it needs a signed Business Associate Agreement (BAA).
Today the system uses synthetic data only, so no BAA is needed for the demo. This table says what a real clinic deployment would require.
Designed for HIPAA, not certified. This is research notes, not legal advice. Plans and prices change: re-check before relying on it.

| Vendor | Role | BAA available? | Condition found | Source | Status |
|---|---|---|---|---|---|
| Anthropic | LLM | Yes | HIPAA-ready API organization with a signed BAA. Batch, Files, Skills, Code Execution APIs are not covered. "Covered Models" need 30-day retention and cannot be used with zero data retention. Which of our two models count as Covered Models: not checked. | support.claude.com/en/articles/8114513 and /15455031, platform.claude.com/docs/en/manage-claude/api-and-data-retention | Verified on vendor pages |
| ElevenLabs | Text to speech | Enterprise tier only | BAA plus Zero Retention Mode. Their HIPAA page covers ElevenLabs Agents. We call the TTS API only. Whether TTS API calls are covered by the BAA: unknown, ask. | elevenlabs.io/docs/eleven-agents/legal/hipaa | Verified on vendor page, TTS coverage unknown |
| Supabase | Database | Yes, paid | Team or Enterprise plan, signed BAA, HIPAA add-on, project set to High Compliance. Not available on Free or Pro. | supabase.com/docs/guides/platform/hipaa-projects, supabase.com/legal/customer-resources/business-associate-agreement | Verified on vendor pages (plan floor from secondary sources) |
| Railway | Hosting | Yes, paid | HIPAA BAA is an add-on with a paid monthly spend threshold. | docs.railway.com/enterprise/compliance | Verified on vendor page |
| Twilio | Phone, SIP | Yes for HIPAA-eligible products | Voice and SIP are listed as eligible in search results. Plan or edition requirement differs between sources. | not yet read on twilio.com | NOT verified on vendor page |
| LiveKit | Realtime audio | Yes on request | Cloud Scale or Enterprise. Approval not guaranteed. Only eligible features. | not yet read on livekit.io | NOT verified on vendor page |
| Deepgram | Speech to text | Yes on request | Provided on request through their trust center. | not yet read on deepgram.com | NOT verified on vendor page |
| athenahealth | EHR | Not applicable to the sandbox | We use the Preview sandbox with dummy data. A real practice signs its own agreement with athenahealth. | developer portal | Sandbox only |

## What this means

- A real deployment needs paid plans or add-ons at four or more vendors. The demo avoids this by using synthetic data only.
- The minimum a clinic pays before real calls: Supabase Team plus add-on, Railway spend threshold, ElevenLabs Enterprise, a Twilio and LiveKit and Deepgram BAA.
- Cheaper design choice to offer a client: store no PHI in Supabase at all (keep patient data in the EHR only, log pseudonymous references). The audit log and redaction already work this way.

## Open questions to ask vendors (only when a real client exists)
1. Anthropic: are claude-haiku-5-5 and claude-sonnet-5-5 Covered Models, and what retention applies in a HIPAA-ready org?
2. ElevenLabs: does the BAA cover the plain TTS API, and does Zero Retention Mode apply to it?
3. Twilio, LiveKit, Deepgram: confirm plan, BAA process and eligible features on their own pages.
