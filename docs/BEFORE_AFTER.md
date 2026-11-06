# Failure by failure: before and after

Evidence classes
- BASELINE: simulated replay of the reported bad call (`results/BASELINE.md`). Not a real call.
- OFFLINE EVAL: the real rules run against a stub model (`results/evals.json`). Proves the safeguard holds. Does not prove model wording or voice latency.
- LIVE: measured on real test calls (synthetic patients). Empty until you run the 20+ calls. Do not quote anything from this column before then.

| # | Failure | Baseline (simulated) | Fix built | Offline evidence | Live test calls |
|---|---------|----------------------|-----------|------------------|-----------------|
| 1 | Dead silence during identify_patient | 7.8s silence, filler never fires | Filler speech after 1s, thinking sound, 4s tool budget with one safe retry | Tool timeout path evals only. Silence length is a voice-layer fact | PENDING |
| 2 | Callers say "hello?" | 9.0s gap, 1 hello turn | Dynamic endpointing 0.4 to 2.0s, preemptive generation, filler, thinking sound | None possible offline | PENDING |
| 3 | Two half-answers stitched | 2.0s overlapping speech | Adaptive interruption, false-interruption resume, preemptive TTS off | None possible offline | PENDING |
| 4 | Whole call goes Spanish | Switched on "Sí, correct", ignored "English, please" | Switch only on explicit request or a full Spanish sentence or 2 clear turns; names and "Sí" stay English; switch back on request | 4 evals: Sí, names, real Spanish, both directions | PENDING (STT language handling) |
| 5 | "You're all booked" after timeout; silent retry | False claim; retry wrote a duplicate | booked/pending/failed status; fixed idempotency key; reconcile by key before retry; DB unique constraint; output check flags false claims | 6 evals: lost response, timeout before commit, outage stays pending, retry reuses key, slot conflict, repeat calls | PENDING |
| 6 | Duplicate parent, no children | Parent 2, children 0 | One call state holds every person; one appointment each | 2 evals: family with lost response, replay of the bad-call pattern | PENDING |
| 7 | Ends call mid-booking | Silence timer hung up with a booking pending | end_call needs caller goodbye + no pending work + agent goodbye; silence policy hands pending work to a callback | 5 evals | PENDING |
| 8 | Fallback skips steps | Skipped collect_patients, verify_dob (modeled) | Step is derived from external state every turn; fallback adapter rebuilds the same instruction | 2 evals: swap model mid-call, instruction rebuilds identically | PENDING: measure real small-model behavior with `evals/live_eval.py` |
| 9 | 160K-character prompt | 160,000 chars, KB off | 1,084-char prompt + retrieval (about 360 chars per lookup) | 2 evals | n/a |
| 10 | Cost per minute | $0.1326/min modeled (LLM only) | Small model by default, large only when routing says so, per-turn token and cost log | Routing evals; `results/cost_model.json` is MODELED | PENDING: real cost from call logs |
| 11 | Dashboard edits vanish on deploy | Edit lost on deploy | Config and prompts in git, immutable hashed releases, eval gate, canary, rollback | Gate blocking proven by hand (a bad prompt change blocked the publish); tamper check proven | n/a |

Cost note: the modeled new cost uses assumptions listed in `results/cost_model.json`. STT, TTS and telephony are not included and will likely dominate. The honest cost-per-minute number comes from `scripts/summarize_calls.py` plus the vendors' invoices.
