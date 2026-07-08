# ablit.py - local uncensored/abliterated model workbench + HuggingFace integration.
# Everything here runs 100% LOCALLY via Ollama (no cloud, no data leaves the PC). Abliterated =
# the refusal direction removed; these models are unfiltered. Used for uncensored local research,
# red-teaming, and creative work - the operator owns the outputs.
import json, os, time, urllib.request, urllib.parse, urllib.error

OLLAMA = "http://127.0.0.1:11434"
UA = {"User-Agent": "Mozilla/5.0 HELM/1.0"}

# Curated abliterated/uncensored GGUF repos (VERIFIED live on HF 2026-07-08). Pull into Ollama with
# `ollama pull hf.co/<repo>:<QUANT>`. On a 6GB GPU prefer 3B-4B @ Q4; bigger runs on CPU (slower).
CATALOG = [
    # --- text ---
    {"id": "hf.co/mradermacher/Qwen2.5-3B-Instruct-abliterated-GGUF", "name": "Qwen2.5-3B abliterated",
     "kind": "text", "size": "~2 GB @ Q4 - FITS 6GB GPU", "quant": "Q4_K_M",
     "why": "Small, fast, fully uncensored general model - best default for a 6GB card."},
    {"id": "hf.co/culturerevolt/gemma-4-12b-heretic-abliterated-GGUF", "name": "Gemma-4 12B heretic-abliterated",
     "kind": "text", "size": "~7 GB @ Q4 - CPU/offload on 6GB", "quant": "Q4_K_M",
     "why": "Stronger reasoning; run mostly on CPU if it won't fit VRAM."},
    {"id": "hf.co/huihui-ai/Huihui-DeepSeek-V4-Flash-abliterated-ds4-GGUF", "name": "DeepSeek-V4-Flash abliterated",
     "kind": "text", "size": "large - pick smallest quant / CPU", "quant": "Q4_K_M",
     "why": "Most-downloaded abliterated model on HF; heavy - use a small quant."},
    # --- multimodal (vision) ---
    {"id": "hf.co/mradermacher/Qwen2.5-VL-3B-Instruct-abliterated-GGUF", "name": "Qwen2.5-VL 3B abliterated (vision)",
     "kind": "vision", "size": "~3 GB @ Q4 - FITS 6GB GPU", "quant": "Q4_K_M",
     "why": "Uncensored image understanding, small enough for your GPU. Needs Ollama vision support."},
    {"id": "hf.co/mradermacher/Qwen2.5-VL-7B-Instruct-abliterated-GGUF", "name": "Qwen2.5-VL 7B abliterated (vision)",
     "kind": "vision", "size": "~5 GB @ Q4", "quant": "Q4_K_M",
     "why": "Bigger uncensored vision model; tight on 6GB, offload to CPU."},
]


def _get(url, timeout=20):
    return json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout).read())


def hf_search(q, limit=12):
    """Search HuggingFace Hub for models (public API, no auth). Sorted by downloads."""
    if not q:
        q = "abliterated gguf"
    url = ("https://huggingface.co/api/models?search=" + urllib.parse.quote(q)
           + "&sort=downloads&direction=-1&limit=" + str(int(limit)))
    try:
        d = _get(url)
        return [{"id": m.get("id"), "downloads": m.get("downloads", 0), "likes": m.get("likes", 0),
                 "gguf": "gguf" in (m.get("id", "").lower() + " ".join(m.get("tags", []) or []).lower()),
                 "pipeline": m.get("pipeline_tag", "")} for m in d]
    except Exception as e:
        return {"error": str(e)[:150]}


def ollama_up():
    try:
        _get(OLLAMA + "/api/tags", timeout=3); return True
    except Exception:
        return False


def ollama_tags():
    try:
        d = _get(OLLAMA + "/api/tags", timeout=4)
        return [m.get("name") for m in d.get("models", [])]
    except Exception:
        return []


_SUGGEST_CACHE = {"t": 0, "data": []}

