# Mission Control server - stdlib only, no deps. Serves the dashboard + live APIs.
# Run: python server.py   ->  http://localhost:8799
# APIs: /api/summary (token usage), /api/workflows (runs), /api/services (health), /api/state (kanban/ratings, GET/POST)
import difflib, json, os, re, shutil, sys, tempfile, threading, time, urllib.request
import doctor as doctor_mod
import nba as nba_mod
import agents_hq
import budget as budget_mod
import providers as providers_mod
import approvals as approvals_mod
import terminals as terminals_mod
import models as models_mod
import logs as logs_mod
import taskhealth as taskhealth_mod
import llm_providers as llm_mod
import apihealth as apihealth_mod
import ablit as ablit_mod
import aiop as aiop_mod
import model_rank as model_rank_mod
import router as router_mod
import events as events_mod
import bridge as bridge_mod
import sysmem as sysmem_mod
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# When frozen into a single GlassPanel.exe (PyInstaller), the app's files (index.html, state.json,
# config.json, the .bat helpers…) live NEXT TO THE EXE — not in PyInstaller's temp _MEIPASS unpack dir.
# So resolve ROOT to the exe's folder when frozen, and to the source dir on a normal `python server.py`.
ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).parent
PROJECTS = Path.home() / ".claude" / "projects"
STATE_FILE = ROOT / "state.json"
# loopback-guard allowlists (see H._guarded): the only Host/Origin values a legitimate same-origin UI uses.
_LOOPBACK_AUTH = {"127.0.0.1:8799", "localhost:8799", "[::1]:8799", "127.0.0.1", "localhost", "[::1]"}
_ALLOWED_ORIGINS = {"http://127.0.0.1:8799", "http://localhost:8799", "http://[::1]:8799"}
_state_lock = threading.RLock()   # serialize state.json read-modify-write (operator loop vs request threads)
CACHE_FILE = ROOT / "usage-cache.json"
PORT = 8799

# ---------------- usage scanner (incremental, cached) ----------------
# CACHE_VERSION bump 2026-07-07: fixed a real day-bucketing bug - transcript timestamps are
# UTC, but "today" was computed from local wall-clock (datetime.now()). At IST (UTC+5:30),
# anything after ~18:30 UTC (i.e. after midnight-ish local) landed in "yesterday"'s UTC bucket
# while the server asked for "today" in local time -> today's meter silently read 0% while
# tokens were actively burning. Fix: bucket by LOCAL date (astimezone()), matching datetime.now()
# used everywhere else. Version bump forces one clean rescan so old (wrong) buckets don't linger.
CACHE_VERSION = 2
_cache = {"files": {}, "built": 0, "version": CACHE_VERSION}  # path -> {mtime, size, days:{date:{in,out,cr,cw,byModel:{}}}}
_cache_lock = threading.Lock()
_scan_state = {"scanning": False, "done": 0, "total": 0}

def _load_cache():
    global _cache
    try:
        loaded = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        if loaded.get("version") != CACHE_VERSION:
            loaded = {"files": {}, "built": 0, "version": CACHE_VERSION}  # stale schema - force rescan
        _cache = loaded
    except Exception:
        pass

def _save_cache():
    try:
        CACHE_FILE.write_text(json.dumps(_cache), encoding="utf-8")
    except Exception:
        pass

def _parse_file(path):
    """Aggregate one transcript jsonl -> per-day token sums. Cheap line filter first."""
    days = {}
    n = 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                n += 1
                if n % 1500 == 0:
                    time.sleep(0.002)   # yield the GIL so API requests stay responsive during big scans
                if '"usage"' not in line or '"output_tokens"' not in line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                msg = o.get("message") or {}
                u = msg.get("usage") or {}
                out = u.get("output_tokens", 0)
                if not isinstance(out, int):
                    continue
                ts = o.get("timestamp") or ""
                day = "unknown"
                if ts:
                    try:
                        # transcripts store UTC ("...Z"); bucket by LOCAL calendar day so it
                        # matches datetime.now() used for "today" elsewhere in this file.
                        dt_utc = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        day = dt_utc.astimezone().strftime("%Y-%m-%d")
                    except Exception:
                        day = ts[:10] if len(ts) >= 10 else "unknown"
                model = (msg.get("model") or "?").replace("claude-", "")
                d = days.setdefault(day, {"in": 0, "out": 0, "cr": 0, "cw": 0, "msgs": 0, "byModel": {}})
                d["in"] += u.get("input_tokens", 0) or 0
                d["out"] += out
                d["cr"] += u.get("cache_read_input_tokens", 0) or 0
                d["cw"] += u.get("cache_creation_input_tokens", 0) or 0
                d["msgs"] += 1
                m = d["byModel"].setdefault(model, {"in": 0, "out": 0})
                m["in"] += u.get("input_tokens", 0) or 0
                m["out"] += out
    except Exception:
        pass
    return days

def _scan(full=False):
    if _scan_state["scanning"]:
        return
    _scan_state["scanning"] = True
    try:
        files = list(PROJECTS.rglob("*.jsonl"))
        _scan_state["total"] = len(files)
        _scan_state["done"] = 0
        for p in files:
            sp = str(p)
            try:
                st = p.stat()
            except OSError:
                continue
            ent = _cache["files"].get(sp)
            if ent and ent["mtime"] == st.st_mtime and ent["size"] == st.st_size:
                _scan_state["done"] += 1
                continue
            days = _parse_file(p)
            with _cache_lock:
                _cache["files"][sp] = {"mtime": st.st_mtime, "size": st.st_size, "days": days}
            _scan_state["done"] += 1
            time.sleep(0.004)           # breathe between files
        with _cache_lock:
            _cache["built"] = time.time()
            snapshot = json.dumps(_cache)
        try:
            CACHE_FILE.write_text(snapshot, encoding="utf-8")   # heavy dump outside the lock
        except Exception:
            pass
    finally:
        _scan_state["scanning"] = False

def summary():
    today = datetime.now().strftime("%Y-%m-%d")
    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    agg = {"today": _blank(), "week": _blank(), "all": _blank()}
    daily = {}
    with _cache_lock:
        for ent in _cache["files"].values():
            for day, d in ent["days"].items():
                dd = daily.setdefault(day, _blank())
                _add(dd, d)
                _add(agg["all"], d)
                if day == today:
                    _add(agg["today"], d)
                if day >= week_ago:
                    _add(agg["week"], d)
    last14 = sorted([k for k in daily if k != "unknown"])[-14:]
    return {
        "today": agg["today"], "week": agg["week"], "all": agg["all"],
        "daily": [{"day": k, **{x: daily[k][x] for x in ("in", "out", "cr", "cw", "msgs")}} for k in last14],
        "scan": dict(_scan_state),
        "lastScanTs": _cache.get("built", 0), "serverNowTs": time.time(),
    }

def _blank():
    return {"in": 0, "out": 0, "cr": 0, "cw": 0, "msgs": 0, "byModel": {}}

def _add(t, d):
    for x in ("in", "out", "cr", "cw", "msgs"):
        t[x] += d.get(x, 0)
    for m, v in d.get("byModel", {}).items():
        tm = t["byModel"].setdefault(m, {"in": 0, "out": 0})
        tm["in"] += v["in"]; tm["out"] += v["out"]

# ---------------- workflow runs (with resume support = feature #1) ----------------
def _find_script(runid):
    """Locate the persisted workflow script for a run id, for resumeFromRunId."""
    for s in PROJECTS.glob(f"*/*/workflows/scripts/*{runid}*.js"):
        return str(s)
    return None

def workflows():
    runs = []
    for wfdir in PROJECTS.glob("*/*/subagents/workflows/wf_*"):
        try:
            agents = list(wfdir.glob("agent-*.jsonl"))
            if not agents:
                continue
            last = max(a.stat().st_mtime for a in agents)
            first = min(a.stat().st_ctime for a in agents)
            runid = wfdir.name
            script = _find_script(runid)
            live = (time.time() - last) < 150
            resumable = bool(script) and not live
            r = {"id": runid, "session": wfdir.parts[-4][:8], "agents": len(agents),
                 "started": first, "last": last, "live": live,
                 "mins": round((last - first) / 60, 1), "resumable": resumable}
            if resumable:
                r["resumeCmd"] = f'Workflow({{scriptPath:"{script}", resumeFromRunId:"{runid}"}})'
            runs.append(r)
        except OSError:
            continue
    runs.sort(key=lambda r: r["last"], reverse=True)
    return runs[:25]

# ---------------- dirty repos (uncommitted work) ----------------
def dirty_repos():
    out = []
    for name, path in venture_repos():
        if not os.path.isdir(os.path.join(path, ".git")):
            continue
        st = _git(path, "status", "--porcelain")
        if st:
            out.append({"name": name, "files": len(st.splitlines())})
    return out

def _mem_days():
    try:
        mem = os.path.expanduser("~/.claude/projects/C--/memory")
        times = [os.path.getmtime(os.path.join(mem, f)) for f in os.listdir(mem)
                 if f.startswith("domain-") and f.endswith(".md")]
        if not times:
            return None
        return int((time.time() - max(times)) / 86400)
    except Exception:
        return None

def next_best_actions():
    gov = budget_mod.governor(summary())
    actions = nba_mod.rank(work(), services(), load_state(), _mem_days(), dirty_repos(), workflows(), gov)
    # apply the user's MANUAL pin order (runtime.json nbaPins): pinned actions float to the top
    # in the user's chosen order; everything else stays auto-ranked by score. Return top 10.
    pins = _runtime().get("nbaPins", [])
    by_key = {a["key"]: a for a in actions}
    ordered, seen = [], set()
    for k in pins:
        a = by_key.get(k)
        if a and k not in seen:
            b = dict(a); b["pinned"] = True; ordered.append(b); seen.add(k)
    for a in actions:
        if a["key"] not in seen:
            b = dict(a); b["pinned"] = False; ordered.append(b); seen.add(a["key"])
    return ordered[:10]

def running_progress():
    """In-flight missions with REAL progress (sub-jobs done/total) + elapsed + est ETA."""
    s = load_state(); tasks = s.get("tasks", [])
    kids_by = {}
    for t in tasks:
        pr = t.get("parent")
        if pr:
            kids_by.setdefault(pr, []).append(t)
    now = time.time(); out = []
    for t in tasks:
        if not t.get("mission") or t.get("col") == "done":
            continue
        kids = kids_by.get(t.get("id"), [])
        if kids:
            done = sum(1 for k in kids if k.get("col") == "done")
            pct = round(100 * done / len(kids)); sub = f"{done}/{len(kids)} jobs done"
        else:
            pct = {"doing": 50, "review": 80}.get(t.get("col"), 5); sub = "estimate (no sub-jobs)"
        ts = t.get("ts", 0) or 0
        started = ts / 1000 if ts > 1e12 else (ts or now)
        elapsed = max(0, now - started)
        st = (t.get("status") or "").lower()
        blocked = any(k in st for k in ("blocked", "paused", "awaiting", "confirmation", "needs human", "not approved", "held"))
        if blocked:
            eta = "waiting on you"
        elif pct >= 100:
            eta = "done"
        elif pct > 0 and elapsed > 30:
            eta = f"~{max(1, int(elapsed / pct * (100 - pct) / 60))}m"
        else:
            eta = "—"
        out.append({"id": t.get("id"), "text": t.get("text", ""), "v": t.get("v"),
                    "pct": pct, "sub": sub, "elapsedMin": int(elapsed / 60), "eta": eta,
                    "blocked": blocked, "status": t.get("status", "")})
    out.sort(key=lambda x: (x["blocked"], -x["pct"]))
    return {"missions": out, "liveRuns": [r for r in workflows() if r.get("live")]}

