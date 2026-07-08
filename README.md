# ⎈ HELM — Local Mission Control for AI Coding Agents

> **One dashboard to run, watch, and govern a fleet of AI coding agents** — Claude Code, OpenAI Codex, Google Antigravity, Kimi, and local Ollama models — on your own machine. Kanban board, live token budgets, human-in-the-loop approvals, stuck-task detection, and an unattended mission runner. **Zero cloud. Zero telemetry. One `python server.py`.**

<p align="center"><em>An open-source project by <a href="https://oorulogix.com">Oorulogix</a> — practical AI systems for founders and small teams.</em></p>

![status](https://img.shields.io/badge/status-active-3fb950) ![python](https://img.shields.io/badge/python-stdlib_only-1f6feb) ![license](https://img.shields.io/badge/license-MIT-a371f7) ![platform](https://img.shields.io/badge/platform-Windows-8b949e)

---

## Why HELM?

If you run **multiple AI coding agents** you already have the problem HELM solves: work scattered across terminals, no idea what's running, tokens burning invisibly, background agents acting without asking, and tasks that silently stall. HELM is the **single pane of glass** that fixes all of it — and it's a ~30 MB Python-stdlib server with a single HTML dashboard, so there's nothing to deploy and nothing phones home.

## Features

- **🗂 Agent kanban** — a real board where AI agents move their own cards; colour-coded by venture and by status (queued / in-progress / review / needs-you / blocked / done).
- **⏸ Human-in-the-loop approvals** — background agents *pause and wait for your click* before running. Approve-all and auto-approve toggle included.
- **💰 Live token governor** — real token burn parsed from your local transcripts, daily/weekly budgets, pace projection, and emergency token-saving levers (optical context compression, local-model offload).
- **⚠ Stuck-task detection** — finds missions that silently stalled, explains *why* in plain English, and offers one-click Retry / Done / Remove.
- **🤖 Multi-provider connector** — one OpenAI-compatible caller for **Kimi K2, DeepSeek, Qwen, Groq, Cerebras, Gemini** plus local **Ollama**. Legitimate free tiers, no multi-accounting.
- **🩺 Self-diagnostics** — a built-in Doctor that health-checks the whole stack and applies safe fixes.
- **📜 Full activity logs** — every approval, every mission, every switch-over, with stats and explanations.
- **📂 Click-through** — click any task to see exactly where the work lives on disk and open that folder.

## Quick start

```bash
git clone https://github.com/<your-org>/helm-mission-control
cd helm-mission-control
SETUP.bat          # prompts for optional API keys, then launches
# or just:
python server.py   # -> http://localhost:8799
```

No dependencies — HELM uses only the Python standard library.

## Keywords

`AI agent orchestration` · `Claude Code dashboard` · `multi-agent kanban` · `LLM token budget tracker` · `Codex Kimi Ollama` · `human-in-the-loop AI` · `local agent mission control` · `AI coding workflow`

## License

MIT — see [LICENSE](LICENSE). Built by [Oorulogix](https://oorulogix.com).
