# providers.py - honest per-provider status. Claude/Codex/Antigravity are account-plan auth
# (OAuth/ChatGPT/Google), NOT metered API keys - none expose a local "X% of quota" number via
# CLI (verified: `codex doctor` shows auth mode=chatgpt; `agy --help` has no usage subcommand).
# This module reports what IS locally measurable and is explicit about what isn't - it never
# invents a percentage. Codex/Antigravity checks spawn processes, so they're cached (TTL) and
# only forced on manual refresh, never auto-polled hard.
import json, os, shutil, subprocess, time, urllib.request

TTL = 120
_cache = {"ts": 0, "data": None}

def _run(cmd, timeout=10):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, shell=False)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return -1, str(e)

def _oauth_token_set():
    # EXISTENCE check only - never capture/return the token value itself.
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return True
    try:
        p = subprocess.run(["reg", "query", r"HKCU\Environment", "/v", "CLAUDE_CODE_OAUTH_TOKEN"],
                           capture_output=True, timeout=5)
        return p.returncode == 0
    except Exception:
        return False

def _codex():
    # KNOWN QUIRK (confirmed 2026-07-07): processes spawned via wscript.exe/WshShell.Run
    # (this dashboard's own boot chain) sometimes can't see recently-added files under
    # AppData\Roaming\npm (os.path.isfile AND cmd.exe both fail on the identical absolute
    # path that an interactive PowerShell resolves fine) - a Windows per-process file
    # visibility issue, not a real absence. Try direct exec (not a pre-check) and if THAT
    # fails, report "unknown" rather than a false "not installed".
    codex = shutil.which("codex") or os.path.join(os.environ.get("APPDATA", ""), "npm", "codex.cmd")
    rc, out = _run([codex, "login", "status"])
    if rc == -1 and ("cannot find" in out.lower() or "not recognized" in out.lower() or "No such file" in out):
        return {"installed": None,
                "note_override": "Could not verify from this background service (a known Windows file-visibility quirk affects wscript-spawned processes) - Codex works fine from an interactive terminal."}
    return {"installed": True, "authed": (rc == 0) or ("Logged in" in out)}

def _antigravity():
    agy = os.path.join(os.environ.get("LOCALAPPDATA", ""), "agy", "bin", "agy.exe")
    if not os.path.isfile(agy):
        return {"installed": False}
    rc, out = _run([agy, "--version"])
    return {"installed": True, "version": out.strip()[:40] if rc == 0 else "unknown"}

def _gemma():
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=2) as r:
            models = [m.get("name") for m in json.loads(r.read()).get("models", [])]
        loaded = []
        try:
            with urllib.request.urlopen("http://127.0.0.1:11434/api/ps", timeout=2) as r2:
                loaded = [m.get("name") for m in json.loads(r2.read()).get("models", [])]
        except Exception:
            pass
        return {"installed": True, "models": models, "loaded": loaded}
    except Exception:
        return {"installed": False}

def _with_note(d, default_note):
    d = dict(d)
    note = d.pop("note_override", default_note)
    return {**d, "note": note}

def status(force=False):
    if not force and _cache["data"] and (time.time() - _cache["ts"] < TTL):
        return _cache["data"]
    data = {
        "claude": {
            "oauthTokenSet": _oauth_token_set(),
            "measured": "output-token burn tracked live from transcripts (see Governor above)",
            "note": "No local quota % exists for account-plan auth - run /usage-credits in a Claude Code session for the exact number + reset time.",
        },
        "codex": _with_note(_codex(), "ChatGPT-plan auth (auth mode=chatgpt, verified via codex doctor) - no local quota API. Check platform.openai.com/usage or your ChatGPT plan page."),
        "antigravity": {**_antigravity(),
                        "note": "Google-account auth - agy has no usage subcommand. Check your Antigravity/Gemini plan page."},
        "gemma": {**_gemma(),
                  "note": "Local model via Ollama - no cloud quota at all; bounded only by this PC's RAM/VRAM."},
    }
    _cache["ts"], _cache["data"] = time.time(), data
    return data