# ---------------- runner health + runtime settings (self-heal plumbing) ----------------
RUNTIME = str(ROOT / "runtime.json")   # next to the exe when frozen (see ROOT above)

def _runtime():
    return aiop_mod.read_runtime()   # shared lock + atomic writer live in aiop (one owner of runtime.json)

def _set_runtime(**kv):
    with aiop_mod.RT_LOCK:           # locked read-modify-write so the operator loop can't lose our update
        d = aiop_mod.read_runtime(); d.update(kv); aiop_mod.write_runtime(d)
    return d

def runner_health():
    """Is the mission-runner actually turning? Subprocess-free: judge by its heartbeat log.
    The runner logs every ~60s, so a log older than 180s means it is hung or was killed -
    which is exactly the failure that left a queued mission 'stuck forever' with no engine."""
    import glob as _glob
    logs = sorted(_glob.glob(os.path.join(os.path.dirname(RUNTIME), "runner-logs", "runner-*.log")))
    if not logs:
        return {"state": "down", "detail": "no runner log yet - the runner has never started", "ageSec": None}
    age = time.time() - os.path.getmtime(logs[-1])
    if age < 180:
        return {"state": "polling", "detail": f"healthy - last heartbeat {int(age)}s ago", "ageSec": int(age)}
    return {"state": "hung", "detail": f"stale - no heartbeat for {int(age/60)} min; the runner is hung or was stopped",
            "ageSec": int(age)}

def failover_status():
    """The auto-failover chain + which tiers are ready. Claude/Codex are agentic (edit files);
    cloud free-tier + Gemma are advisory (strong answer/plan, no autonomous edits)."""
    rt = _runtime()
    claude_ready = bool(llm_mod._key("CLAUDE_CODE_OAUTH_TOKEN"))
    cloud = llm_mod.status_all()
    cloud_avail = llm_mod.available()
    tiers = [
        {"tier": 1, "name": "Claude", "kind": "agentic", "ready": claude_ready,
         "detail": "headless Claude - edits files & executes" if claude_ready else "no CLAUDE_CODE_OAUTH_TOKEN set"},
        {"tier": 2, "name": "Codex", "kind": "agentic", "ready": None,
         "detail": "OpenAI Codex CLI - edits files (needs 'codex login' first)"},
        {"tier": 3, "name": "Cloud free-tier", "kind": "advisory", "ready": len(cloud_avail) > 0,
         "detail": (", ".join(cloud_avail) + " ready") if cloud_avail else "no key yet on any cloud provider - add one below",
         "providers": [{"id": pid, "label": c["label"], "env": c["env"], "model": c["model"], "ready": c["keySet"]}
                       for pid, c in cloud.items()]},
        {"tier": 4, "name": "Gemma (local)", "kind": "advisory", "ready": True,
         "detail": "local Ollama - free, never runs out (writes a human-executable plan)"},
    ]
    return {"auto": bool(rt.get("autoFailover", True)), "tiers": tiers, "cloudAvailable": cloud_avail}

def takeover_manifest():
    """Everything autopilot was driving — so the human can grab each piece on manual takeover.
    Built straight from state (NOT running_progress/workflows, which scan slow workflow dirs)."""
    tasks = load_state().get("tasks", [])
    def lite(t):
        return {"id": t.get("id"), "text": t.get("text", ""), "v": t.get("v"), "status": t.get("status", ""), "pct": None}
    inflight = [lite(t) for t in tasks if t.get("mission") and t.get("col") in ("doing", "review")]
    queued = [lite(t) for t in tasks if t.get("mission") and t.get("col") == "backlog"]
    return {"inflight": inflight, "queued": queued, "runnerWas": runner_health()}

def mood():
    """A playful 'Claude's mood' DERIVED from what the dashboard actually sees (a real signal,
    not random) -> maps to a music bucket: focus | hype | chill | party."""
    try:
        sev = budget_mod.governor(summary()).get("severity", "ok")
    except Exception:
        sev = "ok"
    try:
        nrun = len([m for m in running_progress().get("missions", []) if not m.get("blocked")])
    except Exception:
        nrun = 0
    try:
        stuck = taskhealth_mod.classify(load_state()).get("counts", {}).get("high", 0)
    except Exception:
        stuck = 0
    hr = time.localtime().tm_hour
    if sev == "critical" or stuck >= 2:
        m, why = "chill", ("budget critical - cool it down" if sev == "critical" else f"{stuck} stuck tasks - decompress")
    elif nrun >= 1:
        m, why = "focus", f"{nrun} mission(s) in flight - deep-work mode"
    elif hr >= 23 or hr < 6:
        m, why = "chill", "late night - keep it mellow"
    elif 6 <= hr < 11:
        m, why = "focus", "morning - lock in"
    elif 18 <= hr < 23:
        m, why = ("party" if sev == "ok" else "hype"), "evening - bring the energy"
    else:
        m, why = "hype", "midday - keep momentum"
    return {"mood": m, "reason": why,
            "signals": {"severity": sev, "runningMissions": nrun, "stuckHigh": stuck, "hour": hr}}


def _panel(pid, label, state, spin, detail="", metric="", level=None, kind="gear", actions=None):
    # state: run(green,working) | idle(dim,ok-but-quiet) | warn(amber) | down(red). spin: is it turning?
    # actions: [{label, kind: post|open|tab|reload, url?, body?, tab?}] — the click-to-control menu.
    return {"id": pid, "label": label, "state": state, "spin": bool(spin), "detail": detail[:60],
            "metric": metric, "level": level, "kind": kind, "actions": actions or []}

_panels_cache = {"t": 0, "svc": None}

def health_panels():
    """Factory-style 'glass panels' — one per subsystem, showing if it's SPINNING at a glance, and
    carrying its own click-to-control actions (engage the operator, restart the runner, arm guardian…)."""
    now = time.time()
    P = []
    # SYSTEM RAM first — on a 16 GB box this is the panel that stops the machine from hanging. The
    # oil-level gauge is literal: % of physical RAM used. Ollama models are the biggest reclaimable chunk.
    try:
        mem = sysmem_mod.status()
        held = sysmem_mod.ollama_loaded()
        heldgb = round(sum(m["gb"] for m in held), 1)
        mem_actions = []
        if held:
            mem_actions.append({"label": f"🧹 Free {heldgb} GB (unload local models)",
                                "kind": "post", "url": "/api/mem/reclaim"})
        mem_actions.append({"label": "Open Task Manager", "kind": "post", "url": "/api/mem/taskmgr"})
        detail = (f"{heldgb} GB held by Ollama · GlassPanel ~{sysmem_mod.self_mb()} MB" if held
                  else f"GlassPanel itself uses ~{sysmem_mod.self_mb()} MB")
        P.append(_panel("memory", "SYSTEM RAM", mem["state"], True, detail,
                        f"{mem['availGB']} GB free", level=mem["usedPct"], kind="gauge", actions=mem_actions))
    except Exception:
        pass
    P.append(_panel("server", "SERVER", "run", True, "stdlib http.server :8799", "online",
                    actions=[{"label": "↻ Reload dashboard", "kind": "reload"},
                             {"label": "Open in browser ↗", "kind": "open", "url": "http://localhost:8799"}]))
    st = events_mod.streams()
    P.append(_panel("sse", "LIVE PUSH", "run" if st else "idle", st, f"SSE stream v{events_mod.version()}",
                    f"{st} client{'s' if st != 1 else ''}", kind="flow",
                    actions=[{"label": "⟳ Reconnect stream", "kind": "reload"}]))
    try:
        op = aiop_mod.status(); running = op.get("running")
        op_actions = ([{"label": "⏹ Kill operator", "kind": "post", "url": "/api/operator/stop"},
                       {"label": "⚡ Think once now", "kind": "post", "url": "/api/operator/tick"}]
                      if running else
                      [{"label": "▶ Engage — full auto", "kind": "post", "url": "/api/operator/start"}])
        op_actions.append({"label": "Open AI Operator tab", "kind": "tab", "tab": "operator"})
        P.append(_panel("operator", "AI OPERATOR", "run" if running else "idle", running,
                        op.get("activeModel", "-"), "engaged" if running else "off", actions=op_actions))
    except Exception:
        pass
    rh = runner_health(); rs = rh.get("state")
    P.append(_panel("runner", "MISSION RUNNER", {"polling": "run", "hung": "warn", "down": "idle"}.get(rs, "idle"),
                    rs == "polling", rh.get("detail", ""), rs,
                    actions=[{"label": "▶ Start / Restart", "kind": "post", "url": "/api/runner", "body": {"action": "restart"}},
                             {"label": "⏹ Stop", "kind": "post", "url": "/api/runner", "body": {"action": "stop"}}]))
    # services (ollama/n8n/crawl4ai) — network probes, cached ~15s so panels stay cheap on every tick
    if now - _panels_cache["t"] > 15 or not _panels_cache["svc"]:
        try: _panels_cache["svc"] = services()
        except Exception: _panels_cache["svc"] = []
        _panels_cache["t"] = now
    _svc_actions = {
        "ollama": [{"label": "▶ Start Ollama", "kind": "post", "url": "/api/provider/login", "body": {"provider": "ollama"}},
                   {"label": "Open Abliterated tab", "kind": "tab", "tab": "ablit"}],
        "n8n": [{"label": "Open n8n ↗", "kind": "open", "url": "http://localhost:5678"}],
        "crawl4ai": [{"label": "Open crawl4ai ↗", "kind": "open", "url": "http://localhost:11235"}],
    }
    for s in (_panels_cache["svc"] or []):
        nm, up = s.get("name", "?"), s.get("up"); loaded = s.get("loaded") or []
        P.append(_panel(nm, nm.upper(), "run" if up else "down",
                        up and (nm != "ollama" or bool(loaded)),
                        (", ".join(loaded) if nm == "ollama" and loaded else ("reachable" if up else "unreachable")),
                        "up" if up else "down", actions=_svc_actions.get(nm, [])))
    try:
        avail = llm_mod.available(); keyed = [p for p, v in llm_mod.status_all().items() if v.get("keySet")]
        lvl = round(100 * len(avail) / len(keyed)) if keyed else 0
        P.append(_panel("providers", "CLOUD PROVIDERS", "run" if avail else ("warn" if keyed else "idle"),
                        bool(avail), ("ready: " + ", ".join(avail[:4])) if avail else "no provider live",
                        f"{len(avail)}/{len(keyed)}", level=lvl, kind="gauge",
                        actions=[{"label": "Manage providers & keys", "kind": "tab", "tab": "analytics"}]))
    except Exception:
        pass
    try:
        gov = budget_mod.governor(summary()); sev = gov.get("severity", "ok")
        used = round((gov.get("today") or {}).get("pct", 0))
        P.append(_panel("budget", "TOKEN BUDGET", {"ok": "run", "warn": "warn", "critical": "down"}.get(sev, "run"),
                        True, f"severity: {sev}", f"{used}% today", level=used, kind="gauge",
                        actions=[{"label": "Open budget & governor", "kind": "tab", "tab": "analytics"}]))
    except Exception:
        pass
    # guardian — read the EXTERNAL heartbeat (loose coupling: never imports guardian)
    _g_snap = {"label": "📸 Snapshot now", "kind": "post", "url": "/api/guardian", "body": {"action": "snapshot"}}
    _g_arm = {"label": "🛟 Arm watchdog", "kind": "post", "url": "/api/guardian", "body": {"action": "arm"}}
    try:
        hbp = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "helm-guardian", "heartbeat.json")
        hb = json.loads(open(hbp, encoding="utf-8").read())
        watching = (now - hb.get("ts", 0) / 1000) < 90
        age = hb.get("lastSnapshotAgeSec")
        P.append(_panel("guardian", "GUARDIAN", "run" if watching else "warn", watching,
                        (f"last snapshot {age}s ago" if age is not None else "no snapshot yet"),
                        "watching" if watching else "armed", kind="gear",
                        actions=([_g_snap] if watching else [_g_arm, _g_snap])))
    except Exception:
        P.append(_panel("guardian", "GUARDIAN", "idle", False, "click to arm the watchdog", "off",
                        actions=[_g_arm, _g_snap]))
    return {"panels": P, "ts": int(now * 1000)}

