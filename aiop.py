# aiop.py - HELM's autonomous AI operator. A LOCAL (abliterated / uncensored) LLM drives the whole
# mission-control dashboard in the user's place: observe -> decide -> act, in a loop, unattended.
# FULL AUTO by design - no approval gates, no action allow-list beyond the vocabulary itself.
#
# The three things kept are NOT limits on the AI's decisions - they are what make "leave it running
# on crazy mode" safe:
#   1. The LLM can only invoke actions from a fixed MANIFEST (a vocabulary of HELM operations) -
#      it never gets a raw shell, so a poisoned mission text can't turn into `git push --force`.
#   2. Every action is written to an audit log (operator-audit.jsonl) so you can SEE what it did.
#   3. A kill switch (the `enabled` flag, checked every 2s) stops the loop instantly.
#
# The brain is an abliterated local model via Ollama (nothing leaves the PC). If Ollama is down it
# falls back to the cloud free-tier failover chain (advisory). server.py injects the sensor (world
# snapshot) and the actor (the real, tested action dispatcher) so there is ONE code path for actions.
import json, os, threading, time, re, tempfile
import ablit as ablit_mod
import llm_providers as llm_mod
import model_rank
import events
import sysmem

ROOT = os.path.dirname(os.path.abspath(__file__))
RUNTIME = os.path.join(ROOT, "runtime.json")
AUDIT = os.path.join(ROOT, "operator-audit.jsonl")
MAX_AUDIT_BYTES = 5 * 1024 * 1024   # rotate the audit log past 5MB (keep exactly one .1 backup)

# ONE process-wide lock guards every runtime.json read-modify-write. server.py's _runtime/_set_runtime
# import and use this SAME lock, so the operator loop thread and request threads can never interleave
# a write and corrupt/lose data (Review 2026-07-08). _KILL is the authoritative stop signal: once set,
# the loop exits within one slice regardless of what any concurrent runtime.json write says.
RT_LOCK = threading.RLock()
_KILL = threading.Event()
_thread_lock = threading.Lock()

# ---- the action vocabulary the operator (and any external LLM) may invoke ----------------------
# Every entry maps 1:1 to a real, already-tested effect via the injected actor (server.agent_act).
MANIFEST = [
    {"action": "queue_mission", "params": {"text": "the mission/outcome to work on", "venture": "venture id (optional)"},
     "desc": "Add a new mission to the Backlog for the runner to execute. THE main lever - decide what work moves the ventures forward and queue it."},
    {"action": "mission_act", "params": {"id": "mission card id", "act": "check|boost|rethink|broken"},
     "desc": "Act on an in-flight mission: check=verified done, boost=run next, rethink=re-plan, broken=flag stuck."},
    {"action": "heal_mission", "params": {"id": "stuck mission id"},
     "desc": "Diagnose ONE specific stuck mission (id MUST be a real id from EXISTING MISSION IDS, never invented, never the word 'runner'). If the runner itself is down and there's nothing to heal, use `runner` instead - NOT this."},
    {"action": "move_task", "params": {"id": "card id", "col": "backlog|doing|review|done"},
     "desc": "Move a card between board columns."},
    {"action": "delete_task", "params": {"id": "card id"},
     "desc": "Remove a card from the board entirely."},
    {"action": "task_settings", "params": {"id": "card id", "settings": {"mode": "auto|manual", "executor": "claude|failover", "promptAddon": "text", "maxAttempts": 3}},
     "desc": "Per-task containerized control: set its mode/executor/prompt add-on/retry cap."},
    {"action": "set_mode", "params": {"mode": "auto|manual"},
     "desc": "Master autopilot switch. auto = runner on + auto-failover + auto-approve (hands-off)."},
    {"action": "set_executor", "params": {"executor": "claude|failover"},
     "desc": "How missions execute: headless Claude only, or the Claude->Codex->cloud->Gemma failover chain."},
    {"action": "set_failover", "params": {"auto": True},
     "desc": "Fully-automatic failover on/off (switch providers on exhaustion with no approval)."},
    {"action": "runner", "params": {"action": "start|stop|restart"},
     "desc": "Control the mission-runner engine ITSELF (not a specific mission). Use this - not heal_mission - when the runner is hung/down, even if there are no missions queued right now."},
    {"action": "pin_nba", "params": {"op": "toggle|up|down|clear", "key": "nba action key"},
     "desc": "Reprioritize the Next-Best-Action list (manual priority override)."},
    {"action": "note", "params": {"text": "an observation or plan"},
     "desc": "Record a thought/plan with no side effect (shows in the audit feed). Use to explain your reasoning."},
]
_MANIFEST_NAMES = {m["action"] for m in MANIFEST}

