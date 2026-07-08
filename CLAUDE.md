# Mission Control (`~/Desktop/automation/mission-control`)

Vaishak's agent-company dashboard: kanban board + live infra/usage telemetry + `/mission` operator loop over his ventures. Stdlib-only Python backend, no framework, no build step. Git repo initialized but has **zero commits yet** — everything currently shown as untracked.

## Run / verify
- Start: `python server.py` (or double-click `MISSION-CONTROL.bat`) → http://localhost:8799 (port 8799, stdlib `http.server`, no deps to install).
- Unattended mission runner: `MISSION-RUNNER.bat` (wraps `mission-runner.ps1`, polls `/api/state` every 60s, dispatches headless `claude -p "/mission ..."` — human approval gate via dashboard before it executes).
- Verify rendered: open http://localhost:8799 in a browser — Board tab (kanban), Overview tab (NBA panel), Agents tab (workflow runs + resume). No separate build; edits to `index.html`/`server.py` are live on refresh/restart.
- Key endpoints: `/api/state` (GET/POST kanban+ratings, POST is per-card upsert keyed by `id`), `/api/job` (POST, mission/job card upserts), `/api/nba`, `/api/summary`, `/api/services`, `/api/workflows`, `/api/doctor?fix=1`.

## Gotchas
- **`/api/job` upsert is NOT a deep merge for all callers** — `mission-runner.ps1` posts `{id, status}` only when marking a mission "dispatched", and the resulting board record can end up with just those two fields, losing `text`/`v`/`mission`/etc. If a card looks stripped, recover the original mission text from `runner-logs/runner-<date>.log` (the runner logs the full mission text at dispatch time before POSTing the thin update) and re-POST the full card.
- Headless runner auth: needs `CLAUDE_CODE_OAUTH_TOKEN` in the **User** env var (`setx`), not just the logged-in desktop session — mirrors `agent-failover.ps1`.
- Runner dispatch requires a human Approve click on the dashboard (Agents tab) — it will NOT execute unattended without that; default is deny-on-timeout (1800s).
- `runtime.json` `executor` field can be set to `"failover"` to route a mission through the Claude→Codex→Gemma chain instead of plain headless Claude.
- Playwright MCP tool calls block on interactive permission — unavailable inside a headless `/mission` run. Workaround used repeatedly: `npm install playwright` locally next to the HTML being rendered, then `node render.cjs` using `executablePath` pointed at system Chrome.
- State lives in `state.json` (kanban/ratings) — no database. Reports referenced by cards live under `reports/*.md`, served statically, linked as 📄 on the card.

## Owner
No dedicated venture subagent — this project *is* the orchestration layer that drives the venture subagents (`orbean-web`, `aurumguard-firmware`, `olog-cv`, `hostel-booth`, `oorulogix-analyst`). Treat `/mission` (skill) and `mission-runner.ps1` as the source of truth for how work flows through here.