# ---------------- real work (git commits across venture repos) ----------------
# Ventures/paths are NOT hardcoded - they come from config.json (this machine, git-ignored) so
# HELM is multi-user. Falls back to config.example.json, then a minimal default. Edit config.json
# or the UI never sees your personal paths in the shipped code.
_CFG_DIR = str(ROOT)   # config.json + the .bat/.vbs helpers live next to the exe when frozen
def load_config():
    for p in (os.path.join(_CFG_DIR, "config.json"), os.path.join(_CFG_DIR, "config.example.json")):
        try:
            return json.loads(open(p, encoding="utf-8").read())
        except Exception:
            continue
    return {"user": "you", "ventures": [{"id": "infra", "name": "infra", "color": "#8b949e",
            "dir": _CFG_DIR, "what": "HELM itself"}]}
def venture_repos():
    return [(v.get("name") or v.get("id"), v["dir"]) for v in load_config().get("ventures", []) if v.get("dir")]

def helm_version():
    """Current HELM git commit, read straight from .git (no subprocess). For the Update button."""
    gitdir = os.path.join(_CFG_DIR, ".git")
    if not os.path.isdir(gitdir):
        return {"git": False, "note": "This install isn't a git clone, so it can't self-update. Re-install via 'git clone' (or the USB installer's clone mode) to enable GitHub updates."}
    try:
        head = open(os.path.join(gitdir, "HEAD"), encoding="utf-8").read().strip()
        branch = head.split("/")[-1] if head.startswith("ref:") else "detached"
        commit, subject = "?", ""
        logp = os.path.join(gitdir, "logs", "HEAD")
        if os.path.exists(logp):
            last = open(logp, encoding="utf-8").read().strip().splitlines()[-1]
            commit = (last.split() or ["", "?"])[1][:8]
            if "\t" in last:
                subject = last.split("\t", 1)[1][:70]
        remote = ""
        cfgp = os.path.join(gitdir, "config")
        if os.path.exists(cfgp):
            for ln in open(cfgp, encoding="utf-8"):
                if "url = " in ln:
                    remote = ln.split("url = ", 1)[1].strip(); break
        return {"git": True, "branch": branch, "commit": commit, "subject": subject, "remote": remote}
    except Exception as e:
        return {"git": True, "error": str(e)[:100]}
_work_cache = {"t": 0, "data": None}

def _git(repo, *args):
    import subprocess
    try:
        p = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, timeout=10,
                           encoding="utf-8", errors="replace")
        return p.stdout.strip() if p.returncode == 0 else None
    except Exception:
        return None

def work():
    if time.time() - _work_cache["t"] < 60 and _work_cache["data"]:
        return _work_cache["data"]
    ventures, all_days = [], set()
    for name, path in venture_repos():
        if not os.path.isdir(os.path.join(path, ".git")):
            continue
        today_log = _git(path, "log", "--oneline", "--since=midnight") or ""
        commits_today = len([l for l in today_log.splitlines() if l.strip()])
        last = _git(path, "log", "-1", "--format=%s|%ar") or "|"
        msg, ago = (last.split("|", 1) + [""])[:2]
        days = _git(path, "log", "--since=14.days", "--format=%ad", "--date=short") or ""
        all_days.update(d for d in days.splitlines() if d.strip())
        week = len((_git(path, "log", "--oneline", "--since=7.days") or "").splitlines())
        ventures.append({"name": name, "today": commits_today, "week": week,
                         "lastMsg": msg[:70], "lastAgo": ago})
    data = {"ventures": ventures,
            "commitsToday": sum(v["today"] for v in ventures),
            "commitsWeek": sum(v["week"] for v in ventures),
            "shipDays": sorted(all_days)}
    _work_cache.update(t=time.time(), data=data)
    return data

def heatmap():
    """ventures x last-14-days commit counts, for the CEO heat map."""
    from datetime import datetime as _dt, timedelta as _td
    days = [(_dt.now() - _td(days=i)).strftime("%Y-%m-%d") for i in range(13, -1, -1)]
    rows = []
    for name, path in venture_repos():
        if not os.path.isdir(os.path.join(path, ".git")):
            continue
        log = _git(path, "log", "--since=15.days", "--format=%ad", "--date=short") or ""
        counts = {}
        for d in log.splitlines():
            counts[d.strip()] = counts.get(d.strip(), 0) + 1
        rows.append({"name": name, "cells": [counts.get(d, 0) for d in days]})
    return {"days": days, "rows": rows}

# ---------------- live agents (sessions + workflow agents reporting) ----------------
def _last_action(path, budget=6000):
    """Read the tail of a transcript jsonl and summarize the last meaningful event."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            f.seek(max(0, size - budget))
            tail = f.read().decode("utf-8", errors="replace")
        for line in reversed([l for l in tail.splitlines() if l.strip().startswith("{")]):
            try:
                o = json.loads(line)
            except Exception:
                continue
            t = o.get("type")
            msg = o.get("message") or {}
            content = msg.get("content")
            if t == "assistant" and isinstance(content, list):
                for c in reversed(content):
                    if c.get("type") == "tool_use":
                        inp = c.get("input") or {}
                        hint = inp.get("description") or inp.get("file_path") or inp.get("pattern") or inp.get("prompt") or ""
                        return f"⚙ {c.get('name','tool')}" + (f": {str(hint)[:60]}" if hint else "")
                    if c.get("type") == "text" and c.get("text", "").strip():
                        return "💬 " + c["text"].strip().replace("\n", " ")[:70]
            if t == "user" and isinstance(content, list) and any(x.get("type") == "tool_result" for x in content if isinstance(x, dict)):
                return "↩ tool result received"
        return "…"
    except Exception:
        return "?"

def _pretty_project(dirname):
    n = dirname.replace("C--Users-VAISHAK-", "").replace("C--", "root ").replace("-", " ")
    return (n[:38] or "root").strip()

def agents_live(window=300):
    now = time.time()
    out = []
    # top-level sessions (Claude Code windows/tabs)
    for f in PROJECTS.glob("*/*.jsonl"):
        try:
            m = f.stat().st_mtime
        except OSError:
            continue
        if now - m > window:
            continue
        out.append({"kind": "session", "who": _pretty_project(f.parent.name), "id": f.stem[:8],
                    "age": int(now - m), "action": _last_action(f)})
    # workflow subagents
    for f in PROJECTS.glob("*/*/subagents/**/agent-*.jsonl"):
        try:
            m = f.stat().st_mtime
        except OSError:
            continue
        if now - m > window:
            continue
        out.append({"kind": "agent", "who": f.parent.name.replace("wf_", "wf "), "id": f.stem.replace("agent-", "")[:10],
                    "age": int(now - m), "action": _last_action(f)})
    out.sort(key=lambda x: x["age"])
    return out[:40]

# ---------------- services ----------------
def _http_ok(url, timeout=1.5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status < 500
    except urllib.error.HTTPError as e:
        return e.code in (401, 403, 404)   # server alive, just guarded
    except Exception:
        return False

def services():
    res = {}
    def chk_n8n():   res["n8n"] = {"name": "n8n", "up": _http_ok("http://127.0.0.1:5678", 0.8)}
    def chk_c4a():   res["crawl4ai"] = {"name": "crawl4ai", "up": _http_ok("http://127.0.0.1:11235/health", 0.8)}
    def chk_oll():
        o = {"name": "ollama", "up": False, "loaded": []}
        try:
            with urllib.request.urlopen("http://127.0.0.1:11434/api/ps", timeout=0.8) as r:
                o["up"] = True
                o["loaded"] = [m.get("name") for m in json.loads(r.read()).get("models", [])]
        except Exception:
            pass
        res["ollama"] = o
    ts = [threading.Thread(target=f) for f in (chk_n8n, chk_c4a, chk_oll)]
    [t.start() for t in ts]; [t.join(1.2) for t in ts]
    return [res.get(k, {"name": k, "up": False}) for k in ("n8n", "crawl4ai", "ollama")]

# ---------------- state (kanban + ratings + xp events) ----------------
def load_state():
    with _state_lock:
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {"tasks": [], "ratings": [], "events": []}

def save_state(s):
    data = json.dumps(s, indent=1)
    with _state_lock:
        if STATE_FILE.exists():
            try:
                shutil.copy(STATE_FILE, STATE_FILE.with_suffix(".json.bak"))  # doctor restores from this
            except Exception:
                pass
        # atomic write (tmp + os.replace): a crash mid-write can never leave a torn/empty state.json
        fd, tmp = tempfile.mkstemp(dir=str(ROOT), suffix=".state.tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data)
            os.replace(tmp, STATE_FILE)
        except Exception:
            try: os.remove(tmp)
            except Exception: pass
            raise
    events_mod.bump()   # push a live tick to every open /api/events stream (board changed)

# ---------------- AI operator: sense + act (drives HELM programmatically) ----------------
# build_snapshot = what the operator SENSES each tick; agent_act = the ONE dispatcher every
# programmatic control (the autonomous loop, external headless LLMs, /api/agent/act) routes through.
def build_snapshot():
    """Compact, decision-relevant world state for the AI operator (and any external agent)."""
    s = load_state(); tasks = s.get("tasks", [])
    def lite(t):
        return {"id": t.get("id"), "text": (t.get("text") or "")[:120], "v": t.get("v"),
                "col": t.get("col"), "status": (t.get("status") or "")[:70]}
    missions = [t for t in tasks if t.get("mission")]
    backlog = [lite(t) for t in missions if t.get("col") == "backlog"]
    inflight = [lite(t) for t in missions if t.get("col") in ("doing", "review")]
    stuck = [lite(t) for t in missions if t.get("broken") or "blocked" in (t.get("status") or "").lower()
             or (t.get("runnerAttempts") or 0) >= 2]
    nowms = int(time.time() * 1000)
    recent_done = [lite(t) for t in missions if t.get("col") == "done"
                   and nowms - (t.get("doneTs") or t.get("ts") or 0) <= 30 * 60 * 1000]
    rt = _runtime()
    # taskhealth is state-only (fast). Everything here MUST stay cheap - this runs every tick, so it
    # deliberately AVOIDS the NBA/mood/work pipeline (git across every repo + usage scans = 15s+).
    try:
        th = taskhealth_mod.classify(s).get("counts", {})
    except Exception:
        th = {}
    try:
        cloud = llm_mod.available()
    except Exception:
        cloud = []
    return {
        "ventures": [{"id": v["id"], "what": v.get("what", "")} for v in load_config().get("ventures", [])],
        "counts": {"backlog": len(backlog), "inflight": len(inflight), "stuck": len(stuck),
                   "done": len([t for t in missions if t.get("col") == "done"])},
        "backlog": backlog[:12], "inflight": inflight[:12], "stuck": stuck[:12],
        "recentDone": recent_done[:12],
        "runner": runner_health(),
        "executor": rt.get("executor", "claude"), "mode": rt.get("mode", "manual"),
        "autoFailover": bool(rt.get("autoFailover", True)),
        "cloudProviders": cloud, "claudeReady": bool(llm_mod._key("CLAUDE_CODE_OAUTH_TOKEN")),
        "taskHealth": th, "hour": time.localtime().tm_hour, "ts": int(time.time() * 1000),
    }

def _mission_dupe(text, venture, tasks, nowms, window_ms=30 * 60 * 1000, ratio_thresh=0.6):
    """Id of an existing mission that's a near-duplicate of `text` for the same venture - checked
    against backlog/doing/review (still live) and done in the last `window_ms` (just-finished work
    a small local operator model has no other way to see). Structural guard so a runaway operator
    loop can't re-queue the same mission forever even if its prompt reasoning fails (2026-07-08)."""
    norm = text.strip().lower()
    for t in tasks:
        if not t.get("mission"):
            continue
        tv = t.get("v") or ""
        if venture and tv and tv != venture:
            continue
        col = t.get("col")
        if col == "done":
            if nowms - (t.get("doneTs") or t.get("ts") or 0) > window_ms:
                continue
        elif col not in ("backlog", "doing", "review"):
            continue
        other = (t.get("text") or "").strip().lower()
        if not other:
            continue
        if difflib.SequenceMatcher(None, norm, other).ratio() >= ratio_thresh:
            return t.get("id")
    return None

