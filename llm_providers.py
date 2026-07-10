# llm_providers.py - unified caller + connection tester for open/free models.
# All these providers speak the SAME OpenAI-compatible /chat/completions API, so ONE function
# reaches all of them. Keys are read from the live user environment (registry, so a fresh setx
# is picked up without restarting the server). Nothing here needs Vaishak's key to EXIST - it
# reports honestly whether each is connected, and `call()` works the instant a key is set.
import json, os, time, urllib.request, urllib.error
import model_rank

# A browser-ish User-Agent: some providers (Groq) sit behind Cloudflare and 403 the default
# "Python-urllib/x.y" signature (error 1010). Sending a normal UA fixes it. (Verified 2026-07-08.)
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) HELM/1.0"

# provider -> OpenAI-compatible endpoint, env var, and a currently-working free/cheap model.
# (models_url lets us auto-discover a live free model when the hardcoded one goes paid.)
PROVIDERS = {
    "openrouter": {"url": "https://openrouter.ai/api/v1/chat/completions", "env": "OPENROUTER_API_KEY",
                   "model": "meta-llama/llama-3.3-70b-instruct:free", "label": "OpenRouter (free models)",
                   "models_url": "https://openrouter.ai/api/v1/models"},
    "groq":       {"url": "https://api.groq.com/openai/v1/chat/completions", "env": "GROQ_API_KEY",
                   "model": "llama-3.1-8b-instant", "label": "Groq (Llama 3.1, fast free)"},
    "moonshot":   {"url": "https://api.moonshot.ai/v1/chat/completions", "env": "MOONSHOT_API_KEY",
                   "model": "kimi-k2-0711-preview", "label": "Moonshot / Kimi (needs paid credits)"},
    "cerebras":   {"url": "https://api.cerebras.ai/v1/chat/completions", "env": "CEREBRAS_API_KEY",
                   "model": "gpt-oss-120b", "label": "Cerebras (free tier, very fast)"},
    "gemini":     {"url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", "env": "GEMINI_API_KEY",
                   "model": "gemini-2.0-flash", "label": "Google AI Studio (Gemini Flash)"},
    "mistral":    {"url": "https://api.mistral.ai/v1/chat/completions", "env": "MISTRAL_API_KEY",
                   "model": "mistral-small-latest", "label": "Mistral (free Experiment tier)"},
    "sambanova":  {"url": "https://api.sambanova.ai/v1/chat/completions", "env": "SAMBANOVA_API_KEY",
                   "model": "Meta-Llama-3.3-70B-Instruct", "label": "SambaNova (free, very fast)"},
}

_OR_FREE = {"list": None}  # cache the discovered OpenRouter free-model list
def _openrouter_free_models(key):
    """Ask OpenRouter which models are FREE right now (ordered, preferred first), so we never
    hardcode a slug that went paid AND can fall through when one is rate-limited (429)."""
    if _OR_FREE["list"]:
        return _OR_FREE["list"]
    out = ["meta-llama/llama-3.3-70b-instruct:free"]
    try:
        req = urllib.request.Request("https://openrouter.ai/api/v1/models",
                                     headers={"Authorization": f"Bearer {key}", "User-Agent": _UA})
        d = json.loads(urllib.request.urlopen(req, timeout=20).read())
        free = [m["id"] for m in d.get("data", []) if str((m.get("pricing") or {}).get("prompt")) == "0"]
        pref = ("meta-llama/llama-3.3-70b-instruct:free", "qwen/qwen3-next-80b-a3b-instruct:free",
                "deepseek/deepseek-r1:free", "qwen/qwen3-coder:free", "google/gemini-2.0-flash-exp:free")
        out = [m for m in pref if m in free] + [m for m in free if m not in pref]
        if out:
            _OR_FREE["list"] = out
    except Exception:
        pass
    return out or ["meta-llama/llama-3.3-70b-instruct:free"]

def _request(p, key, use_model, prompt, timeout, max_tokens):
    body = json.dumps({"model": use_model,
                       "messages": [{"role": "user", "content": prompt}],
                       "max_tokens": max_tokens}).encode()
    req = urllib.request.Request(p["url"], data=body, method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json",
        "User-Agent": _UA,   # fixes Groq's Cloudflare 403 (error 1010)
        "HTTP-Referer": "http://localhost:8799", "X-Title": "Mission Control"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
        txt = (d.get("choices") or [{}])[0].get("message", {}).get("content", "")
        return {"ok": True, "text": txt, "model": use_model, "ms": int((time.time() - t0) * 1000)}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        return {"ok": False, "error": f"HTTP {e.code}: {detail}", "code": e.code, "model": use_model}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "code": 0}

def _key(env):
    v = os.environ.get(env)
    if v:
        return v
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            val, _ = winreg.QueryValueEx(k, env)
            return val or None
    except Exception:
        return None

_RUNTIME = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runtime.json")
def _model_override(provider):
    """A working model the auto-fixer discovered for this provider (runtime.json modelOverrides).
    Reads via aiop's locked reader so it never sees a half-written file (lazy import avoids a cycle)."""
    try:
        import aiop
        return (aiop.read_runtime().get("modelOverrides") or {}).get(provider)
    except Exception:
        return None

def list_models(provider):
    """GET the provider's /models catalog (derived from its chat URL). Empty on any failure."""
    p = PROVIDERS.get(provider)
    key = _key(p["env"]) if p else None
    if not p or not key:
        return []
    url = p["url"].replace("/chat/completions", "/models")
    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}", "User-Agent": _UA})
        d = json.loads(urllib.request.urlopen(req, timeout=20).read())
        return [m.get("id") for m in d.get("data", []) if m.get("id")]
    except Exception:
        return []

