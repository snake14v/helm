# Plan — add Grok (xAI) + Hermes agentic setup to HELM

**Status:** approved plan, NOT yet built (plan-only). Decisions locked 2026-07-09:
Grok default = **grok-4.5** · Hermes brain = **Hermes-3 8B** (fits the 6GB GPU) · sequencing = Grok first, then Hermes.

**Why:** when the Claude weekly limit is exhausted, HELM must keep running on non-Claude models.
Grok is the capable paid backstop; Hermes is a free, open-source, local agentic brain that also makes the
AI operator far more reliable (native tool-calling vs the freeform-JSON parsing that misfired on gemma3:4b).

Positioning stays honest: Hermes (local, open-weight) reinforces the **free & open-source** pull-point;
Grok is the paid cloud tier that only engages on failover. `model_rank.py` decides which actually wins over time.

---

## Part A — Grok (xAI) as a failover provider  ·  file: `llm_providers.py`  ·  ~15 min

xAI's API is OpenAI-compatible (`https://api.x.ai/v1`), so it drops into the existing `PROVIDERS` pattern.

1. **Add to `PROVIDERS`:**
   ```python
   "xai": {"url": "https://api.x.ai/v1/chat/completions", "env": "XAI_API_KEY",
           "model": "grok-4.5", "label": "xAI Grok (grok-4.5)"},
   ```
2. **Add to `PRIORITY`** — after the free tiers (free tries first; Grok is the strong *paid* backstop):
   ```python
   PRIORITY = ["openrouter", "groq", "cerebras", "xai", "moonshot", "gemini"]
   ```
   (`model_rank.rank()` re-orders this live by real success/latency, so exact position is only the cold start.)

That's the whole wiring. It flows automatically into: `call_first_available()` (failover chain),
`available()`, `test()`, `status_all()`, `/api/models/setkey` (key button), the provider-status UI, and
`model_rank` scoring. The browser User-Agent header already set on `_request()` covers any Cloudflare check.

**Model note:** grok-4.5 = smartest/fastest flagship (pricier). If cost bites, `grok-code-fast-1`
(~$0.20/$1.50 per M) is the cheap swap — just change the `model` field.

**Dependency (user):** add the key — get it at console.x.ai ($25 free on signup + up to $150/mo via the
data-sharing program). Set it in HELM (Analytics → Models → set key) which writes `XAI_API_KEY` to the user
env, or `setx XAI_API_KEY ...`. Never commit it.

---

## Part B — Hermes-3 8B as the local agentic brain  ·  file: `ablit.py` (+ one pull)  ·  ~10 min

`aiop.py:operator_model()` already prefers any pulled tag containing `hermes`, so once Hermes is in Ollama
it auto-becomes the operator brain — no aiop change needed for selection.

1. **Pull it** (simplest path — official Ollama library, no HuggingFace needed):
   ```
   ollama pull hermes3:8b        # ~4.7 GB Q4 — fits the 6GB GPU
   ```
2. **Add to `ablit.py` CATALOG** (so it's one-click pullable from the Abliterated tab + shows as "agentic"):
   ```python
   {"id": "hermes3:8b", "name": "Hermes 3 8B (agentic)", "kind": "text",
    "size": "~4.7 GB @ Q4 - FITS 6GB GPU", "quant": "Q4_K_M",
    "why": "NousResearch agentic model — native <tool_call> function-calling; best local operator brain."},
   ```
   (CATALOG's `pulled()` check already substring-matches, so `hermes3:8b` will show ✓ once pulled.)

---

## Part C — Native tool-calling for the operator (the real reliability upgrade)  ·  `ablit.py` + `aiop.py`  ·  ~1–2 h

Today `aiop.decide()` asks the model for a freeform JSON array and parses it — brittle on small models
(gemma3:4b hallucinated ids/params). Hermes is trained for structured tool-calling, and **Ollama exposes it
natively** via the `tools` parameter on `/api/chat`, returning `message.tool_calls`. Adopt that.

1. **`ablit.py` — new `chat_tools()`** (alongside `chat()`):
   ```python
   def chat_tools(model, messages, tools, timeout=150):
       """Ollama structured tool-calling. Returns {ok, tool_calls:[{name,arguments}], text, ms}."""
       body = json.dumps({"model": model, "messages": messages, "tools": tools, "stream": False}).encode()
       # POST OLLAMA + "/api/chat"; read d["message"].get("tool_calls", []) and d["message"].get("content")
   ```
2. **`aiop.py` — MANIFEST → OpenAI function schemas.** Give each action a real JSON-Schema `parameters`
   block (currently the params are descriptive strings). One `MANIFEST_TOOLS = [{"type":"function",
   "function":{"name","description","parameters"}}, ...]` derived from MANIFEST.
3. **`aiop.py:decide()` — prefer structured tool-calls:**
   - Build a system message (operator role + the `SITUATION`/`EXISTING MISSION IDS` context already assembled
     in `_build_prompt`) + user message ("decide the next actions").
   - Call `ablit_mod.chat_tools(model, messages, MANIFEST_TOOLS)`.
   - Map returned `tool_calls` → `[{action, params, why}]` (arguments are already parsed JSON — no regex).
   - **Fallback:** if `tool_calls` is empty (a non-tool model, or Ollama version without support), fall back
     to the existing `_build_prompt` + `_parse_actions` freeform path. Zero regression for gemma.
   - The dedup guard (`_mission_dupe`), audit log, kill switch, and vocabulary whitelist all stay exactly as-is
     — this only changes *how the model's intent is captured*, not what actions are allowed.
4. **Grok/cloud path:** `call_first_available` can carry the same `tools` (xAI + most OpenAI-compatible
   providers support function-calling) — optional second step; local Hermes is the primary target.

**Net effect:** the operator emits `queue_mission({"text": "...", "venture": "orbean"})` as a real function
call Hermes was trained to produce, instead of guessing JSON — killing the wrong-param / hallucinated-id class
of failures that caused the duplicate-mission incident.

---

## Verification (when built)

1. `python -c "import ast; [ast.parse(open(f,encoding='utf-8').read()) for f in ('llm_providers.py','ablit.py','aiop.py')]"`
2. Restart server; `POST /api/models/setkey {provider:"xai", key:...}` → expect `test.ok = true` (needs key).
3. `ollama pull hermes3:8b`; `GET /api/operator/status` → `activeModel` should be `hermes3:8b`.
4. `POST /api/operator/tick` → confirm actions come back via tool_calls with correct params; check `operator-audit.jsonl`.
5. Browser: AI Operator tab → Model ranking shows `hermes3:8b` (local) + `xai` (cloud) accruing scores.
6. Failover smoke test: with Claude "capped", `agent-failover.ps1 -SkipClaude` should reach the cloud tier and,
   if free tiers are exhausted, land on Grok.

## Guardrails (unchanged, still hold)
- Secrets (`XAI_API_KEY`) never committed — user env only; already covered by `.gitignore` `.env*` + the
  winreg-based `setkey`.
- Operator autonomy still bounded by the MANIFEST vocabulary + audit + kill switch (Part C changes capture,
  not authority).
- `model-scores.json` already gitignored; no new secret-bearing files.

## Effort / sequencing
Part A (Grok) ≈ 15 min, ships value immediately (failover when Claude capped). Part B ≈ 10 min + pull time.
Part C ≈ 1–2 h, the quality upgrade. Recommended order: A → B → C.
