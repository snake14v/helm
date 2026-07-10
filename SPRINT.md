# HELM sprint — native window + live push (the lazy version)

Two audits ran: a full architecture audit (16-day framework plan) and a Ponytail anti-over-engineering
audit. **Ponytail won.** The felt wins are SSE (no lag/refresh) + an app window; everything else
(router rewrite, event bus, plugin registry, "integration hub", native rewrite) is a framework with one
author and one process — YAGNI. This sprint ships **net-negative lines**.

Verdict source: `agent_act` is already the command bus; a dict is the integration hub; pywebview is the exe.

## Ship list (in order)

1. **App window** — `helm_app.py` (pywebview): starts `server.py` if the port is down, opens HELM in a
   real chromeless window (WebView2, already on Win11 — escapes the Chrome tab without bundling Chromium).
   Pure addition, touches no existing file. ~15 lines, 1 dep. *(PyInstaller → single-file .exe only if a
   distributable is explicitly wanted; that's packaging, not architecture.)*

2. **Stdlib SSE** — `GET /api/events`: a global `VERSION` int bumped inside the existing locked write
   chokepoints (`save_state`, `_set_runtime`, `aiop._write_audit`); the SSE handler emits a `tick` on
   change; frontend `EventSource` re-runs the existing `pollX`/`renderX` on tick. Delete the poll
   intervals it obsoletes; keep ONE 30s poll as reconnect fallback. Kills lag (130 req/min → 1 conn) and
   "refresh required" (stale tabs get pushed to). ~40 lines server, ~10 client, 0 deps, net-negative.

3. **Unify mutations → `agent_act`** — the 7 duplicated POST handlers (`/api/mission/act`, `/runner`,
   `/executor`, `/mode`, `/task/settings`, `/nba/pins`, `/failover`) become
   `return self._json(agent_act(<name>, b, actor="human"))`. Move the cosmetic `msg` + done-report guard
   into `agent_act`. −180 lines; one command path; every mutation gets the audit + SSE tick for free.

4. **Warm-up helpers** (pay the line budget): `_body(self)` (kills 28 copies of the Content-Length/JSON
   parse), `_q(self,k)` (6 copies of parse_qs), one `makeModal(id)` + `.fld` class in index.html.

## Correctness carve-outs (Ponytail scoped these out; keep them)
- **`runtime.json` lock bypass** — `apihealth._set_model_override` + `llm_providers._model_override` write
  the file raw, bypassing `aiop.RT_LOCK` → lost updates. Route both through `aiop.read_runtime/write_runtime`.
- **Auth gap (flag, don't necessarily gate)** — every `/api/*` is unauthenticated on 127.0.0.1;
  `/api/launch` spawns PowerShell, `/api/models/setkey` writes the registry. Fine for a personal machine,
  but a malicious page hitting `localhost:8799` could drive it. Optional hardening: a loopback token in
  runtime.json + an `X-HELM-Token` middleware check. Deferred unless HELM opens to other machines.

## Explicitly NOT doing (and why)
- Router table / middleware framework — 90 branches → a dict is a mechanical −35, but a full framework is
  YAGNI for one author.
- Event bus, plugin registry, `integrations/` discovery — `INTEGRATIONS = {"n8n": call_n8n}` dict instead.
- SQLite for state.json — whole-file atomic replace is fine at solo write volume; revisit only if lock
  contention is *measured*.
- Tauri / Electron / PySide rewrite — throws away a working page to re-solve a solved problem.

## Reversibility
Every step is additive or behind a flag. `MISSION-CONTROL.bat` browser mode stays untouched; the window is
a *second* client. SSE keeps the old pollers behind `USE_SSE=false` for one release. Nothing is deleted
until its replacement is verified rendered.