def _venture_busy(venture, tasks, nowms, cooldown_ms=20 * 60 * 1000):
    """True if `venture` already has a pending mission, or one finished too recently to justify
    inventing another. Text-similarity dedup alone is gameable by rewording (proven 2026-07-08: a
    reworded near-duplicate scored 0.556, just under the 0.6 threshold, and ran a SECOND real
    headless Claude session investigating the same thing 31s after the first was queued) - this is
    a rate-limit that doesn't depend on wording at all: at most one pending/just-done mission per
    venture invented by the operator at a time, full stop."""
    if not venture:
        return False
    for t in tasks:
        if not t.get("mission") or (t.get("v") or "") != venture:
            continue
        col = t.get("col")
        if col in ("backlog", "doing", "review"):
            return True
        if col == "done" and nowms - (t.get("doneTs") or t.get("ts") or 0) <= cooldown_ms:
            return True
    return False

def agent_act(action, params, actor="external"):
    """The ONE dispatcher for programmatic HELM control. Whitelisted vocabulary only (see aiop.MANIFEST);
    each branch reuses the same effect a UI button triggers. Never runs arbitrary shell. Mission TEXT is
    stored verbatim (the headless path references it by id, not text); on the FAILOVER path the runner +
    agent-failover.ps1 neutralize the double-quote before it reaches a child-process argv (Review 2026-07-08)."""
    a = (action or "").lower().strip(); p = params or {}
    base = os.path.dirname(RUNTIME); nowms = int(time.time() * 1000)
    _state_lock.acquire()   # whole load->mutate->save is one critical section (vs the loop thread / UI POSTs)
    try:
        if a == "queue_mission":
            text = str(p.get("text") or "").strip()[:1000]
            if not text:
                return {"ok": False, "error": "empty mission text"}
            v = str(p.get("venture") or p.get("v") or "").strip()
            if v and v not in {x.get("id") for x in load_config().get("ventures", [])}:
                v = ""
            s = load_state(); s.setdefault("tasks", [])
            dupe = _mission_dupe(text, v, s["tasks"], nowms)
            if dupe:
                return {"ok": False, "error": f"duplicate of existing/recently-completed mission {dupe} - not queuing"}
            if v and _venture_busy(v, s["tasks"], nowms):
                return {"ok": False, "error": f"venture '{v}' already has a pending mission or one finished "
                        "within the last 20 min - not inventing more yet"}
            tid = f"{nowms}{len(s['tasks'])}"
            s["tasks"].insert(0, {"id": tid, "text": text, "v": v or None, "col": "backlog",
                                  "mission": True, "ts": nowms, "by": actor, "status": "queued by AI operator"})
            save_state(s)
            return {"ok": True, "id": tid, "msg": f"queued mission for {v or 'no venture'}"}
        if a == "mission_act":
            mid, act = p.get("id"), (p.get("act") or "").lower()
            s = load_state(); tk = next((t for t in s.get("tasks", []) if t.get("id") == mid), None)
            if not tk:
                return {"ok": False, "error": "mission not found"}
            if act == "check":
                tk["col"] = "done"; tk["status"] = "verified & done (AI operator)"; tk["doneTs"] = nowms
            elif act == "boost":
                tk["col"] = "backlog"; tk["runnerAttempts"] = 0; tk["boosted"] = True
                s["tasks"] = [tk] + [t for t in s["tasks"] if t.get("id") != mid]; tk["status"] = "boosted - next in queue"
            elif act == "rethink":
                tk["col"] = "backlog"; tk["runnerAttempts"] = 0; tk["replan"] = True; tk["status"] = "re-plan requested"
            elif act == "broken":
                tk["col"] = "backlog"; tk["broken"] = True; tk["status"] = "BROKEN - flagged by AI operator"
            else:
                return {"ok": False, "error": "act must be check|boost|rethink|broken"}
            save_state(s); return {"ok": True, "msg": f"{act} applied"}
        if a == "heal_mission":
            mid = p.get("id")
            tk = next((t for t in load_state().get("tasks", []) if t.get("id") == mid), None)
            if not tk:
                return {"ok": False, "error": "mission not found"}
            h = runner_health()
            if h["state"] != "polling" and tk.get("col") == "backlog":
                try:
                    os.startfile(os.path.join(base, "restart-runner-hidden.vbs"))
                    return {"ok": True, "msg": f"runner was {h['state']} - restarted it"}
                except Exception as e:
                    return {"ok": False, "error": f"could not restart runner: {e}"}
            return {"ok": True, "msg": f"no structural fix needed (runner {h['state']}, col {tk.get('col')})"}
        if a == "move_task":
            mid, col = p.get("id"), (p.get("col") or "").lower()
            if col not in ("backlog", "doing", "review", "done"):
                return {"ok": False, "error": "col must be backlog|doing|review|done"}
            s = load_state(); tk = next((t for t in s.get("tasks", []) if t.get("id") == mid), None)
            if not tk:
                return {"ok": False, "error": "task not found"}
            # STRUCTURAL guard (Review 2026-07-08): don't let a raw move_task silently revive a
            # mission that's actually done WITH a real report attached - the operator did exactly
            # this ("stuck... moving to backlog") on a mission that had already finished with a
            # report, which would re-trigger the runner into re-executing already-completed work.
            # A genuine "this needs redoing" call belongs to mission_act's rethink/broken (explicit
            # intent), not a bare column move.
            if tk.get("col") == "done" and col != "done" and tk.get("report"):
                return {"ok": False, "error": "task is done with a report attached - use mission_act "
                        "(rethink/broken) if it genuinely needs redoing, not a raw column move"}
            tk["col"] = col; save_state(s); return {"ok": True, "msg": f"moved to {col}"}
        if a == "delete_task":
            mid = p.get("id"); s = load_state(); n0 = len(s.get("tasks", []))
            s["tasks"] = [t for t in s.get("tasks", []) if t.get("id") != mid]
            if len(s["tasks"]) == n0:
                return {"ok": False, "error": "task not found"}
            save_state(s); return {"ok": True, "msg": "deleted"}
        if a == "task_settings":
            mid = p.get("id"); inc = p.get("settings") or {}
            s = load_state(); tk = next((t for t in s.get("tasks", []) if t.get("id") == mid), None)
            if not tk:
                return {"ok": False, "error": "task not found"}
            cur = dict(tk.get("settings") or {})
            if "mode" in inc: cur["mode"] = "auto" if str(inc["mode"]).lower() == "auto" else "manual"
            if "executor" in inc: cur["executor"] = "failover" if str(inc["executor"]).lower() == "failover" else "claude"
            if "promptAddon" in inc: cur["promptAddon"] = str(inc["promptAddon"] or "")[:2000]
            if "maxAttempts" in inc:
                try: cur["maxAttempts"] = max(1, min(9, int(inc["maxAttempts"])))
                except Exception: pass
            tk["settings"] = cur; save_state(s); return {"ok": True, "settings": cur}
        if a == "set_mode":
            mode = (p.get("mode") or "").lower()
            if mode not in ("auto", "manual"):
                return {"ok": False, "error": "mode must be auto|manual"}
            try: os.startfile(os.path.join(base, "restart-runner-hidden.vbs" if mode == "auto" else "stop-runner-hidden.vbs"))
            except Exception: pass
            _set_runtime(mode=mode, autoFailover=(mode == "auto"))
            try: approvals_mod.set_auto(mode == "auto")
            except Exception: pass
            return {"ok": True, "msg": f"mode -> {mode}"}
        if a == "set_executor":
            ex = (p.get("executor") or "").lower()
            if ex not in ("claude", "failover"):
                return {"ok": False, "error": "executor must be claude|failover"}
            _set_runtime(executor=ex); return {"ok": True, "msg": f"executor -> {ex}"}
        if a == "set_failover":
            auto = bool(p.get("auto")); _set_runtime(autoFailover=auto)
            return {"ok": True, "msg": f"auto-failover {'ON' if auto else 'OFF'}"}
        if a == "runner":
            act = (p.get("action") or "").lower()
            vbs = {"start": "restart-runner-hidden.vbs", "restart": "restart-runner-hidden.vbs",
                   "stop": "stop-runner-hidden.vbs"}.get(act)
            if not vbs:
                return {"ok": False, "error": "action must be start|stop|restart"}
            # STRUCTURAL guard (Review 2026-07-08): the operator kept restarting an ALREADY-HEALTHY
            # runner every tick ("preventative measure" reasoning) - a prompt rule alone didn't stop
            # it, so block a no-op restart/start here regardless of what any LLM decides. A restart
            # that would actually kill in-flight work is exactly the failure mode this prevents. The
            # human's own dashboard button hits a separate endpoint (path0=="/api/runner") and is
            # NEVER blocked by this - only programmatic callers (agent_act) are gated.
            if act in ("start", "restart") and runner_health().get("state") == "polling":
                return {"ok": False, "error": "runner is already healthy (polling) - restart not needed"}
            os.startfile(os.path.join(base, vbs)); return {"ok": True, "msg": f"runner {act}"}
        if a == "pin_nba":
            op, key = p.get("op"), p.get("key"); pins = list(_runtime().get("nbaPins", []))
            if op == "toggle" and key:
                pins.remove(key) if key in pins else pins.append(key)
            elif op in ("up", "down") and key in pins:
                i = pins.index(key); j = i - 1 if op == "up" else i + 1
                if 0 <= j < len(pins): pins[i], pins[j] = pins[j], pins[i]
            elif op == "clear":
                pins = []
            else:
                return {"ok": False, "error": "op must be toggle|up|down|clear"}
            _set_runtime(nbaPins=pins); return {"ok": True, "pins": pins}
        if a == "note":
            return {"ok": True, "msg": "noted"}
        return {"ok": False, "error": f"unknown action '{a}'"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
    finally:
        _state_lock.release()

# ---------------- http ----------------
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _fail(self, e):
        # error boundary: a raising handler returns a clean JSON 500, never a silent empty reply
        try:
            self._json({"error": f"{type(e).__name__}: {e}"[:300], "endpoint": self.path}, 500)
        except Exception:
            pass

    # --- loopback guard: close the "any webpage can drive GlassPanel" hole (report's #1 finding) ---
    # The server binds 127.0.0.1, but that alone does NOT stop a malicious page open in the user's
    # browser from POSTing to http://localhost:8799/api/launch, nor a DNS-rebind attack. Two header
    # checks the browser sets and JS cannot forge close both, breaking nothing legitimate (the UI is
    # always same-origin on a loopback host):
    #   Host   must be a loopback authority   -> defeats DNS-rebinding (attacker's domain in Host)
    #   Origin (when present) must be our own  -> defeats a foreign page CSRF-driving us
    def _guarded(self):
        host = (self.headers.get("Host") or "").split(",")[0].strip().lower()
        if host and host not in _LOOPBACK_AUTH:
            return {"ok": False, "error": "blocked: non-loopback Host (possible DNS-rebind)"}, 403
        origin = self.headers.get("Origin")
        if origin and origin.strip().lower() not in _ALLOWED_ORIGINS:
            return {"ok": False, "error": "blocked: cross-origin request"}, 403
        return None

    def do_GET(self):
        try:
            self._get()
        except Exception as e:
            self._fail(e)

    def _get(self):
        blocked = self._guarded()          # rebind/cross-origin reads blocked too (e.g. exfiltrating /api/state)
        if blocked:
            return self._json(blocked[0], blocked[1])
        p = self.path.split("?")[0]
        if p == "/api/events":
            # SSE live-push: one long-lived stream per client. Emits "tick" the instant any state write
            # bumps events.version() (board/runtime/operator), else a heartbeat comment every ~20s so
            # dead connections are detected. Replaces ~15 polling loops with ONE connection.
            if not events_mod.open_stream():
                return self._json({"error": "too many streams"}, 503)
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                last = events_mod.version()
                self.wfile.write(f"event: tick\ndata: {last}\n\n".encode()); self.wfile.flush()
                while True:
                    v = events_mod.wait(last, 20)
                    if v != last:
                        last = v
                        self.wfile.write(f"event: tick\ndata: {v}\n\n".encode())
                    else:
                        self.wfile.write(b": ping\n\n")   # heartbeat -> detect a dead client
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
                pass
            finally:
                events_mod.close_stream()
            return
        if p == "/api/summary":
            threading.Thread(target=_scan, daemon=True).start()  # refresh in background
            return self._json(summary())
        if p == "/api/workflows":
            return self._json(workflows())
        if p == "/api/services":
            return self._json(services())
        if p == "/api/state":
            return self._json(load_state())
        if p == "/api/work":
            return self._json(work())
        if p == "/api/heatmap":
            return self._json(heatmap())
        if p == "/api/agents":
            return self._json(agents_live())
        if p == "/api/nba":
            return self._json(next_best_actions())
        if p == "/api/budget":
            return self._json(budget_mod.load())
        if p == "/api/governor":
            return self._json(budget_mod.governor(summary()))
        if p == "/api/providers":
            return self._json(providers_mod.status())
        if p == "/api/approvals":
            return self._json(approvals_mod.list_pending())
        if p == "/api/approvals/history":
            return self._json(approvals_mod.list_all())
        if p == "/api/approvals/config":
            return self._json(approvals_mod.get_config())
        if p == "/api/approvals/status":
            from urllib.parse import parse_qs, urlparse
            iid = parse_qs(urlparse(self.path).query).get("id", [""])[0]
            item = approvals_mod.get(iid)
            return self._json({"status": item["status"] if item else "unknown"})
        if p == "/api/terminals":
            return self._json(terminals_mod.running())
        if p == "/api/models":
            return self._json(models_mod.catalog())
        if p == "/api/ablit/state":
            return self._json(ablit_mod.state())
        if p == "/api/hf/search":
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query).get("q", [""])[0]
            return self._json({"results": ablit_mod.hf_search(q)})
        if p == "/api/logs/approvals":
            return self._json(logs_mod.approval_activity())
        if p == "/api/logs/files":
            return self._json(logs_mod.log_files())
        if p == "/api/taskhealth":
            return self._json(taskhealth_mod.classify(load_state()))
        if p == "/api/running":
            return self._json(running_progress())
        if p == "/api/runner":
            return self._json(runner_health())
        if p == "/api/executor":
            return self._json({"executor": _runtime().get("executor", "claude")})
        if p == "/api/failover":
            return self._json(failover_status())
        if p == "/api/mode":
            rt = _runtime()
            return self._json({"mode": rt.get("mode", "manual"),
                               "autoFailover": rt.get("autoFailover", True),
                               "runner": runner_health()})
        if p == "/api/mood":
            return self._json(mood())
        if p == "/api/health/panels":
            return self._json(health_panels())
        if p == "/api/bridge/sessions":
            # the workflow index: every recent Claude Code/Desktop session, newest first
            return self._json({"sessions": bridge_mod.index_sessions()})
        if p == "/api/bridge/session":
            from urllib.parse import parse_qs, urlparse
            sid = parse_qs(urlparse(self.path).query).get("id", [""])[0]
            return self._json(bridge_mod.session_workflow(sid))
        if p == "/api/bridge/pull":
            # GlassPanel -> here: the open work a Claude session can pick up and continue.
            # NOTE: this list must NOT be named `work` — that shadows the module-level work() function
            # across the whole do_GET scope and breaks /api/work (UnboundLocalError). (mythos audit fix)
            s = load_state()
            openwork = [{"id": t.get("id"), "text": t.get("text"), "v": t.get("v"), "status": t.get("status", "")}
                        for t in s.get("tasks", []) if t.get("mission") and t.get("col") == "backlog"]
            return self._json({"work": openwork, "count": len(openwork),
                               "howto": "Act on these in a Claude session; POST results to /api/bridge/push or /api/job."})
        if p == "/api/config":
            return self._json(load_config())
        if p == "/api/version":
            return self._json(helm_version())
        if p == "/api/agent/manifest":
            # the action vocabulary any LLM (built-in operator OR an external headless agent) can invoke.
            return self._json({"actions": aiop_mod.MANIFEST,
                               "note": "POST /api/agent/act {action, params} to drive HELM. GET /api/agent/observe for state."})
        if p == "/api/agent/observe":
            return self._json(build_snapshot())
        if p == "/api/agent/audit":
            return self._json({"audit": aiop_mod.audit(80)})
        if p == "/api/operator/status":
            return self._json(aiop_mod.status())
        if p == "/api/models/rank":
            # transparency for the "query best LLMs, adapt over time" ranking - not a fixed opinion,
            # a live record of what's actually been working (see model_rank.py).
            return self._json({"local": model_rank_mod.snapshot("local"), "cloud": model_rank_mod.snapshot("cloud")})
        if p == "/api/music":
            rt = _runtime().get("music", {})
            return self._json({"artist": rt.get("artist", "auto"), "autoMood": rt.get("autoMood", True),
                               "lastMood": rt.get("lastMood"), "volume": rt.get("volume", 60)})
        if p == "/api/models/test":
            from urllib.parse import parse_qs, urlparse
            prov = parse_qs(urlparse(self.path).query).get("provider", [""])[0]
            return self._json(llm_mod.test(prov))
        if p == "/api/models/providers":
            return self._json(llm_mod.status_all())
        if p == "/api/agents-hq/list":
            return self._json(agents_hq.list_agents())
        if p == "/api/agents-hq/get":
            from urllib.parse import parse_qs, urlparse
            name = parse_qs(urlparse(self.path).query).get("name", [""])[0]
            try:
                return self._json(agents_hq.get_agent(name))
            except Exception as e:
                return self._json({"error": str(e)}, 400)
        if p == "/api/agents-hq/versions":
            from urllib.parse import parse_qs, urlparse
            name = parse_qs(urlparse(self.path).query).get("name", [""])[0]
            try:
                return self._json(agents_hq.list_versions(name))
            except Exception as e:
                return self._json({"error": str(e)}, 400)
        if p == "/api/doctor":
            fix = "fix=1" in (self.path.split("?")[1] if "?" in self.path else "")
            return self._json(doctor_mod.run_doctor(fix))
        if p == "/favicon.ico":
            self.send_response(204); self.end_headers(); return
        f = ROOT / ("index.html" if p == "/" else p.lstrip("/"))
        if f.is_file() and f.resolve().is_relative_to(ROOT.resolve()):
            self.send_response(200)
            ct = {".html": "text/html", ".md": "text/plain", ".json": "application/json",
                  ".txt": "text/plain", ".png": "image/png", ".jpg": "image/jpeg",
                  ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
                  ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
                  ".pdf": "application/pdf", ".svg": "image/svg+xml"}.get(f.suffix.lower(), "application/octet-stream")
            self.send_header("Content-Type", f"{ct}; charset=utf-8")
            self.end_headers()
            self.wfile.write(f.read_bytes())
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        try:
            self._post()
        except Exception as e:
            self._fail(e)

    def _post(self):
        blocked = self._guarded()          # reject foreign-origin / rebind before ANY mutation runs
        if blocked:
            return self._json(blocked[0], blocked[1])
        path0 = self.path.split("?")[0]
        if path0 == "/api/launch":
            # day-start tiers, WHITELISTED profiles only (never arbitrary commands)
            import subprocess
            try:
                n = int(self.headers.get("Content-Length", 0))
                profile = (json.loads(self.rfile.read(n)) or {}).get("profile", "")
                if profile not in ("claude", "codex", "antigravity", "gemma", "trio", "full"):
                    return self._json({"ok": False, "error": "unknown profile"}, 400)
                subprocess.Popen(
                    ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                     str(ROOT / "day-launcher.ps1"), "-Profile", profile, "-FromDashboard"],
                    creationflags=0x08000000, cwd=str(ROOT))  # CREATE_NO_WINDOW
                return self._json({"ok": True, "profile": profile,
                                   "note": "launching - windows will appear; details in day-launcher.log"})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 400)
        if path0 == "/api/upload":
            # raw-body upload: POST /api/upload?name=<filename>, body = file bytes.
            # Saved under attachments/ and served statically. 200MB cap.
            from urllib.parse import parse_qs, urlparse, unquote
            try:
                q = parse_qs(urlparse(self.path).query)
                name = unquote(q.get("name", ["file.bin"])[0])
                name = re.sub(r"[^A-Za-z0-9._ -]", "_", os.path.basename(name))[:120] or "file.bin"
                n = int(self.headers.get("Content-Length", 0))
                if n <= 0 or n > 200 * 1024 * 1024:
                    return self._json({"ok": False, "error": "empty or >200MB"}, 400)
                att = ROOT / "attachments"
                att.mkdir(exist_ok=True)
                dest = att / name
                stem, suf, i = dest.stem, dest.suffix, 1
                while dest.exists():
                    dest = att / f"{stem}-{i}{suf}"; i += 1
                remaining, chunks = n, []
                while remaining > 0:
                    c = self.rfile.read(min(remaining, 1 << 20))
                    if not c:
                        break
                    chunks.append(c); remaining -= len(c)
                dest.write_bytes(b"".join(chunks))
                return self._json({"ok": True, "url": f"/attachments/{dest.name}",
                                   "path": str(dest), "size": dest.stat().st_size})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 400)
        if path0 == "/api/job":
            # upsert ONE job/card by id — used by the /mission orchestrator so agents
            # update their own card without racing the whole board.
            n = int(self.headers.get("Content-Length", 0))
            try:
                j = json.loads(self.rfile.read(n))
                assert j.get("id")
                s = load_state()
                s.setdefault("tasks", [])
                found = next((t for t in s["tasks"] if t.get("id") == j["id"]), None)
                if found:
                    found.update({k: v for k, v in j.items() if v is not None})
                else:
                    s["tasks"].append(j)
                save_state(s)
                return self._json({"ok": True})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 400)
        if path0 == "/api/mission/act":
            # one-click controls on an IN-FLIGHT mission card. Every action has a REAL,
            # consumed effect (no decorative fields): the runner picks the FIRST eligible
            # backlog mission and always launches the same headless Claude - there is no
            # effort/model knob - so 'boost' = jump the run queue, not a fake "high effort".
            n = int(self.headers.get("Content-Length", 0))
            try:
                b = json.loads(self.rfile.read(n)) if n else {}
                mid, act = b.get("id"), (b.get("act") or "").lower()
                s = load_state(); s.setdefault("tasks", [])
                tk = next((t for t in s["tasks"] if t.get("id") == mid), None)
                if not tk:
                    return self._json({"ok": False, "error": "mission not found"}, 404)
                nowms = int(time.time() * 1000)
                if act == "check":            # you verified it against reality -> Done
                    tk["col"] = "done"; tk["status"] = "verified & done (you checked it)"
                    tk["doneTs"] = nowms; tk["verifiedBy"] = "human"
                    msg = "checked -> moved to Done"
                elif act == "boost":          # more effort / fast-forward: run it NEXT
                    tk["col"] = "backlog"; tk["runnerAttempts"] = 0; tk["boosted"] = True
                    tk["boostedTs"] = nowms; tk["status"] = "boosted - next in the run queue"
                    # runner takes the first eligible mission in array order -> move to front
                    s["tasks"] = [tk] + [t for t in s["tasks"] if t.get("id") != mid]
                    msg = "boosted - it's now next in the run queue"
                elif act == "rethink":        # the plan is wrong -> re-queue for a fresh plan
                    tk["col"] = "backlog"; tk["runnerAttempts"] = 0; tk["replan"] = True
                    tk["status"] = "re-plan requested (you flagged the approach)"
                    msg = "flagged for re-plan -> back in Backlog for a fresh plan"
                elif act == "broken":         # declare it broken/stuck -> stop the false progress
                    tk["col"] = "backlog"; tk["broken"] = True; tk["blockedTs"] = nowms
                    tk["status"] = "BROKEN - blocked, flagged by you"
                    msg = "marked broken -> now in the Stuck tracker with fix options"
                else:
                    return self._json({"ok": False, "error": "unknown act"}, 400)
                save_state(s)
                return self._json({"ok": True, "act": act, "msg": msg})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 400)
        if path0 == "/api/runner":
            # start/stop/restart the mission-runner. os.startfile a hidden .vbs (reliable from
            # the wscript-spawned server; subprocess is not). 'restart' also kills a hung runner.
            n = int(self.headers.get("Content-Length", 0))
            try:
                b = json.loads(self.rfile.read(n)) if n else {}
                act = (b.get("action") or "").lower()
                base = os.path.dirname(RUNTIME)
                if act in ("start", "restart"):
                    os.startfile(os.path.join(base, "restart-runner-hidden.vbs"))
                    return self._json({"ok": True, "action": act, "msg": "runner (re)starting - give it ~5s, then it polls the queue"})
                if act == "stop":
                    os.startfile(os.path.join(base, "stop-runner-hidden.vbs"))
                    return self._json({"ok": True, "action": "stop", "msg": "runner stopping (background fleet paused)"})
                return self._json({"ok": False, "error": "action must be start|stop|restart"}, 400)
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 400)
        if path0 == "/api/executor":
            # switch how the runner executes missions. Two HONEST, already-tested paths:
            #   claude   = headless Claude only (default)
            #   failover = agent-failover chain (Claude -> Codex -> Gemma, each switch you approve).
            # Standalone Codex/Gemma-first isn't offered until `codex login` is done and Gemma is
            # promoted past menial - so the control reflects reality, not a wish.
            n = int(self.headers.get("Content-Length", 0))
            try:
                b = json.loads(self.rfile.read(n)) if n else {}
                ex = (b.get("executor") or "").lower()
                if ex not in ("claude", "failover"):
                    return self._json({"ok": False, "error": "executor must be claude|failover"}, 400)
                _set_runtime(executor=ex)
                auto = _runtime().get("autoFailover", True)
                if ex == "claude":
                    msg = "next missions run on headless Claude only"
                else:
                    msg = ("next missions run through the failover chain (Claude -> Codex -> cloud free-tier -> Gemma), "
                           + ("switching AUTOMATICALLY on exhaustion" if auto else "each switch approved by you"))
                return self._json({"ok": True, "executor": ex, "msg": msg})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 400)
        if path0 == "/api/mode":
            # ONE big switch. autopilot = runner running + auto-failover + auto-approve (hands-off).
            # manual takeover = pause the runner, gate failover + approvals, and hand back the
            # manifest of everything that was auto-running so the human can drive it.
            n = int(self.headers.get("Content-Length", 0))
            try:
                b = json.loads(self.rfile.read(n)) if n else {}
                mode = (b.get("mode") or "").lower()
                base = os.path.dirname(RUNTIME)
                if mode == "manual":
                    try: os.startfile(os.path.join(base, "stop-runner-hidden.vbs"))
                    except Exception: pass
                    _set_runtime(mode="manual", autoFailover=False)
                    try: approvals_mod.set_auto(False)
                    except Exception: pass
                    return self._json({"ok": True, "mode": "manual", "manifest": takeover_manifest(),
                                       "msg": "Manual takeover - auto-runner paused, auto-failover OFF, approvals manual. You have the wheel."})
                if mode == "auto":
                    try: os.startfile(os.path.join(base, "restart-runner-hidden.vbs"))
                    except Exception: pass
                    _set_runtime(mode="auto", autoFailover=True)
                    try: approvals_mod.set_auto(True)
                    except Exception: pass
                    return self._json({"ok": True, "mode": "auto",
                                       "msg": "Autopilot ON - runner running, auto-failover + auto-approve on. Missions execute hands-off."})
                return self._json({"ok": False, "error": "mode must be auto|manual"}, 400)
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 400)
        if path0 == "/api/task/settings":
            # per-task "containerized" control (CRUD). Each mission card carries its own
            # settings: mode (auto|manual), promptAddon (injected into its run), executor,
            # maxAttempts. Per-task mode is authoritative; unset falls back to the global switch.
            n = int(self.headers.get("Content-Length", 0))
            try:
                b = json.loads(self.rfile.read(n)) if n else {}
                tid = b.get("id")
                if not tid:
                    return self._json({"ok": False, "error": "missing task id"}, 400)
                s = load_state(); s.setdefault("tasks", [])
                t = next((x for x in s["tasks"] if x.get("id") == tid), None)
                if not t:
                    return self._json({"ok": False, "error": "task not found"}, 404)
                if b.get("clear"):                     # DELETE settings -> back to default
                    t.pop("settings", None); save_state(s)
                    return self._json({"ok": True, "settings": None, "cleared": True})
                cur = dict(t.get("settings") or {})
                inc = b.get("settings") or {}
                if "mode" in inc:
                    cur["mode"] = "auto" if str(inc["mode"]).lower() == "auto" else "manual"
                if "executor" in inc:
                    cur["executor"] = "failover" if str(inc["executor"]).lower() == "failover" else "claude"
                if "promptAddon" in inc:
                    cur["promptAddon"] = str(inc["promptAddon"] or "")[:2000]
                if "maxAttempts" in inc:
                    try:
                        cur["maxAttempts"] = max(1, min(9, int(inc["maxAttempts"])))
                    except Exception:
                        pass
                t["settings"] = cur; save_state(s)
                return self._json({"ok": True, "settings": cur})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 400)
        if path0 == "/api/nba/pins":
            # manual priority override for Next Best Action. op-based so the server owns the
            # list (pins survive even when a signal briefly drops off the top-12).
            n = int(self.headers.get("Content-Length", 0))
            try:
                b = json.loads(self.rfile.read(n)) if n else {}
                op, key = b.get("op"), b.get("key")
                pins = list(_runtime().get("nbaPins", []))
                if op == "toggle" and key:
                    pins.remove(key) if key in pins else pins.append(key)
                elif op in ("up", "down") and key in pins:
                    i = pins.index(key); j = i - 1 if op == "up" else i + 1
                    if 0 <= j < len(pins):
                        pins[i], pins[j] = pins[j], pins[i]
                elif op == "clear":
                    pins = []
                else:
                    return self._json({"ok": False, "error": "op must be toggle|up|down|clear"}, 400)
                _set_runtime(nbaPins=pins)
                return self._json({"ok": True, "pins": pins})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 400)
        if path0 == "/api/music":
            # persist the music player's small prefs (chosen artist, auto-mood on/off, volume).
            n = int(self.headers.get("Content-Length", 0))
            try:
                b = json.loads(self.rfile.read(n)) if n else {}
                cur = _runtime().get("music", {})
                for k in ("artist", "autoMood", "lastMood", "volume"):
                    if k in b:
                        cur[k] = b[k]
                _set_runtime(music=cur)
                return self._json({"ok": True, "music": cur})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 400)
        if path0 == "/api/failover":
            # toggle fully-automatic failover (no approval on exhaustion switchover).
            n = int(self.headers.get("Content-Length", 0))
            try:
                b = json.loads(self.rfile.read(n)) if n else {}
                auto = bool(b.get("auto"))
                _set_runtime(autoFailover=auto)
                return self._json({"ok": True, "auto": auto,
                                   "msg": ("FULL AUTO - if Claude/Fable/Opus runs out, it switches to Codex -> cloud free-tier -> Gemma with no approval"
                                           if auto else "MANUAL - you approve each agentic switchover (advisory tiers stay automatic)")})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 400)
        if path0 == "/api/mission/heal":
            # THE self-heal brain. Diagnose WHY a mission is stuck and apply the safe structural
            # fix (restart a hung/down runner), while surfacing the honest reason. Never
            # auto-approves mission content - that stays a manual, one-by-one human decision.
            n = int(self.headers.get("Content-Length", 0))
            try:
                b = json.loads(self.rfile.read(n)) if n else {}
                mid = b.get("id")
                s = load_state()
                tk = next((t for t in s.get("tasks", []) if t.get("id") == mid), None)
                if not tk:
                    return self._json({"ok": False, "error": "mission not found"}, 404)
                st = (tk.get("status") or "").lower()
                col = tk.get("col"); att = tk.get("runnerAttempts") or 0
                text = (tk.get("text") or "").lower()
                health = runner_health()
                eligible = (tk.get("mission") and col == "backlog"
                            and not any(k in st for k in ("blocked", "dispatched", "partial", "held", "awaiting"))
                            and att < 2)
                reason = fix = recommend = ""; applied = False
                if col == "done":
                    reason = "Already Done - nothing to fix."
                elif any(k in st for k in ("partial", "paused", "awaiting", "confirmation")):
                    reason = f"Half-finished and waiting on YOU (status: '{tk.get('status')}'). The runner never auto-retries a partial mission."
                    recommend = "Click Check if it's actually done, or Retry from the Stuck tracker."
                elif "blocked" in st or tk.get("broken"):
                    reason = "Flagged blocked/broken. Resolve the blocker, then Retry."
                    recommend = "Retry once unblocked, or Rethink to re-plan."
                elif att >= 2 or "runner-failed" in st:
                    reason = f"Failed {att} times - the runner gave up. Check the mission log."
                    recommend = "Rethink (the approach may be wrong) or Remove."
                elif eligible and health["state"] != "polling":
                    reason = (f"Your mission is queued and READY - but the runner engine is {health['state']} "
                              f"({health['detail']}). Nothing is turning the queue.")
                    try:
                        os.startfile(os.path.join(os.path.dirname(RUNTIME), "restart-runner-hidden.vbs"))
                        applied = True
                        fix = "Restarted the runner. Your mission is next in line; approve it in the Agents tab when it asks."
                    except Exception as e:
                        fix = f"Could not auto-restart the runner ({e}) - start it with MISSION-RUNNER.bat."
                elif eligible:
                    reason = "Queued and the runner is healthy - it will pick this up within ~60s, then ask for your approval."
                    recommend = "Watch the Agents tab for the approval prompt."
                else:
                    reason = f"Status '{tk.get('status')}', column '{col}' - not currently runner-eligible."
                    recommend = "Boost to re-queue it, or Rethink to re-plan."
                NOT_HEADLESS = ("png", "image", "photo", "logo", "design", "whatsapp", "video", "jpg", "jpeg")
                needs = [w for w in NOT_HEADLESS if w in text]
                note = ""
                if needs and col != "done":
                    note = ("HEADS UP: this asks for " + "/".join(needs) + " - headless Claude can't generate images or "
                            "send WhatsApp, so it will fail even with a healthy runner. Run it yourself in the interactive "
                            "app, or Rethink it into text/HTML deliverables the runner CAN produce.")
                return self._json({"ok": True, "reason": reason, "fix": fix, "applied": applied,
                                   "recommend": recommend, "note": note, "runner": health})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 400)
        if path0 in ("/api/agents-hq/recommend", "/api/agents-hq/save", "/api/agents-hq/lesson", "/api/agents-hq/restore"):
            n = int(self.headers.get("Content-Length", 0))
            try:
                b = json.loads(self.rfile.read(n))
                if path0.endswith("recommend"):
                    return self._json(agents_hq.recommend(b.get("task", "")))
                if path0.endswith("save"):
                    return self._json(agents_hq.save_agent(b.get("name", ""), b.get("content", "")))
                if path0.endswith("lesson"):
                    return self._json(agents_hq.add_lesson(b.get("name", ""), b.get("lesson", "")))
                if path0.endswith("restore"):
                    return self._json(agents_hq.restore(b.get("name", ""), b.get("version", "")))
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 400)
        if path0 == "/api/approvals":
            n = int(self.headers.get("Content-Length", 0))
            try:
                b = json.loads(self.rfile.read(n))
                item = approvals_mod.create(b.get("title", "(untitled)"), b.get("detail", ""), b.get("who", "system"))
                return self._json(item)
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 400)
        if path0 == "/api/open-folder":
            # open Windows Explorer at a whitelisted path. Safe: only paths under the user's
            # home or C:\Temp, must exist. os.startfile (not subprocess) - reliable from the
            # wscript-spawned server, opens on the interactive desktop.
            n = int(self.headers.get("Content-Length", 0))
            try:
                path = (json.loads(self.rfile.read(n)) or {}).get("path", "")
                rp = os.path.abspath(path)
                allowed = (rp.startswith(os.path.expanduser("~")) or rp.startswith("C:\\Temp"))
                if not allowed:
                    return self._json({"ok": False, "error": "path not in an allowed location"}, 403)
                if not os.path.exists(rp):
                    return self._json({"ok": False, "error": "folder does not exist on disk yet"}, 404)
                os.startfile(rp if os.path.isdir(rp) else os.path.dirname(rp))
                return self._json({"ok": True, "opened": rp})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 400)
        if path0 == "/api/taskhealth/mitigate":
            n = int(self.headers.get("Content-Length", 0))
            try:
                b = json.loads(self.rfile.read(n))
                tid, action = b.get("id"), b.get("action")
                s = load_state()
                s.setdefault("tasks", [])
                if action == "remove":
                    before = len(s["tasks"])
                    s["tasks"] = [t for t in s["tasks"] if t.get("id") != tid]
                    save_state(s)
                    return self._json({"ok": before != len(s["tasks"]), "action": "removed"})
                tk = next((t for t in s["tasks"] if t.get("id") == tid), None)
                if not tk:
                    return self._json({"ok": False, "error": "task not found"}, 404)
                if action == "retry":
                    tk["col"] = "backlog"; tk["status"] = "queued (manual retry)"; tk["runnerAttempts"] = 0
                elif action == "done":
                    tk["col"] = "done"; tk["status"] = "done (manual)"; tk["doneTs"] = int(time.time() * 1000)
                else:
                    return self._json({"ok": False, "error": "unknown action"}, 400)
                save_state(s)
                return self._json({"ok": True, "action": action})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 400)
        if path0 == "/api/approvals/approve-all":
            return self._json(approvals_mod.approve_all())
        if path0 == "/api/approvals/auto":
            n = int(self.headers.get("Content-Length", 0))
            try:
                b = json.loads(self.rfile.read(n)) if n else {}
                return self._json(approvals_mod.set_auto(b.get("on", False)))
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 400)
        if path0 == "/api/approvals/respond":
            n = int(self.headers.get("Content-Length", 0))
            try:
                b = json.loads(self.rfile.read(n))
                ok = approvals_mod.respond(b.get("id", ""), bool(b.get("approved")))
                return self._json({"ok": ok})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 400)
        if path0 == "/api/ablit/pull":
            # pull a (large) GGUF model into Ollama in a visible terminal so the user sees progress.
            n = int(self.headers.get("Content-Length", 0))
            try:
                model = (json.loads(self.rfile.read(n)) if n else {}).get("model", "").strip()
                # whitelist: hf.co/... repo pulls or plain ollama model names. No shell metachars.
                if not model or not re.match(r"^[\w./:@-]+$", model):
                    return self._json({"ok": False, "error": "invalid model name"}, 400)
                base = os.path.dirname(RUNTIME)
                bat = os.path.join(base, "_ablit-pull.bat")
                with open(bat, "w", encoding="utf-8") as f:
                    f.write("@echo off\r\ntitle HELM - pulling %s\r\n" % model)
                    f.write("echo Pulling %s into Ollama (this can take a while for big models)...\r\n" % model)
                    f.write('ollama pull "%s"\r\n' % model)
                    f.write("echo.\r\necho Done. Close this window; the model now shows in HELM's Abliterated tab.\r\npause\r\n")
                os.startfile(bat)
                return self._json({"ok": True, "msg": f"pulling {model} in a terminal - watch its progress there, then it appears as 'pulled'."})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 400)
        if path0 == "/api/guardian":
            # Machine Room control for the external watchdog: arm it (launch GUARDIAN.bat) or snapshot now.
            n = int(self.headers.get("Content-Length", 0))
            try:
                act = (json.loads(self.rfile.read(n)) if n else {}).get("action", "").lower()
                if act == "arm":
                    os.startfile(os.path.join(_CFG_DIR, "GUARDIAN.bat"))
                    return self._json({"ok": True, "msg": "Guardian watchdog launching — the panel flips to 'watching' shortly."})
                if act == "snapshot":
                    import guardian as _g
                    p = _g.snapshot("manual-ui")
                    return self._json({"ok": True, "msg": "snapshot saved: " + os.path.basename(p)})
                return self._json({"ok": False, "error": "action must be arm|snapshot"}, 400)
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 400)
        if path0 == "/api/mem/reclaim":
            # Get RAM back: evict every model Ollama is holding resident (often 3-4 GB on a 4B model).
            res = sysmem_mod.unload_ollama()
            res["memory"] = sysmem_mod.status()
            res["msg"] = (f"freed {res['freedGB']} GB ({', '.join(res['unloaded'])})" if res["unloaded"]
                          else "nothing was loaded — no RAM to reclaim")
            events_mod.bump()
            return self._json(res)
        if path0 == "/api/mem/taskmgr":
            try:
                os.startfile("taskmgr.exe"); return self._json({"ok": True, "msg": "opened Task Manager"})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 400)
        if path0 in ("/api/bridge/capture", "/api/bridge/push"):
            # THE FULL-PERMISSIONS TRANSFER SLOT (loopback-only). capture = index a Claude session's
            # workflow into HELM as a spec + mission; push = a Claude session shoves a workflow straight
            # in. Both create a mission DIRECTLY (no approval gate) — that's what makes it "full perms".
            n = int(self.headers.get("Content-Length", 0))
            try:
                b = json.loads(self.rfile.read(n)) if n else {}
                if path0.endswith("capture"):
                    wf = bridge_mod.session_workflow(b.get("id", ""))
                    if wf.get("error"):
                        return self._json(wf, 404)
                else:  # push: build a workflow from the posted payload
                    ints = b.get("intents") or ([b["text"]] if b.get("text") else [])
                    wf = {"id": b.get("id") or f"push-{int(time.time())}", "title": b.get("title", "pushed workflow"),
                          "intents": ints, "intentCount": len(ints), "files": b.get("files", []), "tools": b.get("tools", [])}
                cap = bridge_mod.capture_doc(wf)
                res = {"ok": True, "doc": os.path.basename(cap["doc"]), "title": cap["title"]}
                if b.get("asMission", True):
                    res["mission"] = agent_act("queue_mission",
                                               {"text": b.get("text") or cap["mission"], "venture": b.get("venture")},
                                               actor="bridge")
                return self._json(res)
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 400)
        if path0 in ("/api/route", "/api/relay", "/api/plan"):
            # Efficiency router: a LOCAL model picks the cheapest capable provider per task (never
            # defaults to Claude) and relays outputs provider->provider with wiki+git+ledger context.
            n = int(self.headers.get("Content-Length", 0))
            try:
                b = json.loads(self.rfile.read(n)) if n else {}
                repo = b.get("repo")
                if path0.endswith("route"):
                    ctx = (router_mod.wiki_context(repo) + "\n" + router_mod.git_context(repo)).strip()
                    return self._json(router_mod.run(b.get("task", ""), ctx))
                if path0.endswith("relay"):
                    return self._json(router_mod.relay(b.get("goal", ""), b.get("steps", []), repo))
                return self._json(router_mod.plan_and_relay(b.get("goal", ""), repo))
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 400)
        if path0 == "/api/infer/venture":
            # LOCAL inference (Gemma via Ollama) to auto-pick the venture for a mission - free, on-device.
            n = int(self.headers.get("Content-Length", 0))
            try:
                text = (json.loads(self.rfile.read(n)) if n else {}).get("text", "")
                vents = [{"id": v["id"], "label": v.get("name", ""), "desc": v.get("what", "")}
                         for v in load_config().get("ventures", [])]
                return self._json(ablit_mod.classify(text, vents) or {"venture": None})
            except Exception as e:
                return self._json({"venture": None, "error": str(e)}, 400)
        if path0 == "/api/agent/act":
            # PROGRAMMATIC control - the surface a headless LLM uses to operate HELM in your place.
            # {action, params} routed through the ONE whitelisted dispatcher (aiop.MANIFEST vocabulary).
            n = int(self.headers.get("Content-Length", 0))
            try:
                b = json.loads(self.rfile.read(n)) if n else {}
                return self._json(agent_act(b.get("action", ""), b.get("params") or {},
                                            actor=b.get("actor", "external")))
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 400)
        if path0 in ("/api/operator/start", "/api/operator/stop", "/api/operator/tick", "/api/operator/config"):
            # the built-in autonomous loop: start = engage full-auto, stop = KILL SWITCH,
            # tick = think-once now, config = set model/interval/maxActionsPerTick.
            n = int(self.headers.get("Content-Length", 0))
            try:
                b = json.loads(self.rfile.read(n)) if n else {}
                if path0.endswith("start"):
                    return self._json(aiop_mod.start())
                if path0.endswith("stop"):
                    return self._json(aiop_mod.stop())
                if path0.endswith("tick"):
                    return self._json(aiop_mod.tick())
                aiop_mod.set_cfg(**{k: b[k] for k in ("model", "intervalSec", "maxActionsPerTick", "allowCloud") if k in b})
                return self._json(aiop_mod.status())
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 400)
        if path0 == "/api/ablit/chat":
            # run ONE local turn (text or vision) against a pulled abliterated model. All local.
            n = int(self.headers.get("Content-Length", 0))
            try:
                b = json.loads(self.rfile.read(n)) if n else {}
                return self._json(ablit_mod.chat(b.get("model", ""), b.get("prompt", ""), b.get("image")))
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 400)
        if path0 == "/api/update":
            # pull latest HELM from GitHub + restart, in a terminal (git is in Program Files, not the
            # npm folder that Defender hides from this lineage, so it resolves fine).
            try:
                bat = os.path.join(_CFG_DIR, "UPDATE.bat")
                if not os.path.isdir(os.path.join(_CFG_DIR, ".git")):
                    return self._json({"ok": False, "error": "not a git clone - can't self-update. Re-install via git clone."}, 400)
                if not os.path.exists(bat):
                    return self._json({"ok": False, "error": "UPDATE.bat missing"}, 404)
                os.startfile(bat)
                return self._json({"ok": True, "msg": "Updating from GitHub in a terminal - it'll git-pull + restart HELM. Hard-refresh (Ctrl+Shift+R) when it finishes."})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 400)
        if path0 == "/api/provider/login":
            # VERIFIED root cause: Windows Defender's file-access filter hides the npm-shim CLIs
            # (codex, gemini) from HELM's background service (the wscript scripting-host lineage sees
            # 4/46 files in AppData\npm) - so the server can NEVER launch them. Fix: drop a self-
            # contained login shortcut on the DESKTOP + open the Desktop; the user double-clicks it
            # (real explorer = clean lineage that sees all 46 files). Non-npm launchers run directly.
            n = int(self.headers.get("Content-Length", 0))
            try:
                prov = (json.loads(self.rfile.read(n)) if n else {}).get("provider", "").lower()
                base = os.path.dirname(RUNTIME)
                if prov == "antigravity":
                    return self._json({"ok": True, "launched": False, "url": "https://antigravity.google/",
                                       "msg": "Antigravity signs in through its own app (Google account) - open Antigravity, sign in, then hit refresh."})
                CLI = {"codex": ("codex", "@openai/codex", "codex login"),
                       "gemini": ("gemini", "@google/gemini-cli", "gemini")}
                if prov in CLI:  # npm shims - must run from a clean (desktop) lineage
                    exe, pkg, cmd = CLI[prov]
                    desk = os.path.join(os.path.expanduser("~"), "Desktop")
                    name = f"HELM - Log in to {prov}.bat"
                    ps = f"if(-not(Get-Command {exe} -EA SilentlyContinue)){{Write-Host 'installing {exe}...';npm i -g {pkg}}}; {cmd}"
                    with open(os.path.join(desk, name), "w", encoding="utf-8") as f:
                        f.write("@echo off\r\ntitle HELM - Log in to %s\r\n" % prov)
                        f.write("echo A browser will open - sign in, then close this window and refresh HELM.\r\n")
                        f.write('powershell -NoProfile -ExecutionPolicy Bypass -Command "%s"\r\npause\r\n' % ps)
                    try: os.startfile(desk)   # open Desktop so the shortcut is right there
                    except Exception: pass
                    return self._json({"ok": True, "launched": False, "desktop": name,
                                       "msg": f"Opened your Desktop - DOUBLE-CLICK '{name}' to log in. (Windows Defender hides the {exe} CLI from HELM's background service, so it must run from your desktop.)"})
                bats = {"claude": "login-claude.bat", "gemma": "start-ollama.bat", "ollama": "start-ollama.bat"}
                bat = bats.get(prov)
                if not bat:
                    return self._json({"ok": False, "error": f"no login flow for '{prov}'"}, 400)
                path = os.path.join(base, bat)
                if not os.path.exists(path):
                    return self._json({"ok": False, "error": "launcher missing"}, 404)
                os.startfile(path)
                steps = {"claude": "A terminal opened running `claude setup-token` - follow it to save the token, then refresh.",
                         "gemma": "A terminal opened starting Ollama - leave it running; then refresh."}
                return self._json({"ok": True, "launched": True, "msg": steps.get(prov, "launcher opened")})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 400)
        if path0 in ("/api/models/diagnose", "/api/models/autofix", "/api/models/aifix"):
            # API/login error handler: diagnose classifies the failure + fix plan; autofix runs
            # real queries to self-heal (model discovery); aifix asks a WORKING free LLM to fix it.
            n = int(self.headers.get("Content-Length", 0))
            try:
                prov = (json.loads(self.rfile.read(n)) if n else {}).get("provider", "")
                if not prov:
                    return self._json({"ok": False, "error": "missing provider"}, 400)
                if path0.endswith("autofix"):
                    return self._json(apihealth_mod.autofix(prov))
                if path0.endswith("aifix"):
                    return self._json(apihealth_mod.aifix(prov))
                res = llm_mod.test(prov)
                return self._json({"ok": True, "test": res, "diagnosis": apihealth_mod.diagnose(prov, res)})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 400)
        if path0 == "/api/models/setkey":
            # write an API key to the user env (HKCU\Environment via winreg - reliable, no
            # subprocess) then live-test it. Whitelisted to the 5 known providers only.
            n = int(self.headers.get("Content-Length", 0))
            try:
                b = json.loads(self.rfile.read(n))
                prov = b.get("provider", ""); key = (b.get("key") or "").strip()
                p = llm_mod.PROVIDERS.get(prov)
                if not p:
                    return self._json({"ok": False, "error": "unknown provider"}, 400)
                if len(key) < 8:
                    return self._json({"ok": False, "error": "key looks too short"}, 400)
                import winreg
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as k:
                    winreg.SetValueEx(k, p["env"], 0, winreg.REG_SZ, key)
                os.environ[p["env"]] = key  # so this server tests it right away
                return self._json({"ok": True, "env": p["env"], "test": llm_mod.test(prov)})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 400)
        if path0 == "/api/providers/refresh":
            return self._json(providers_mod.status(force=True))
        if path0 == "/api/budget":
            n = int(self.headers.get("Content-Length", 0))
            try:
                return self._json({"ok": True, "budget": budget_mod.save(json.loads(self.rfile.read(n)))})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 400)
        if path0 == "/api/state":
            n = int(self.headers.get("Content-Length", 0))
            try:
                s = json.loads(self.rfile.read(n))
                assert isinstance(s.get("tasks"), list) and isinstance(s.get("ratings"), list)
                # Preserve per-task `settings` written out-of-band (via /api/task/settings by the
                # runner or another tab): a stale full-state save from one tab must NOT wipe them.
                prev = {t.get("id"): t for t in load_state().get("tasks", []) if t.get("id")}
                for t in s["tasks"]:
                    tid = t.get("id")
                    if tid and "settings" not in t and tid in prev and "settings" in prev[tid]:
                        t["settings"] = prev[tid]["settings"]
                save_state({"tasks": s["tasks"], "ratings": s["ratings"], "events": s.get("events", [])})
                return self._json({"ok": True})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 400)
        self._json({"error": "not found"}, 404)

def serve(block=True):
    """Start the server. block=True runs forever (CLI); block=False returns after starting a daemon
    thread (so GlassPanel.exe can run the server in-process behind the native window). Returns True if
    this instance is serving, False if another instance already holds the port."""
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    except OSError:
        print(f"GlassPanel already running on :{PORT} - this instance won't bind (harmless).")
        return False
    _load_cache()
    threading.Thread(target=_scan, daemon=True).start()
    # AI operator: inject sense+act hooks; resume the autonomous loop if it was left engaged.
    aiop_mod.set_hooks(build_snapshot, agent_act)
    if aiop_mod.cfg().get("enabled"):
        try:
            aiop_mod.start(); print("AI operator resumed (was engaged) - full auto")
        except Exception as e:
            print("AI operator resume failed:", e)
    print(f"GlassPanel -> http://localhost:{PORT}  (first usage scan runs in background)")
    if block:
        srv.serve_forever()
    else:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    return True


if __name__ == "__main__":
    serve(block=True)
