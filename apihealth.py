# apihealth.py - diagnose & self-heal provider API/login errors, with an agentic LLM fixer.
# Layered: (1) deterministic diagnose() classifies the error + gives a fix plan; (2) autofix()
# runs real queries to fix what's mechanically fixable (discover a working model); (3) aifix()
# asks a WORKING free provider (Groq/OpenRouter/etc.) to troubleshoot the broken one in English.
import json
import llm_providers as L

# Where to get/refresh each provider's key (verified from live docs, 2026-07-08).
KEY_URLS = {
    "openrouter": "https://openrouter.ai/keys",
    "groq":       "https://console.groq.com/keys",
    "cerebras":   "https://cloud.cerebras.ai/",
    "gemini":     "https://aistudio.google.com/apikey",
    "moonshot":   "https://platform.moonshot.ai/console/api-keys",
    "mistral":    "https://console.mistral.ai/api-keys",
    "sambanova":  "https://cloud.sambanova.ai/apis",
}
# Free-tier limits + when they reset (verified from live docs, 2026-07-08).
RESET_WINDOWS = {
    "openrouter": "20 req/min · 50/day (<$10 lifetime credit) or 1,000/day (≥$10). Daily resets 00:00 UTC.",
    "groq":       "per-model: ~30 req/min · 1,000–14,400 req/day. Minute resets every 60s; daily rolls 24h.",
    "cerebras":   "per-minute + daily token limits on the free tier; reset daily.",
    "gemini":     "free tier: per-minute + per-DAY limits, reset at midnight Pacific (00:00 PT).",
    "moonshot":   "pay-as-you-go — NOT a free tier; needs a positive balance (no reset, add credits).",
    "mistral":    "free Experiment tier: per-minute + ~1B tokens/month cap (monthly reset).",
    "sambanova":  "free tier: ~20 req/min · 20 req/day · 200K tokens/day per model. Per-minute + daily reset.",
}
# Free providers to suggest switching to when one is down.
FREE_ALTERNATIVES = ["openrouter", "groq", "cerebras", "sambanova", "mistral"]


def diagnose(provider, res):
    """Classify a test() result into {cat, severity, title, human, autoFixable, actions, keyUrl, resetHint}."""
    code = res.get("code")
    err = (res.get("error") or "").lower()
    keyurl = KEY_URLS.get(provider, "")
    reset = RESET_WINDOWS.get(provider, "")

    def d(cat, sev, title, human, auto, actions, **extra):
        return {"cat": cat, "severity": sev, "title": title, "human": human,
                "autoFixable": auto, "actions": actions, "keyUrl": keyurl, "resetHint": reset, **extra}

    if res.get("ok"):
        return d("ok", "ok", "Working", "Connected and responding.", False, [])
    if not res.get("connected"):
        return d("no_key", "warn", "No key yet",
                 "You haven't added an API key for this provider.", False, ["get_key", "add_key"])
    if "1010" in err or "cloudflare" in err:
        return d("cloudflare", "warn", "Cloudflare blocked the client",
                 "The provider's Cloudflare blocked the request signature (error 1010). HELM now sends a "
                 "browser User-Agent that fixes this — just retry.", True, ["autofix", "test"])
    if code == 404 and "model" in err:
        return d("wrong_model", "warn", "Configured model unavailable",
                 "Your key is VALID, but the model HELM used isn't available to your account. "
                 "Auto-fix will query the provider's model list and pick one that works.", True, ["autofix", "aifix"])
    if any(x in err for x in ("balance", "insufficient", "recharge", "suspend")):
        return d("no_credits", "warn", "Out of paid credits",
                 "Your key is VALID, but this account has no balance — this provider isn't free. "
                 "Top up, or just use the free ones (OpenRouter / Groq / Cerebras / SambaNova).",
                 False, ["switch", "get_key"])
    if code == 429:
        return d("quota", "warn", "Free quota used up (it resets)",
                 "Your key is VALID — you've hit the free-tier quota for now. It comes back on the "
                 "provider's schedule (see reset window). Meanwhile, switch to another working provider.",
                 True, ["autofix", "switch"])
    if code in (401, 403):
        return d("bad_key", "high", "Key invalid or expired",
                 "The provider rejected the key (auth failed). Get a fresh key and re-enter it.",
                 False, ["get_key", "add_key", "aifix"])
    if code == 0:
        return d("network", "warn", "Network / endpoint error",
                 "Couldn't reach the provider (timeout or DNS). Check your connection, then retry.",
                 True, ["test", "aifix"])
    return d("unknown", "warn", f"Unexpected error (HTTP {code})",
             "An error HELM doesn't have a rule for. Try the AI troubleshooter.", True, ["aifix", "test"])


def _set_model_override(provider, model):
    try:
        d = json.loads(open(L._RUNTIME, encoding="utf-8").read())
    except Exception:
        d = {}
    d.setdefault("modelOverrides", {})[provider] = model
    open(L._RUNTIME, "w", encoding="utf-8").write(json.dumps(d, indent=1))


def autofix(provider):
    """Run real queries to fix what's mechanical: rediscover a working model + retest."""
    p = L.PROVIDERS.get(provider)
    if not p or not L._key(p["env"]):
        return {"ok": False, "error": "no key / unknown provider"}
    r = L.test(provider)
    if r.get("ok"):
        return {"ok": True, "fixed": False, "msg": "already working — nothing to fix", "test": r}
    tried = []
    for m in (L.list_models(provider) or [])[:6]:
        rr = L.call(provider, "Reply with exactly: OK", model=m, max_tokens=8, timeout=25)
        tried.append(m)
        if rr.get("ok"):
            _set_model_override(provider, m)
            return {"ok": True, "fixed": True, "model": m,
                    "msg": f"found a working model and locked it in: {m}", "test": L.test(provider)}
    return {"ok": False, "fixed": False, "tried": tried,
            "error": "no model responded — likely rate-limited / out of credits, not a fixable config issue"}


def aifix(provider):
    """Ask a WORKING free provider to troubleshoot the broken one (the 'LLM fixes API errors' path)."""
    res = L.test(provider)
    err = res.get("error") or ("connected but empty" if res.get("ok") else "unknown error")
    helper = next((c for c in ("groq", "openrouter", "cerebras", "sambanova", "mistral")
                   if c != provider and L.test(c).get("ok")), None)
    if not helper:
        return {"ok": False, "error": "no working free provider available to run the AI fixer — "
                "add an OpenRouter or Groq key first (those are free)."}
    prompt = (f"You are an API-troubleshooting assistant. A developer's call to the '{provider}' LLM API "
              f"failed with:\n\n{err}\n\nReset/limits context: {RESET_WINDOWS.get(provider, 'unknown')}\n\n"
              "In 3-5 tight bullets: the most likely CAUSE, the exact FIX steps, and whether it's the "
              "user's key/account/billing or a config (model/endpoint) issue. Be concrete and brief.")
    r = L.call(helper, prompt, max_tokens=400, timeout=45)
    return {"ok": r.get("ok"), "helper": helper, "advice": (r.get("text") or "").strip(),
            "error": r.get("error"), "diagnosis": diagnose(provider, res)}
