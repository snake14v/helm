# models.py - catalog of open/free models Vaishak can add, and how to reach them LEGITIMATELY.
# Philosophy: one account per provider (their real free tier) + a single multi-model gateway
# (OpenRouter) + unlimited local (Ollama). This beats multi-accounting and can't get you banned.
# "available" is computed from env-var keys actually present on this machine - never faked.
import os, json, urllib.request

# Curated as of mid-2026. Each: how to reach it, whether it's free, and what key env-var unlocks it.
CATALOG = [
    {"name": "OpenRouter (gateway)", "kind": "gateway", "pid": "openrouter", "envKey": "OPENROUTER_API_KEY",
     "free": "hundreds of models incl. free ones (DeepSeek R1, Qwen, Kimi, GLM, + MiMo `xiaomi/mimo-v2-flash:free`) - ONE key",
     "get": "openrouter.ai/keys (free signup)", "keyUrl": "https://openrouter.ai/keys",
     "reset": "20/min · 50/day (<$10 credit) or 1,000/day (≥$10) · daily resets 00:00 UTC",
     "why": "Best single move: hundreds of models incl. free ones + MiMo behind one key."},
    {"name": "Groq", "kind": "cloud", "pid": "groq", "envKey": "GROQ_API_KEY",
     "free": "generous free tier, extremely fast; hosts Llama, Qwen, Kimi-K2, GPT-OSS",
     "get": "console.groq.com/keys", "keyUrl": "https://console.groq.com/keys",
     "reset": "~30/min · 1k-14.4k/day per model · minute resets 60s, daily rolls 24h",
     "why": "Fastest free inference; great for menial/bulk agent work."},
    {"name": "Moonshot / Kimi", "kind": "cloud", "pid": "moonshot", "envKey": "MOONSHOT_API_KEY",
     "free": "Kimi K2 (open weights) - PAY-AS-YOU-GO, needs a positive balance (not truly free)",
     "get": "platform.moonshot.ai", "keyUrl": "https://platform.moonshot.ai/console/api-keys",
     "reset": "pay-as-you-go - no free reset; add credits to use",
     "why": "Kimi K2 is a top open model, but this account needs balance."},
    {"name": "Cerebras", "kind": "cloud", "pid": "cerebras", "envKey": "CEREBRAS_API_KEY",
     "free": "free tier, very fast; gpt-oss-120b, GLM, Gemma", "get": "cloud.cerebras.ai",
     "keyUrl": "https://cloud.cerebras.ai/", "reset": "per-minute + daily token limits · reset daily",
     "why": "Another free fast-inference option to spread load across."},
    {"name": "Google AI Studio", "kind": "cloud", "pid": "gemini", "envKey": "GEMINI_API_KEY",
     "free": "free Gemini Flash tier (real, one account)", "get": "aistudio.google.com/apikey",
     "keyUrl": "https://aistudio.google.com/apikey", "reset": "per-minute + per-DAY · resets midnight Pacific (00:00 PT)",
     "why": "Legit free Gemini access via API key (separate from the retired free CLI)."},
    {"name": "Mistral", "kind": "cloud", "pid": "mistral", "envKey": "MISTRAL_API_KEY",
     "free": "free Experiment tier (no card): mistral-small, Nemo, Codestral", "get": "console.mistral.ai/api-keys",
     "keyUrl": "https://console.mistral.ai/api-keys", "reset": "per-minute + ~1B tokens/month (monthly reset)",
     "why": "Solid European open-weight models, genuinely free to start."},
    {"name": "SambaNova", "kind": "cloud", "pid": "sambanova", "envKey": "SAMBANOVA_API_KEY",
     "free": "free tier (no card): Llama-3.3-70B, DeepSeek-V3, gpt-oss-120b - very fast", "get": "cloud.sambanova.ai/apis",
     "keyUrl": "https://cloud.sambanova.ai/apis", "reset": "~20/min · 20/day · 200K tokens/day per model · per-min + daily",
     "why": "Blazing RDU inference, persistent free tier, no credit card."},
    # Local (Ollama) - genuinely unlimited, no key, bounded only by the 6GB GPU / RAM.
    {"name": "Ollama: qwen2.5:3b", "kind": "local", "pull": "qwen2.5:3b",
     "free": "unlimited, offline, free", "why": "Beats gemma3:4b at coding/instructions; fits 6GB."},
    {"name": "Ollama: deepseek-r1:1.5b", "kind": "local", "pull": "deepseek-r1:1.5b",
     "free": "unlimited, offline, free", "why": "Tiny reasoning model for quick logic/menial tasks."},
    {"name": "Ollama: llama3.2:3b", "kind": "local", "pull": "llama3.2:3b",
     "free": "unlimited, offline, free", "why": "Solid general small model; good fallback."},
    {"name": "Ollama: MiMo-7B (GGUF)", "kind": "local", "pull": "hf.co/XiaomiMiMo/MiMo-7B-RL-GGUF",
     "free": "unlimited, offline, free (tight on 6GB - use a small quant)", "why": "Xiaomi's open reasoning model, as requested."},
]

def _ollama_models():
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=2) as r:
            return {m.get("name") for m in json.loads(r.read()).get("models", [])}
    except Exception:
        return set()

def catalog():
    local = _ollama_models()
    out = []
    for m in CATALOG:
        e = dict(m)
        if m["kind"] == "local":
            # available = already pulled (match on name prefix before any tag)
            base = m.get("pull", "").split("/")[-1]
            e["available"] = any(x == base or x.split(":")[0] == base.split(":")[0] for x in local)
            e["action"] = f"ollama pull {m['pull']}"
        else:
            key = m.get("envKey", "")
            has = bool(os.environ.get(key)) or _user_env_has(key)
            e["available"] = has
            e["action"] = f"set {key} (get it at {m.get('get','')})"
        out.append(e)
    return {"models": out,
            "note": "More tokens legitimately = OpenRouter (1 key, many free models) + Groq/Cerebras free tiers + unlimited local Ollama. Multi-accounting to dodge limits violates provider ToS and risks bans - not supported."}

def _user_env_has(key):
    if not key:
        return False
    try:
        import subprocess
        # read from registry without spawning cmd (per the wscript-chain quirk lesson): use reg via ctypes-free winreg
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            winreg.QueryValueEx(k, key)
            return True
    except Exception:
        return False
