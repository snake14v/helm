# sysmem.py — RAM sensing + guards, so GlassPanel never hangs a small machine.
#
# Stdlib only (ctypes, same trick terminals.py uses) — NO psutil, keeping the zero-dependency promise.
#
# Measured reality on a 16 GB box: GlassPanel's own server is ~82 MB and its native window ~133 MB.
# It is NOT the memory problem. The real hazard is what it can TRIGGER: engaging the AI Operator makes
# Ollama load a local model (a 4B Q4 ≈ 3.3 GB resident). On a 16 GB machine already carrying Claude
# (~3.7 GB) that can tip the box into swap-death. So: measure before loading, refuse when tight, and
# offer a one-click unload to get the RAM back.
import ctypes, json, os, urllib.request

OLLAMA = "http://127.0.0.1:11434"

# Free-RAM floor required before we let a local model load. A 4B Q4 needs ~3.3 GB; +0.7 GB headroom so
# Windows doesn't start swapping the moment it lands. Override with GLASSPANEL_MIN_FREE_GB.
MIN_FREE_GB_FOR_LOCAL_MODEL = float(os.environ.get("GLASSPANEL_MIN_FREE_GB", "4.0"))
WARN_PCT, CRIT_PCT = 80, 90


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]


def status():
    """Total / available physical RAM + a state. Instant (one syscall), safe to call every tick."""
    try:
        m = _MEMORYSTATUSEX(); m.dwLength = ctypes.sizeof(m)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
            raise OSError("GlobalMemoryStatusEx failed")
        total = m.ullTotalPhys / 2**30
        avail = m.ullAvailPhys / 2**30
        pct = int(m.dwMemoryLoad)
        state = "down" if pct >= CRIT_PCT else ("warn" if pct >= WARN_PCT else "run")
        return {"ok": True, "totalGB": round(total, 1), "availGB": round(avail, 1),
                "usedPct": pct, "state": state}
    except Exception as e:
        return {"ok": False, "error": str(e)[:100], "totalGB": 0, "availGB": 0, "usedPct": 0, "state": "idle"}


def self_mb():
    """This process's resident set, MB. Honest self-reporting for the panel."""
    try:
        class _PMC(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
        c = _PMC(); c.cb = ctypes.sizeof(c)
        k = ctypes.windll.kernel32
        k.GetCurrentProcess.restype = ctypes.c_void_p     # HANDLE is 64-bit; without this it truncates
        h = k.GetCurrentProcess()
        # Win7+ exposes this on kernel32 as K32GetProcessMemoryInfo; psapi.dll export doesn't always resolve.
        fn = getattr(k, "K32GetProcessMemoryInfo", None) or ctypes.WinDLL("psapi").GetProcessMemoryInfo
        fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PMC), ctypes.c_ulong]
        fn.restype = ctypes.c_int
        if fn(h, ctypes.byref(c), c.cb):
            return round(c.WorkingSetSize / 2**20)
    except Exception:
        pass
    return 0


def ollama_loaded():
    """Models Ollama is holding in RAM right now: [{name, gb}] (empty if Ollama is down)."""
    try:
        with urllib.request.urlopen(OLLAMA + "/api/ps", timeout=2) as r:
            models = json.loads(r.read()).get("models", []) or []
        return [{"name": m.get("name", "?"), "gb": round((m.get("size") or 0) / 1e9, 2)} for m in models]
    except Exception:
        return []


def unload_ollama():
    """Evict every loaded Ollama model (keep_alive:0). Returns GB freed. The 'get my RAM back' button."""
    loaded = ollama_loaded()
    freed = 0.0
    for m in loaded:
        try:
            body = json.dumps({"model": m["name"], "keep_alive": 0}).encode()
            req = urllib.request.Request(OLLAMA + "/api/generate", data=body,
                                         headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=8).read()
            freed += m["gb"]
        except Exception:
            pass
    return {"ok": True, "freedGB": round(freed, 2), "unloaded": [m["name"] for m in loaded]}


def can_load_local_model(need_gb=None):
    """Guard: is there enough free RAM to load a local model without swap-death?
    Returns (allowed: bool, reason: str). Called before the operator drives Ollama."""
    need = float(need_gb or MIN_FREE_GB_FOR_LOCAL_MODEL)
    s = status()
    if not s["ok"]:
        return True, "memory unreadable — allowing (fail-open)"
    if any(m for m in ollama_loaded()):
        return True, "a model is already resident — no new load needed"
    if s["availGB"] < need:
        return False, (f"only {s['availGB']} GB free of {s['totalGB']} GB ({s['usedPct']}% used); "
                       f"a local model needs ~{need} GB. Close some apps or use a cloud provider.")
    return True, f"{s['availGB']} GB free — ok"