def suggest_new_abliterated(limit=3, ttl=6 * 3600):
    """Live HF search for currently-popular abliterated GGUF models NOT already in CATALOG or
    pulled - so the local fleet can improve over time instead of staying frozen to whatever was
    known when CATALOG was written. Cached (6h) so the Abliterated tab doesn't hit HF on every
    poll; on a transient network failure it keeps serving the last good result rather than going
    blank. Purely a suggestion surfaced in the UI - never auto-pulled (that stays a manual click)."""
    now = time.time()
    if now - _SUGGEST_CACHE["t"] < ttl:
        return _SUGGEST_CACHE["data"]
    results = hf_search("abliterated gguf", limit=8)
    if isinstance(results, dict):  # hf_search's error shape - keep serving the last good value
        return _SUGGEST_CACHE["data"]
    known = {m["id"].lower().replace("hf.co/", "") for m in CATALOG}  # CATALOG ids carry an hf.co/ prefix; HF search results don't
    have = set(t.lower() for t in ollama_tags())
    out = []
    for m in results:
        mid = m.get("id") or ""
        low = mid.lower()
        if not mid or not m.get("gguf") or low in known:
            continue
        if any(low in h or h.split(":")[0] in low for h in have):
            continue
        out.append({"id": mid, "downloads": m.get("downloads", 0), "likes": m.get("likes", 0)})
    out.sort(key=lambda m: -m["downloads"])
    out = out[:limit]
    _SUGGEST_CACHE.update(t=now, data=out)
    return out


def state():
    """Catalog + which are already pulled locally + ollama health + any newer model worth trying."""
    have = set(ollama_tags())
    def pulled(cid):
        base = cid.lower()
        return any(base in h.lower() or h.lower().split(":")[0] in base for h in have)
    return {"ollama": ollama_up(), "local": sorted(have),
            "catalog": [{**m, "pulled": pulled(m["id"])} for m in CATALOG],
            "suggested": suggest_new_abliterated()}


def pick_model():
    """A pulled local model to run classification on (prefer small, fast, present)."""
    tags = ollama_tags()
    for pref in ("gemma3:4b", "qwen2.5:3b", "llama3.2:3b", "deepseek-r1:1.5b"):
        if pref in tags:
            return pref
    return tags[0] if tags else None

def classify(text, options):
    """Local LLM classifier (Ollama). options=[{id,label,desc}] -> best-matching id, or None.
    Used to auto-pick the venture for a mission - free, on-device, no cloud tokens."""
    if not text or not options or not ollama_up():
        return None
    model = pick_model()
    if not model:
        return None
    ids = [o["id"] for o in options]
    lines = "\n".join(f"- {o['id']}: {o.get('label','')} - {o.get('desc','')}" for o in options)
    prompt = ("Pick the ONE project that best matches the task below. Answer with ONLY the project id "
              "(a single lowercase word from the list), nothing else.\n\n"
              f"PROJECTS:\n{lines}\n\nTASK: {text}\n\nBest project id:")
    r = chat(model, prompt, timeout=45)
    if not r.get("ok"):
        return {"venture": None, "error": r.get("error"), "model": model}
    out = (r.get("text") or "").strip().lower()
    first = (out.split() or [""])[0].strip(".,:;\"'`*")
    vid = first if first in ids else next((i for i in ids if i in out), None)
    return {"venture": vid, "model": model, "ms": r.get("ms")}

def chat(model, prompt, image_b64=None, timeout=180):
    """Run a local turn against a pulled model. image_b64 (no data: prefix) enables vision models."""
    if not model or not prompt:
        return {"ok": False, "error": "model + prompt required"}
    msg = {"role": "user", "content": prompt}
    if image_b64:
        msg["images"] = [image_b64]
    body = json.dumps({"model": model, "messages": [msg], "stream": False}).encode()
    t0 = time.time()
    try:
        req = urllib.request.Request(OLLAMA + "/api/chat", data=body,
                                     headers={"Content-Type": "application/json", **UA}, method="POST")
        d = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        return {"ok": True, "text": (d.get("message") or {}).get("content", ""),
                "ms": int((time.time() - t0) * 1000), "model": model}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        return {"ok": False, "error": f"HTTP {e.code}: {detail} (is the model pulled? run pull first)"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:180] + " - is Ollama running + the model pulled?"}
