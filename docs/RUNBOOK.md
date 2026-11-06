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
- Roll back takes effect on the next call. No redeploy needed because releases are files in the image; commit the new POINTER.json and redeploy the worker, or set `AGENT_RELEASE=<old version>` on the worker for an instant pin.

## Code deploy (Railway)
Push to main. Railway builds the Dockerfile. Two services: backend (`SERVICE=backend`) and worker (`SERVICE=worker`). Set the health check path `/healthz` on the backend service only (Railway service settings). The worker does not serve that path.
Set `BACKEND_API_KEY` to the same value on both services, and `BACKEND_URL` on the worker to the backend service URL.
Rollback of code: Railway "Redeploy" on the previous deployment.

## Alerts
Error events (tool errors, pending bookings, false booking claims) post to `ALERT_WEBHOOK_URL` when set.

## Known limits
- The Supabase store and the Twilio setup script have not been run against live services.
- Eval gate tests the rails with a stub model. It does not test model wording or voice latency.
- The release gate runs evals in the machine that publishes. Wire it into CI before a team uses this.