def call(provider, prompt, model=None, timeout=60, max_tokens=1024):
    p = PROVIDERS.get(provider)
    if not p:
        return {"ok": False, "error": f"unknown provider '{provider}'"}
    key = _key(p["env"])
    if not key:
        return {"ok": False, "error": f"no key set ({p['env']}) - run connect-model.ps1 {provider}"}
    eff = model or _model_override(provider)   # a working model the auto-fixer discovered
    if eff:
        r = _request(p, key, eff, prompt, timeout, max_tokens)
        model_rank.record(provider, r.get("ok"), r.get("ms"), kind="cloud")
        return r
    # OpenRouter: free models rate-limit hard (429) or go paid (404). Try several free ones in
    # order; return the first success, or stop early on a definitive auth error (401/403).
    if provider == "openrouter":
        r = None
        for mid in _openrouter_free_models(key)[:4]:
            r = _request(p, key, mid, prompt, timeout, max_tokens)
            if r["ok"] or r.get("code") in (401, 403):
                model_rank.record(provider, r.get("ok"), r.get("ms"), kind="cloud")
                return r
        model_rank.record(provider, False, None, kind="cloud")
        return r or {"ok": False, "error": "no free OpenRouter model responded (all rate-limited?)"}
    r = _request(p, key, p["model"], prompt, timeout, max_tokens)
    model_rank.record(provider, r.get("ok"), r.get("ms"), kind="cloud")
    return r

def test(provider):
    """Tiny real call to prove the connection works end-to-end."""
    p = PROVIDERS.get(provider)
    if not p:
        return {"provider": provider, "ok": False, "error": "unknown provider"}
    if not _key(p["env"]):
        return {"provider": provider, "label": p["label"], "ok": False, "connected": False,
                "error": f"no key ({p['env']}) yet"}
    r = call(provider, "Reply with exactly: OK", max_tokens=8, timeout=30)
    # A 429 means the KEY IS VALID; distinguish a transient free-tier rate-limit from an
    # account that's out of paid credits (Moonshot etc.) so the message isn't misleading.
    err = (r.get("error") or "").lower()
    needs_credits = any(x in err for x in ("balance", "insufficient", "suspend", "recharge"))  # actual money words
    ratelimited = (not r["ok"]) and r.get("code") == 429 and not needs_credits  # quota/rate limit that RESETS
    note = None
    if needs_credits:
        note = "key is VALID, but this account is out of paid credits — top up, or use OpenRouter/Groq (free)"
    elif ratelimited:
        note = "key is VALID — free quota is used up right now; it resets on the provider's schedule (see reset window)"
    return {"provider": provider, "label": p["label"], "connected": True,
            "ok": r["ok"], "reply": (r.get("text") or "").strip()[:40] if r["ok"] else None,
            "error": r.get("error"), "ms": r.get("ms"), "model": r.get("model", p["model"]),
            "rateLimited": ratelimited, "keyValid": bool(r["ok"] or r.get("code") in (429,)), "note": note}

def status_all():
    return {pid: {"label": p["label"], "keySet": bool(_key(p["env"])), "env": p["env"], "model": p["model"]}
            for pid, p in PROVIDERS.items()}

# Cold-start ordering ONLY - which cloud providers to try first before any real track record
# exists (OpenRouter fronts Kimi K2 free + MiMo + DeepSeek + many models on ONE key, so it leads).
# The ACTUAL try order used at runtime is model_rank.rank(), which starts here and then reorders
# itself as real calls succeed/fail/rate-limit - so "best" adapts over time instead of staying fixed.
PRIORITY = ["openrouter", "groq", "cerebras", "moonshot", "gemini"]

def available():
    """Cloud providers with a key set right now, ordered by live track record (best-performing
    first) - falls back to the cold-start PRIORITY order for providers with no history yet."""
    have = [pid for pid in PRIORITY if _key(PROVIDERS[pid]["env"])]
    return model_rank.rank(have, kind="cloud", fallback_order=PRIORITY)

def call_first_available(prompt, max_tokens=1500, timeout=90):
    """Try each key-having provider, best-track-record first (model_rank); return the first that
    answers. This is the cloud advisory tier of the failover chain (used when Claude+Codex are out)."""
    have = [pid for pid in PRIORITY if _key(PROVIDERS[pid]["env"])]
    order = model_rank.rank(have, kind="cloud", fallback_order=PRIORITY)
    tried = []
    for pid in order:
        r = call(pid, prompt, max_tokens=max_tokens, timeout=timeout)
        tried.append({"provider": pid, "ok": r.get("ok"), "error": r.get("error")})
        if r.get("ok"):
            return {"ok": True, "provider": pid, "label": PROVIDERS[pid]["label"],
                    "text": r["text"], "model": r.get("model"), "ms": r.get("ms"), "tried": tried}
    return {"ok": False, "error": "no cloud free-tier provider available (set a key on any of: "
            + ", ".join(PROVIDERS[p]["env"] for p in PRIORITY) + ")", "tried": tried}

if __name__ == "__main__":
    # CLI so agent-failover.ps1 (PowerShell) can reach the cloud tier without any SDK.
    import sys
    a = sys.argv[1:]
    if a and a[0] == "auto":
        print(json.dumps(call_first_available(a[1] if len(a) > 1 else sys.stdin.read())))
    elif a and a[0] == "auto-file":
        print(json.dumps(call_first_available(open(a[1], encoding="utf-8").read())))
    elif a and a[0] == "available":
        print(json.dumps(available()))
    else:
        print(json.dumps(status_all(), indent=1))
