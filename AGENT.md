# Operating HELM with an AI / headless LLM

HELM is fully driveable by an LLM over plain HTTP on `http://127.0.0.1:8799` — no SDK, no auth
(it binds to localhost only). Any headless agent (a `claude -p` session, `codex exec`, a cron
script, or HELM's own built-in operator loop) uses the **same** three-verb surface a human uses
in the UI: **sense → decide → act**.

There are two ways to run it:

1. **Built-in autonomous operator** — a local, uncensored model (Ollama) drives HELM on full auto,
   unattended. Toggle it from the **🧠 AI Operator** tab, or via the `/api/operator/*` endpoints.
2. **Your own external agent** — any LLM you drive calls `/api/agent/*` in a loop.

Both share one dispatcher (`agent_act` in `server.py`) and one action vocabulary (`aiop.MANIFEST`).

---

## The three verbs

### 1. SENSE — `GET /api/agent/observe`
Compact, decision-relevant world state (deliberately cheap — no git/usage scans):
```json
{ "ventures":[{"id":"orbean","what":"POS + storefront"}, ...],
  "counts":{"backlog":2,"inflight":1,"stuck":0,"done":14},
  "backlog":[{"id":"1783...","text":"...","v":"orbean","col":"backlog","status":"..."}],
  "inflight":[...], "stuck":[...],
  "runner":{"state":"polling","detail":"healthy - last heartbeat 12s ago"},
  "executor":"failover", "mode":"auto", "autoFailover":true,
  "cloudProviders":["openrouter","groq","cerebras"], "claudeReady":true,
  "taskHealth":{...}, "hour":16, "ts":1783... }
```
No secrets are ever in the snapshot (`claudeReady` is a bool, not the token).

### 2. DISCOVER — `GET /api/agent/manifest`
The full action vocabulary with params + descriptions. This is the agent's entire capability set —
it can invoke **only** these; there is no raw-shell escape hatch.

### 3. ACT — `POST /api/agent/act`
```json
{ "action": "queue_mission",
  "params": { "text": "Ship the ORBEAN receipt-printing fix and document it", "venture": "orbean" },
  "actor": "my-external-agent" }
```
Returns `{ "ok": true, "id": "...", "msg": "..." }`. Unknown actions are rejected. Every call is
appended to the audit log.

### AUDIT — `GET /api/agent/audit`
The last ~80 actions (newest first): what was done, why, ok/fail, which model. Also rendered live in
the AI Operator tab.

---

## Action vocabulary (as of writing — always re-read `/api/agent/manifest` at runtime)

| action | params | effect |
|---|---|---|
| `queue_mission` | `text`, `venture?` | add a mission to Backlog for the runner to execute — the main lever |
| `mission_act` | `id`, `act`=check\|boost\|rethink\|broken | act on an in-flight mission |
| `heal_mission` | `id` | diagnose a stuck mission + auto-fix (e.g. restart a hung runner) |
| `move_task` | `id`, `col`=backlog\|doing\|review\|done | move a card |
| `delete_task` | `id` | remove a card |
| `task_settings` | `id`, `settings`{mode,executor,promptAddon,maxAttempts} | per-task control |
| `set_mode` | `mode`=auto\|manual | master autopilot (runner + auto-failover + auto-approve) |
| `set_executor` | `executor`=claude\|failover | how missions execute |
| `set_failover` | `auto`=true\|false | fully-automatic provider failover |
| `runner` | `action`=start\|stop\|restart | control the mission-runner engine |
| `pin_nba` | `op`=toggle\|up\|down\|clear, `key` | reprioritize Next-Best-Action |
| `note` | `text` | record a thought/plan (no side effect; shows in the audit feed) |

`id` values come from the `backlog`/`inflight`/`stuck` arrays in `/api/agent/observe` — copy them
verbatim.

---

## The built-in operator loop — `/api/operator/*`

| endpoint | method | effect |
|---|---|---|
| `/api/operator/status` | GET | running? model? interval? |
| `/api/operator/start` | POST | **engage** — full auto |
| `/api/operator/stop` | POST | **kill switch** — stops within ≤2s |
| `/api/operator/tick` | POST | think once now (one observe→decide→act cycle) |
| `/api/operator/config` | POST | `{model, intervalSec, maxActionsPerTick, allowCloud}` |

The brain is chosen by `operator_model()`, and it is NOT a fixed list: the candidate pool is
whatever's actually pulled in Ollama right now (preferring abliterated/uncensored tags —
`abliterat`/`heretic`/`uncensored`/`dolphin`/`hermes` — when any are pulled), and the pool is
ranked by **`model_rank.py`** — a live, empirical track record (success rate + latency, recency-
weighted) recorded after every real call. A model with no history yet is tried on equal footing
(broken by parameter-count as a rough guess), so a newly-pulled model gets a fair shot; one that
starts failing (OOM, uninstalled, degraded) naturally sinks without any code change. See the
scores live at `GET /api/models/rank`, or the **🧠 AI Operator** tab's "Model ranking" table.

The same ranking replaces the old static cloud-provider priority order: `llm_providers.
call_first_available()` (used by the failover chain and by the operator's cloud fallback) now
tries providers best-track-record-first instead of a hardcoded order, adapting as providers get
rate-limited, go paid, or improve.

The **🔓 Abliterated** tab also live-checks HuggingFace (cached 6h) for newer abliterated models
not yet in the curated catalog or pulled locally, surfacing them under "Discovered since" — so the
local fleet can improve over time instead of staying frozen to whatever was known at authoring
time. Pulling one is still a manual click, never automatic.

If Ollama is down and `allowCloud` is on, decisions fall back to the cloud free-tier chain
(advisory) — note that this sends the world snapshot (mission texts, venture ids) to an external
provider, so keep sensitive mission wording out of the box if that matters to you.

---

## Example: a minimal external operator loop (bash)

```bash
while true; do
  STATE=$(curl -s localhost:8799/api/agent/observe)
  # ... hand STATE + the manifest to your LLM, get back {action, params} ...
  curl -s -X POST localhost:8799/api/agent/act \
       -H 'Content-Type: application/json' \
       -d '{"action":"queue_mission","params":{"text":"...","venture":"orbean"},"actor":"my-agent"}'
  sleep 90
done
```

## Example: drive it from a headless Claude session
`claude -p` can operate HELM by calling these endpoints with Bash/curl. Give it this file, then:
> "Read http://127.0.0.1:8799/api/agent/observe and /api/agent/manifest, then POST the best
>  actions to /api/agent/act to keep every venture moving. Loop every few minutes."

---

## Safety model (why it's safe to leave running)

Full autonomy over HELM's own board is **intended**. Three properties bound it:
- **Vocabulary, not shell** — an agent can invoke only the manifest actions; there is no path to a
  raw shell, arbitrary argv, `eval`, or arbitrary filesystem path. A queued mission's text is
  sanitized by `mission-runner.ps1` before it ever becomes a `claude -p` argv.
- **Audited** — every action is written to `operator-audit.jsonl` and shown in the UI.
- **Killable** — the kill switch (`/api/operator/stop`, or the UI button) stops the loop in ≤2s.

Blast radius is confined to HELM's own `state.json` / `runtime.json` / runner — there is no action
that deletes or corrupts files, repos, or data outside HELM.
