# Baseline results (Phase 1)

Label: simulation of the task's bad call. These are NOT real phone calls and NOT production results.
Timestamps tagged `log` in harness/scenario.py come from the task PDF; `harness` ones are our assumptions.

| # | Failure | Status | Key measurement | Cause lives in |
|---|---------|--------|-----------------|----------------|
| 1 | identify_patient: dead silence while the tool runs | FAIL | `{"identify_patient_s": 7.8, "silence_s": 7.8, "limit_s": 2.0, "filler_configured_after_s": 8, "tool_call_sound": "none"}` | agent config (filler threshold, tool sound) plus backend tool (latency) |
| 2 | Callers say "hello?" because of dead air | FAIL | `{"hello_turns": 1, "dead_air_after_caller_s": [9.0, 1.5, 0.8, 0.3, 1.0], "turns_over_limit": 1, "limit_s": 3.0}` | phone layer (turn detection, response latency) plus agent config |
| 3 | Two half-answers stitched together (overlapping agent speech) | FAIL | `{"overlap_total_s": 2.0, "overlapping_pairs": [{"a": "Thank you, let me check that date", "b": "Got it, one moment while I confirm", "overlap_s": 2...` | phone layer (speculative turn handling, interruption and cancel logic) |
| 4 | Whole call switches to Spanish on a single non-English word | FAIL | `{"switches": [{"t": 41.0, "lang": "es", "trigger": "S\u00ed, correct"}], "final_language": "es", "asked_for_english_at_s": 80.0, "agent_replied_in_...` | agent config (language_rule) plus prompt |
| 5 | Says "You're all booked" after a timeout, then retries silently | FAIL | `{"claims": [{"t": 126.0, "said": "You're all booked.", "confirmed": false, "tool_result": {"success": false, "error": "timeout"}}], "false_claims":...` | backend tool (idempotency, explicit status) plus prompt (only say booked on confirmed status) |
| 6 | Duplicate parent appointments, none for the children | FAIL | `{"appointments_per_patient": {"parent": 2, "child1": 0, "child2": 0}, "expected_each": 1}` | agent workflow state (one patient held, no booking list) plus backend tool |
| 7 | Agent ends the call mid-booking | FAIL | `{"end_call_events": [{"t": 472.0, "kind": "end_call", "reason": "silence_end_call_s", "pending_tool": true, "caller_said_goodbye": false}], "violat...` | agent config (silence_end_call_s) and workflow edges (end_call guard) |
| 8 | Fallback to the small model skips steps | FAIL | `{"steps_skipped": ["collect_patients", "verify_dob"], "steps_executed_after_fallback": ["choose_slot", "book", "confirm", "wrap_up"]}` | agent architecture (workflow state outside the model) |
| 9 | 160K-character prompt with the knowledge base off | FAIL | `{"prompt_chars": 160000, "est_input_tokens_per_turn_from_prompt": 40000, "knowledge_base": "off", "target_max_chars": 20000}` | prompt plus agent config (knowledge base off) |
| 10 | Cost per minute over budget (baseline recorded, no budget in the task) | RECORDED | `{"agent_turns": 13, "call_minutes": 7.87, "input_tokens_total_est": 521109, "output_tokens_total_est": 95, "input_tokens_per_call_minute_est": 6624...` | prompt (size) plus agent config (model routing) |
| 11 | Dashboard edits vanish on deploy | FAIL | `{"live_prompt_hash_before_deploy": "df45e3947df2", "live_prompt_hash_after_deploy": "77a1f373c14f", "edit_survived": false}` | deployment process (config and prompt are not code) |

## Evidence per failure

### 1. identify_patient: dead silence while the tool runs
Status: FAIL

Tool ran 7.8s with no audio. soft_timeout_s is 8s, so the filler never fires for a 7.8s tool, and tool_call_sound is none.

Data source: harness replay of the task log (simulation, not a real call)

### 2. Callers say "hello?" because of dead air
Status: FAIL

1 'hello?' turn(s); longest gap from end of caller speech to agent audio was 9.0s.

Data source: harness replay of the task log (simulation, not a real call)

### 3. Two half-answers stitched together (overlapping agent speech)
Status: FAIL

2.0s where two agent replies play at once: a speculative reply was started on a partial transcript and not cancelled when the final transcript produced a second reply.

Data source: harness replay of the task log (simulation, not a real call)

### 4. Whole call switches to Spanish on a single non-English word
Status: FAIL

language_rule flips to Spanish on "Sí, correct" and has no path back, so the caller's "English, please" is ignored.

Data source: harness replay of the task log (simulation, not a real call)

### 5. Says "You're all booked" after a timeout, then retries silently
Status: FAIL

book_appointment returned {"success": false, "error": "timeout"} and the agent still said "booked". The server had already written the row, and the background retry (no idempotency key) wrote it again.

Data source: harness replay of the task log (simulation, not a real call)

### 6. Duplicate parent appointments, none for the children
Status: FAIL

Records after the call: {'parent': 2, 'child1': 0, 'child2': 0}. The parent has a duplicate from failure 5, and the call ended before either child was booked.

Data source: harness replay of the task log (simulation, not a real call)

### 7. Agent ends the call mid-booking
Status: FAIL

end_call fired from the silence timer while book_appointment for the second child was still pending, with no goodbye from the caller. Candidate cause A (proven in this replay): silence_end_call_s. Candidate cause B (not replayed): the wrap_up->end_call edge firing on the word 'No' in 'No, wait, my son too'. Real logs must show which one fired (end_call reason field).

Data source: harness replay of the task log (simulation, not a real call)

### 8. Fallback to the small model skips steps
Status: FAIL

After the large model timed out, the fallback ran ['choose_slot', 'book', 'confirm', 'wrap_up'] and skipped ['collect_patients', 'verify_dob']. Workflow state exists only in the model context, so the fallback cannot know where to resume. NOTE: which steps a real small model skips is MODELED here, per the task text; measure it in Phase 4.

Data source: modeled behavior of the fallback model (needs real measurement in Phase 4)

### 9. 160K-character prompt with the knowledge base off
Status: FAIL

Every provider and location document is in the system prompt (160,000 chars, about 40,000 tokens by a 4-chars-per-token estimate) and is sent on every turn.

Data source: harness measurement of a synthetic prompt built to the 160,000-char size in the task; tokens are estimated

### 10. Cost per minute over budget (baseline recorded, no budget in the task)
Status: RECORDED

Token volume is a harness measurement (estimated at 4 chars per token). The dollar figure applies real list prices from config/pricing.json, but the baseline model is ASSUMED to be priced like Sonnet 5.5 (the task does not name it) and caching is ignored, so it is a modeled figure. The task gives no budget, so this records a baseline instead of pass or fail.

Data source: tokens: harness measurement (estimated); dollars: modeled with real list prices and an assumed model

### 11. Dashboard edits vanish on deploy
Status: FAIL

A dashboard edit was made, then a deploy from the repo replaced the live prompt with the older repo copy. The edit had no version, no review and no eval, and was never in the repo.

Data source: harness replay of the task log (simulation, not a real call)