DEFAULTS = {"enabled": False, "model": "auto", "intervalSec": 90, "maxActionsPerTick": 5, "allowCloud": True}

_SENSOR = None   # () -> snapshot dict            (injected by server: build_snapshot)
_ACTOR = None    # (action, params, actor) -> res  (injected by server: agent_act)
_thread = None


def set_hooks(sensor, actor):
    global _SENSOR, _ACTOR
    _SENSOR, _ACTOR = sensor, actor


# ---- config (persisted in runtime.json under "operator") ---------------------------------------
def read_runtime():
    """Locked read of runtime.json (shared lock so a concurrent write can't be seen half-written)."""
    with RT_LOCK:
        try:
            return json.loads(open(RUNTIME, encoding="utf-8").read())
        except Exception:
            return {}


def write_runtime(d):
    """Atomic, locked whole-file write of runtime.json. server._set_runtime uses this too."""
    with RT_LOCK:
        fd, tmp = tempfile.mkstemp(dir=ROOT, suffix=".rt.tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(d, indent=1))
            os.replace(tmp, RUNTIME)
        except Exception:
            try: os.remove(tmp)
            except Exception: pass
            raise
    events.bump()   # runtime settings changed -> push a live tick (covers _set_runtime + operator cfg)


_rt = read_runtime   # back-compat alias


def cfg():
    return {**DEFAULTS, **(read_runtime().get("operator") or {})}


def set_cfg(**kv):
    with RT_LOCK:
        d = read_runtime()
        o = {**DEFAULTS, **(d.get("operator") or {})}
        for k, v in kv.items():
            if k == "intervalSec":
                try: v = max(20, min(3600, int(v)))
                except Exception: continue
            if k == "maxActionsPerTick":
                try: v = max(1, min(20, int(v)))
                except Exception: continue
            o[k] = v
        d["operator"] = o
        write_runtime(d)
    return o


# ---- which model is the brain -------------------------------------------------------------------
# Not a fixed list: the pool is whatever's ACTUALLY pulled right now, ranked by model_rank's real
# track record (recorded after every tick in decide() below). A model that keeps winning naturally
# rises; one that starts failing (OOM'd, uninstalled, degraded) naturally sinks - no code change
# needed as the local fleet changes over time. Untested candidates are broken by parameter-count as
# a capability guess, so a bigger newly-pulled model gets a fair first try instead of being ignored.
_COLD_START_PREF = ("gemma3:4b", "qwen2.5:3b", "llama3.2:3b", "deepseek-r1:1.5b")

def operator_model(c=None):
    """The decision model. Explicit override wins outright. Otherwise pick the best-ranked pulled
    local model, preferring abliterated/uncensored tags when any are pulled ('full abliterated')."""
    c = c or cfg()
    m = c.get("model")
    if m and m not in ("auto", ""):
        return m
    tags = ablit_mod.ollama_tags()
    if not tags:
        return "gemma3:4b"
    ab = [t for t in tags if any(k in t.lower() for k in ("abliterat", "heretic", "uncensored", "dolphin", "hermes"))]
    pool = ab or tags
    fallback = [t for t in _COLD_START_PREF if t in pool] + [t for t in pool if t not in _COLD_START_PREF]
    return model_rank.best(pool, kind="local", fallback_order=fallback, param_hint=True) or pool[0]


# ---- audit -------------------------------------------------------------------------------------
def _write_audit(entry):
    entry = {"ts": int(time.time() * 1000), **entry}
    try:
        with open(AUDIT, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        if os.path.getsize(AUDIT) > MAX_AUDIT_BYTES:   # bound disk: keep one rotation
            bak = AUDIT + ".1"
            try:
                if os.path.exists(bak): os.remove(bak)
                os.replace(AUDIT, bak)
            except Exception:
                pass
    except Exception:
        pass
    events.bump()   # operator acted -> push a live tick (audit feed + status update)
    return entry


def audit(n=60):
    try:
        lines = open(AUDIT, encoding="utf-8").read().splitlines()
    except Exception:
        return []
    out = []
    for ln in lines[-n:]:
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    out.reverse()
    return out


# ---- decide (the LLM turn) ---------------------------------------------------------------------
def _build_prompt(snap):
    vocab = "\n".join(f'  - {m["action"]}({", ".join(m["params"].keys())}): {m["desc"]}' for m in MANIFEST)
    ventures = ", ".join(v["id"] for v in snap.get("ventures", []))
    c = snap.get("counts", {}); r = snap.get("runner", {}); rstate = str(r.get("state", "?"))
    ids = []
    for grp in ("backlog", "inflight", "stuck"):
        for t in snap.get(grp, []):
            if t.get("id"):
                ids.append(f'{t["id"]} [{grp}: {(t.get("text") or "")[:45]}]')
    idlist = "; ".join(ids) if ids else "NONE - there are no missions on the board right now"
    recent_done = snap.get("recentDone", [])
    donelist = ("; ".join(f'{t["id"]} [{t.get("v","?")}: {(t.get("text") or "")[:60]}]' for t in recent_done)
                if recent_done else "none in the last 30 min")
    prov = ", ".join(snap.get("cloudProviders", [])) or "none"
    runner_verdict = "HEALTHY - do NOT restart it, that would be a wasted/harmful action" if rstate == "polling" else "NEEDS a restart (runner action, not heal_mission)"
    situation = (f"Runner engine is {rstate} -> {runner_verdict}. Executor={snap.get('executor')}. Mode={snap.get('mode')}. "
                 f"AutoFailover={snap.get('autoFailover')}. Board: {c.get('backlog',0)} queued, "
                 f"{c.get('inflight',0)} in-flight, {c.get('stuck',0)} stuck, {c.get('done',0)} done. "
                 f"Cloud providers ready: {prov}. Claude token: {'set' if snap.get('claudeReady') else 'MISSING'}.")
    return (
        "You are HELM's autonomous operator, running a solo founder's mission-control in his place, "
        "fully unattended. Keep every venture moving: queue the right work, unstick stalled missions, "
        "keep the engine running, switch providers before anything runs out.\n\n"
        f"VENTURES you can create work for: {ventures}\n\n"
        f"SITUATION RIGHT NOW: {situation}\n\n"
        f"EXISTING MISSION IDS (copy an id EXACTLY if you act on one): {idlist}\n\n"
        f"COMPLETED IN THE LAST 30 MIN (do NOT re-queue these - the work is already done): {donelist}\n\n"
        "ACTIONS (use ONLY these names; params must match exactly):\n" + vocab + "\n\n"
        "RULES:\n"
        "- mission_act / heal_mission / move_task / delete_task / task_settings need an id from the "
        "EXISTING MISSION IDS list above. If that list says NONE, do NOT use those actions - there is "
        "nothing to act on. NEVER invent an id, and NEVER pass the word 'runner' as an id.\n"
        f'- The runner verdict above is authoritative: if it says HEALTHY, do NOT call runner at all - '
        'restarting a healthy runner is REJECTED by the server and wastes this turn. Only call runner '
        '{"action":"restart"} when the verdict says NEEDS a restart. Never use heal_mission for this - '
        "heal_mission is ONLY for a specific stuck mission that exists in EXISTING MISSION IDS.\n"
        "- Do NOT change executor or failover if they are already the value you want.\n"
        "- To create work use queue_mission with a concrete outcome + a venture id. The server allows "
        "AT MOST ONE pending-or-just-done mission per venture at a time (rejected otherwise, wording "
        "doesn't matter) - so if a venture already has one queued/in-flight/recently done, do NOT "
        "invent another for it this tick even if you'd phrase it differently. An empty backlog right "
        "after a completion is expected, not a gap to fill.\n"
        "- Prefer returning [] over taking a wrong action. Only act when it genuinely helps.\n\n"
        "FULL WORLD STATE (JSON, source of truth):\n" + json.dumps(snap, ensure_ascii=False)[:3500] + "\n\n"
        'Respond with ONLY a JSON array, no prose, no markdown. Each item: '
        '{"action":"<name>","params":{...},"why":"<short reason>"}.\n'
        'Examples: [{"action":"queue_mission","params":{"text":"Draft the ORBEAN Q3 menu update","venture":"orbean"},"why":"orbean backlog empty"}]   or   []\n'
        "JSON array:"
    )


def _parse_actions(text):
    if not text:
        return []
    t = text.strip()
    t = re.sub(r"^```[a-zA-Z]*", "", t).replace("```", "").strip()
    # grab the outermost [...] block
    i, j = t.find("["), t.rfind("]")
    if i == -1 or j == -1 or j < i:
        return []
    blob = t[i:j + 1]
    try:
        arr = json.loads(blob)
    except Exception:
        # tolerate trailing commas / single quotes from small models
        try:
            arr = json.loads(re.sub(r",\s*([\]}])", r"\1", blob.replace("'", '"')))
        except Exception:
            return []
    if not isinstance(arr, list):
        return []
    out = []
    for a in arr:
        if isinstance(a, dict) and a.get("action"):
            pp = a.get("params")
            out.append({"action": str(a["action"]).strip(),
                        "params": pp if isinstance(pp, dict) else {},   # never let a str/list params crash a downstream .get
                        "why": str(a.get("why", ""))[:160]})
    return out


def decide(snap, c):
    prompt = _build_prompt(snap)
    model = operator_model(c)
    used, raw, err = model, "", None
    # RAM GUARD: driving Ollama makes it load a model resident (a 4B Q4 ~3.3 GB). On a small box that
    # can swap-death the machine. If free RAM is under the floor, DON'T load locally — prefer the cloud
    # tier, and if that's off, skip the tick with an honest reason instead of hanging the PC.
    ram_ok, ram_why = sysmem.can_load_local_model()
    if ablit_mod.ollama_up() and ram_ok:
        r = ablit_mod.chat(model, prompt, timeout=150)
        model_rank.record(model, r.get("ok"), r.get("ms"), kind="local")  # the only thing that makes ranking adapt
        if r.get("ok"):
            raw = r.get("text", "")
        else:
            err = r.get("error")
    elif not ram_ok and not c.get("allowCloud"):
        err = f"skipped — {ram_why}"
    elif c.get("allowCloud"):
        r = llm_mod.call_first_available(prompt, max_tokens=800, timeout=90)
        if r.get("ok"):
            raw, used = r.get("text", ""), (r.get("provider", "cloud"))
        else:
            err = r.get("error")
    else:
        err = "Ollama is down and cloud fallback is off - start Ollama or enable allowCloud."
    return {"model": used, "raw": raw, "error": err, "actions": _parse_actions(raw)}


# ---- one tick: decide, then execute each action via the injected actor --------------------------
def tick():
    if not _SENSOR or not _ACTOR:
        return {"ok": False, "error": "operator not wired (server must inject sensor+actor)"}
    c = cfg()
    snap = _SENSOR()
    d = decide(snap, c)
    results = []
    if d.get("error") and not d["actions"]:
        _write_audit({"action": "(think)", "ok": False, "error": d["error"], "model": d["model"]})
        return {"ok": False, "error": d["error"], "model": d["model"]}
    for a in d["actions"][: c["maxActionsPerTick"]]:
        name, params = a["action"], a["params"]
        if name not in _MANIFEST_NAMES:
            entry = {"action": name, "ok": False, "error": "not in manifest (ignored)", "why": a.get("why"), "model": d["model"]}
            results.append(_write_audit(entry)); continue
        if name == "note":
            results.append(_write_audit({"action": "note", "ok": True, "msg": str(params.get("text", ""))[:300], "model": d["model"]}))
            continue
        try:
            res = _ACTOR(name, params, "ai-operator") or {}
        except Exception as e:
            res = {"ok": False, "error": str(e)[:200]}
        results.append(_write_audit({"action": name, "params": params, "why": a.get("why"),
                                     "ok": bool(res.get("ok")), "msg": res.get("msg") or res.get("error"),
                                     "model": d["model"]}))
    if not d["actions"]:
        _write_audit({"action": "(idle)", "ok": True, "msg": "surveyed - nothing needed action", "model": d["model"]})
    return {"ok": True, "model": d["model"], "decided": len(d["actions"]), "results": results,
            "raw": (d.get("raw") or "")[:600]}


# ---- the loop + lifecycle ----------------------------------------------------------------------
def _loop():
    while not _KILL.is_set() and cfg().get("enabled"):
        try:
            tick()
        except Exception as e:
            _write_audit({"action": "(error)", "ok": False, "error": str(e)[:200]})
        iv = max(20, int(cfg().get("intervalSec", 90)))
        slept = 0
        while slept < iv:
            if _KILL.is_set() or not cfg().get("enabled"):   # kill switch takes effect within 2s
                return
            time.sleep(2); slept += 2


def start():
    global _thread
    _KILL.clear()
    set_cfg(enabled=True)
    with _thread_lock:                       # atomic check-and-spawn: no doubled loop / thread leak
        if _thread and _thread.is_alive():
            return status()
        _thread = threading.Thread(target=_loop, daemon=True)
        _thread.start()
    _write_audit({"action": "(engage)", "ok": True, "msg": "AI operator ENGAGED - full auto", "model": operator_model()})
    return status()


def stop():
    _KILL.set()                              # authoritative: a lost runtime.json write can't resurrect the loop
    set_cfg(enabled=False)
    _write_audit({"action": "(kill)", "ok": True, "msg": "AI operator stopped by kill switch"})
    return status()


def status():
    c = cfg()
    running = bool(_thread and _thread.is_alive() and not _KILL.is_set() and c.get("enabled"))
    ram_ok, ram_why = sysmem.can_load_local_model()   # so the UI can say WHY it won't load a local model
    return {**c, "running": running, "activeModel": operator_model(c),
            "ollama": ablit_mod.ollama_up(), "wired": bool(_SENSOR and _ACTOR),
            "ramOk": ram_ok, "ramWhy": ram_why, "memory": sysmem.status(),
            "manifest": [m["action"] for m in MANIFEST]}
