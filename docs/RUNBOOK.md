# Deploy and rollback runbook

All commands run from the repo root. Config and prompts exist only in git. Nothing edited in a dashboard can change a call.

## Release model
- `config/agent.json` and `prompts/system.md` are the working copy.
- `releases/<version>/` are immutable snapshots with a content hash. A snapshot that was edited after publish refuses to load.
- `releases/POINTER.json` says which version is stable and which (if any) is canary.
- The worker picks a version per call: `AGENT_RELEASE=current` follows the pointer; a version number pins it.

## Ship a change
1. Edit `config/agent.json` and/or `prompts/system.md`. Bump `version`.
2. `python -c "from tests.run_all import main; main()"`
3. `python -c "from ava.release import cli; cli(['publish'])"` runs the orchestration evals first and refuses to publish if any fails.
4. Staged rollout instead of a full switch: `python -c "from ava.release import cli; cli(['canary','1.1.0','10'])"` sends 10 percent of calls (chosen by hash of call id) to 1.1.0.
5. Watch `logs/calls` and alerts for a while: `python -c "from ava.telemetry import replay; replay('logs/calls/<file>.jsonl')"`.
6. Promote: `python -c "from ava.release import cli; cli(['promote'])"`.

## Roll back
- During a canary: `python -c "from ava.release import cli; cli(['rollback'])"` drops the canary.
- After promotion: same command returns stable to the previous version.
- Two ways to take effect. (1) Pointer: run the rollback command, commit `releases/POINTER.json`, push; Railway redeploys the worker and new calls use the old version. (2) Instant pin: set `AGENT_RELEASE=<old version>` on the worker in Railway; the worker restarts and new calls use it. Remove the variable to follow the pointer again. Either way, calls already running finish on the old container (draining window). How long a live rollback takes on Railway has not been timed yet.

## Rollback drill
Offline drill done 2026-11-13 in a temp copy of the repo (`results/rollback_drill_offline.json`): rollback 1.0.6 to 1.0.5 took under 0.1s and a new call picked 1.0.5 with the hash verified; roll forward through the canary command (eval gate 61 of 61) and promote returned to 1.0.6; `AGENT_RELEASE=1.0.5` pinned the old release with no file change; a release edited after publish was refused (hash mismatch). This is NOT a live drill. A live drill means setting `AGENT_RELEASE=1.0.5` on the Railway worker, timing the restart, confirming one call logs `release 1.0.5` in `call_start`, then removing the variable and confirming `1.0.6` again. Not done yet.

## Code deploy (Railway)
Push to main. Railway builds the Dockerfile. Two services: backend (`SERVICE=backend`) and worker (`SERVICE=worker`). Set the health check path `/healthz` on the backend service only (Railway service settings). The worker does not serve that path.
Set `BACKEND_API_KEY` to the same value on both services, and `BACKEND_URL` on the worker to the backend service URL.

Which pushes redeploy (Settings > Watch Paths, set on both services, in the Railway dashboard):
```
/ava/**  /prompts/**  /config/**  /releases/**  /Dockerfile  /requirements.txt  /railway.json  /clinic/**  /web/**
```
Pushes that only touch docs, results, scripts, tests, evals or harness do not redeploy. Verified on one push (a results-only commit started no new deployment). Watch paths live in the dashboard, not in git, so re-check them after any service is recreated.

Live calls during a deploy: the worker has `RAILWAY_DEPLOYMENT_DRAINING_SECONDS=300`, which gives running calls up to 5 minutes before the old container stops. Two test calls were cut off during earlier deploys (cause likely the redeploy, not proven). A call during a real deploy has not been tested with the draining window. Rule: do not push runtime changes while a call is running.

Rollback of code: Railway "Redeploy" on the previous deployment.

## Browser test line ("Talk to Ava")
Public button on the demo page. Off unless `WEB_CALL_ENABLED=1` on the **backend** service. Limits (backend variables, all optional):
```
WEB_CALL_MAX_SECONDS=240        hard stop per call (the server deletes the room)
WEB_CALL_MAX_CONCURRENT=2       calls at the same time
WEB_CALL_PER_IP_PER_HOUR=3      per visitor
WEB_CALL_DAILY_CAP=30           all visitors, resets at 00:00 UTC
```
Counters are in memory and reset when the backend restarts. Kill switch: remove `WEB_CALL_ENABLED` (button disappears, endpoint returns 503). Web calls have no phone leg, so a transfer request becomes a logged callback. Web test calls are a separate evidence class from phone calls and from the scripted simulated caller.

## Alerts
Error events (tool errors, pending bookings, false booking claims) post to `ALERT_WEBHOOK_URL` when set.

## Known limits
- The Supabase store and Twilio SIP trunk run live (test calls only). The human transfer success path has not been tested on a real line.
- Variables and watch paths are set in the Railway dashboard and are not versioned in git. Config and prompts are.
- Eval gate tests the rails with a stub model. It does not test model wording or voice latency.
- The release gate runs evals in the machine that publishes. Wire it into CI before a team uses this.
