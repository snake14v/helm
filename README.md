<h1 align="center">🔲 GlassPanel</h1>

<p align="center"><b>A Claude-focused AI workflow control plane you run on your own machine.</b></p>

<p align="center">
One local app to drive a whole fleet of AI agents — <b>Claude Code first</b>, then Codex, cloud free-tiers,
and local uncensored models — from a single board. It <b>routes each task to the cheapest capable model</b>
(so Claude isn't the default), <b>relays work between providers</b> with your code + context, shows every
subsystem behind a <b>factory glass panel</b>, <b>heals and reverts itself</b>, and <b>updates itself from git</b>.
</p>

<p align="center">
<img src="https://img.shields.io/badge/status-active-3fb950"> <img src="https://img.shields.io/badge/python-stdlib_only-1f6feb"> <img src="https://img.shields.io/badge/deps-none-a371f7"> <img src="https://img.shields.io/badge/telemetry-zero-8b949e"> <img src="https://img.shields.io/badge/license-MIT-e3b341">
</p>

<p align="center"><em>No framework · no build step · no database · no telemetry · one <code>python server.py</code>.</em><br>
<em>An open-source project by <a href="https://www.oorulogix.com">Ooru Logix</a>.</em></p>

---

## Why it exists

You already run AI agents all day. What you *don't* have is one place that **sees them, routes them, and keeps
them running** — without burning your best (most expensive) model on every trivial task, and without a bad
automated change quietly bricking your tooling. GlassPanel is that place. It's **Claude-first** (Claude Code is
the primary executor), but it treats your whole model fleet as one adaptive system.

The name comes from the **Machine Room**: like the little sight-glasses on a factory machine that show oil level
and whether the gears are turning, GlassPanel gives every subsystem its own panel — so you can glance and see
what's *spinning*.

## What's inside

| | |
|---|---|
| 🏭 **Machine Room** | A glass panel per subsystem (server, live-push, operator, runner, Ollama, providers, services, budget, guardian). Spinning gears = working; oil-level gauges = token budget & provider health. |
| 🧠 **AI Operator** | A **local, uncensored** model (Ollama) drives GlassPanel unattended — observes state, decides, acts — bounded by a fixed action vocabulary, an audit log, and a one-click kill switch. |
| 🔀 **Model Router** | Classifies each task locally and sends it to the **cheapest capable** provider — never defaults to Claude. Chains outputs provider→provider carrying your code-wiki + git state + a "what's done / what's left" ledger. |
| 🔗 **Bridge** | Indexes every Claude Code/Desktop session on the machine, extracts what each one actually *did* (intents, files, tools), and **transfers workflows into GlassPanel — and back out** for a session to continue. |
| 🛟 **Guardian** | An **external** watchdog (imports zero app code, keeps cold snapshots *outside* the repo). Detects a bricked build and does a **full revert** — even the revert is reversible. The safety net that lets automation touch its own code. |
| ⚡ **Live push** | Server-Sent Events replace polling: the whole UI runs on **one connection** and updates the instant anything changes — no manual refresh. |
| ♻️ **Failover chain** | Claude → Codex → cloud free-tier → local Gemma. When your Claude weekly limit is hit, work keeps flowing. |
| 📊 **Adaptive ranking** | Every model call is scored (success + latency); routing and failover order **re-rank themselves over time**. |
| 🎯 **Mission board + runner** | A kanban of missions an unattended runner executes headlessly, with a human approval gate. |
| ⬆️ **Self-update** | One button pulls the latest from GitHub and restarts. |

## Integrations (all optional, all local-first)

- **Agents:** Claude Code (primary), OpenAI Codex, Google Antigravity
- **Cloud free-tiers:** OpenRouter, Groq, Cerebras, Google AI Studio (Gemini), Moonshot / Kimi, Mistral, SambaNova — add a key, it appears
- **Local models via Ollama:** any model, including **abliterated / uncensored** GGUFs (one-click pull from a curated list + live HuggingFace search)
- **Services:** n8n (`:5678`), crawl4ai (`:11235`), Docker health
- Everything speaks the same OpenAI-compatible surface, so a new provider is one dict entry.

## Quick start

```bash
git clone https://github.com/snake14v/helm.git glasspanel
cd glasspanel
cp config.example.json config.json        # edit with your project folders
python server.py                          # → http://localhost:8799
```

**As a native desktop window** (no browser tab; uses Windows' built-in WebView2):

```bash
pip install pywebview
python helm_app.py
```

**Arm the safety net** (recommended before enabling any automation):

```text
GUARDIAN.bat        external watchdog: snapshots + auto-revert on a bricked build
REVERT-HELM.bat     panic button: full-restore to the last good snapshot
```

## Design

- **Stdlib Python only.** `http.server.ThreadingHTTPServer` + a single `index.html`. No framework, no build, no DB — state is plain JSON files with atomic writes and a lock.
- **One dispatcher.** Every state change flows through a single whitelisted action vocabulary (`agent_act`) — so the UI, the autonomous operator, and any external agent share one audited code path, with **no raw-shell escape hatch**.
- **Loopback-only + guarded.** Binds `127.0.0.1`, and rejects any request whose `Host` isn't a loopback authority or whose `Origin` is foreign — so a malicious page in your browser (or a DNS-rebind) cannot drive it. Proven by `test_security.py`.
- **Honest by default.** No fabricated metrics; every published number carries its provenance; automation is bounded, audited, and reversible.

## Safety model

Automation here can be genuinely autonomous, so the guardrails are structural, not vibes:

1. **Vocabulary, not a shell** — agents invoke only known actions, never arbitrary commands.
2. **Audited** — every action is logged and shown in the UI.
3. **Killable** — a kill switch stops the operator in ≤2s.
4. **Reversible** — the external Guardian snapshots before risky changes and can full-revert a bricked build.

## Honest limitations (read before trusting it)

- **Windows-only today.** Paths, the RAM guard, the process tools, and the launchers use Windows APIs. A Linux/macOS port is not done.
- **The failover chain's Codex leg runs `codex` at `--dangerously-bypass-approvals-and-sandbox` (approval: never).** When a mission fails over to Codex, that tier executes with full local access and no per-action prompt — the human gate is the *mission approval*, not each command. Only enable failover on work you'd let an agent run unattended.
- **Single-user.** No auth, no accounts, no RBAC. It assumes one trusted operator on one machine.
- **The autonomous operator is only as good as its local model.** A small model makes weak calls; that's why every action is bounded, audited, and reversible rather than trusted.

## Configuration

`config.json` (git-ignored, per-machine) holds your ventures/projects `{id, name, color, dir, what}`. Provider API
keys live in your user environment, never in the repo. `config.example.json` ships as the template.

## Keywords

`Claude Code control plane` · `AI agent orchestration` · `multi-model router` · `LLM failover` · `local uncensored models`
· `Ollama dashboard` · `AI workflow automation` · `self-healing agent tooling` · `human-in-the-loop AI`

---

<p align="center"><sub>Local-first · single-user · MIT · built by <a href="https://www.oorulogix.com">Ooru Logix</a>, Bengaluru. · Repo/codename: <code>helm</code>.</sub></p>
