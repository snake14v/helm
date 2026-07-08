# doctor.py - self-troubleshooting engine for Mission Control.
# Every check returns {name, status: ok|warn|fail, detail, fix: str|None (what was applied)}.
# FIX POLICY (safety invariant): fixes may START things, RESTORE backups, or REBUILD our own
# cache files. They must NEVER stop/kill/recreate containers or processes, and never delete
# anything that is not created by Mission Control itself.
import json, os, shutil, subprocess, time, urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
CLAUDE = Path.home() / ".claude"
STATE = ROOT / "state.json"
CACHE = ROOT / "usage-cache.json"

def _http(url, timeout=1.5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return None

def _run(cmd, timeout=20):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, shell=False)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return -1, str(e)

def _have(exe):
    return shutil.which(exe) is not None

def _check_disk(fix):
    free_gb = shutil.disk_usage("C:\\").free / 1e9
    st = "ok" if free_gb > 15 else ("warn" if free_gb > 8 else "fail")
    detail = f"{free_gb:.1f} GB free"
    if st != "ok":
        detail += " - run cleanup.ps1 to reclaim safe caches (npm/npx/pip/temp/playwright), then consider RAM+SSD upgrade"
    return {"name": "disk C: free", "status": st, "detail": detail, "fix": None}

def _check_state(fix):
    bak = STATE.with_suffix(".json.bak")
    try:
        if STATE.exists():
            json.loads(STATE.read_text(encoding="utf-8"))
        return {"name": "state.json", "status": "ok", "detail": "valid", "fix": None}
    except Exception:
        pass
    applied = None
    if fix:
        quarantine = ROOT / f"state.corrupt-{int(time.time())}.json"
        try:
            if STATE.exists():
                shutil.copy(STATE, quarantine)
            if bak.exists():
                shutil.copy(bak, STATE)
                applied = f"restored from backup (corrupt copy kept as {quarantine.name})"
            else:
                STATE.write_text('{"tasks": [], "ratings": [], "events": []}', encoding="utf-8")
                applied = f"reset to empty (corrupt copy kept as {quarantine.name})"
        except Exception as e:
            applied = f"fix failed: {e}"
    return {"name": "state.json", "status": "fail" if not applied or "failed" in applied else "warn",
            "detail": "corrupt JSON", "fix": applied}

def _check_cache(fix):
    if not CACHE.exists():
        return {"name": "usage-cache", "status": "warn", "detail": "missing (first scan will rebuild)", "fix": None}
    try:
        sz = CACHE.stat().st_size / 1e6
        json.loads(CACHE.read_text(encoding="utf-8"))
        return {"name": "usage-cache", "status": "ok", "detail": f"valid, {sz:.1f} MB", "fix": None}
    except Exception:
        applied = None
        if fix:
            try:
                CACHE.unlink()
                applied = "deleted corrupt cache - next /api/summary rebuilds it"
            except Exception as e:
                applied = f"fix failed: {e}"
        return {"name": "usage-cache", "status": "warn", "detail": "corrupt", "fix": applied}

def _check_assets(fix):
    missing = [d for d in ("skills", "agents", "workflows", "hooks") if not (CLAUDE / d).is_dir()]
    if missing:
        return {"name": "claude assets", "status": "fail", "detail": f"missing dirs: {missing} - re-run infra-kit bootstrap", "fix": None}
    wf = len(list((CLAUDE / "workflows").glob("*.js")))
    return {"name": "claude assets", "status": "ok", "detail": f"skills/agents/workflows({wf})/hooks present", "fix": None}

def _check_ollama(fix):
    if _http("http://127.0.0.1:11434/api/tags", 1.0):
        return {"name": "ollama", "status": "ok", "detail": "API up", "fix": None}
    applied = None
    if fix and _have("ollama"):
        try:
            subprocess.Popen(["ollama", "serve"], creationflags=0x08000208,  # DETACHED|NO_WINDOW|NEW_GROUP
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(3)
            applied = "started `ollama serve`" + (" - now UP" if _http("http://127.0.0.1:11434/api/tags", 2.0) else " - still down, check ollama logs")
        except Exception as e:
            applied = f"fix failed: {e}"
    return {"name": "ollama", "status": "warn" if applied and "now UP" in applied else "fail",
            "detail": "API down", "fix": applied}

def _check_docker(fix):
    env = os.environ.copy()
    env["PATH"] = r"C:\Program Files\Docker\Docker\resources\bin;" + env.get("PATH", "")
    def docker(*args, t=15):
        try:
            p = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=t, env=env)
            return p.returncode, (p.stdout or "") + (p.stderr or "")
        except Exception as e:
            return -1, str(e)
    rc, _ = docker("info", t=10)
    if rc != 0:
        # Deliberately NOT auto-launched: starting Docker Desktop is heavy, has a known
        # Inference-manager crash bug, and is unsafe at low disk. Recommend instead.
        return {"name": "docker", "status": "fail", "detail": "engine down",
                "fix": None if not fix else "not auto-started (by policy) - run fix-docker-inference.ps1, then relaunch Docker Desktop"}
    # engine up -> ensure the two known containers are running (start only, never create/stop)
    rc2, out = docker("ps", "-a", "--format", "{{.Names}}|{{.State}}")
    started = []
    if rc2 == 0:
        states = dict(l.split("|", 1) for l in out.strip().splitlines() if "|" in l)
        for name in ("n8n", "crawl4ai-srv"):
            if name in states and states[name] != "running" and fix:
                if docker("start", name)[0] == 0:
                    started.append(name)
        detail = ", ".join(f"{n}:{states.get(n,'MISSING')}" for n in ("n8n", "crawl4ai-srv"))
    else:
        detail = "engine up, ps failed"
    note = (applied + "; " if applied else "") + (f"started {started}" if started else "")
    return {"name": "docker", "status": "ok" if rc == 0 else "fail", "detail": detail, "fix": note or None}

def _check_clis(fix):
    rows = []
    for exe, hint in (("codex", "npm i -g @openai/codex + codex login"),
                      ("agy", "install Antigravity CLI (see /council skill)"),
                      ("node", "winget install OpenJS.NodeJS.LTS"),
                      ("git", "winget install Git.Git")):
        rows.append(f"{exe}:{'ok' if _have(exe) else 'MISSING(' + hint + ')'}")
    misses = sum(1 for r in rows if "MISSING" in r)
    return {"name": "agent CLIs", "status": "ok" if misses == 0 else "warn",
            "detail": "  ".join(rows), "fix": None}

def _check_wio(fix):
    rc, out = _run(["powershell", "-NoProfile", "-Command",
                    "(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object CommandLine -match 'monitor.py' | Measure-Object).Count"])
    up = rc == 0 and out.strip() and int(out.strip().splitlines()[-1]) > 0
    return {"name": "wio bridge", "status": "ok" if up else "warn",
            "detail": "monitor.py running" if up else "not running (desk gadget idle - start manually; never auto-started, it grabs the serial port)", "fix": None}

CHECKS = [_check_disk, _check_state, _check_cache, _check_assets, _check_ollama, _check_docker, _check_clis, _check_wio]

def run_doctor(fix=False):
    t0 = time.time()
    results = []
    for c in CHECKS:
        try:
            results.append(c(fix))
        except Exception as e:
            results.append({"name": c.__name__, "status": "fail", "detail": f"check crashed: {e}", "fix": None})
    return {"results": results, "fixMode": fix, "tookMs": int((time.time() - t0) * 1000),
            "summary": {s: sum(1 for r in results if r["status"] == s) for s in ("ok", "warn", "fail")}}
