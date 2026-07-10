# guardian.py — HELM's EXTERNAL watchdog + full-revert safety net.
#
# Design rule: the thing that restores HELM must NOT share fate with HELM. So this file:
#   - imports ZERO HELM code (pure stdlib) — a bad AI fix that bricks HELM can't break the restorer;
#   - keeps cold source snapshots OUTSIDE the repo (%LOCALAPPDATA%\helm-guardian) — a git-reset or a
#     bad write inside the repo can't touch the backups;
#   - restores from a plain ZIP, NOT `git reset` — because git itself could be the thing that broke;
#   - makes even the revert reversible — before restoring it zips the CURRENT (broken) tree to
#     broken_<ts>.zip, so a bad restore loses nothing;
#   - is run as a SEPARATE process (GUARDIAN.bat copies it to the external store and runs THAT copy,
#     so the live watchdog isn't the in-repo file a fix might edit).
#
# Snapshots are CODE ONLY (*.py/*.html/*.bat/*.vbs/*.ico + integrations/, templates/). It never touches
# state.json / runtime.json / config.json — reverting a bad code fix must not roll back your real board
# data. Talks to HELM only over HTTP (health) + filesystem (snapshot/restore).
import glob, json, os, shutil, socket, subprocess, sys, time, urllib.request, zipfile
from datetime import datetime

HELM_DIR = os.environ.get("HELM_DIR", os.path.dirname(os.path.abspath(__file__)))
PORT = int(os.environ.get("HELM_PORT", "8799"))
STORE = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "helm-guardian")
SNAPS = os.path.join(STORE, "snapshots")
HEARTBEAT = os.path.join(STORE, "heartbeat.json")
KEEP = 24                      # rolling snapshots to retain
CODE_GLOBS = ("*.py", "*.html", "*.bat", "*.vbs", "*.ico")
CODE_DIRS = ("integrations", "templates")


def _log(msg):
    os.makedirs(STORE, exist_ok=True)
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    try:
        with open(os.path.join(STORE, "guardian.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line, flush=True)


def _code_files():
    """Source files worth snapshotting, as (abs_path, arcname) — code only, deduped by arcname."""
    seen = {}   # arcname -> abs_path (dedupe: guardian.py is caught by the glob AND added explicitly)
    for g in CODE_GLOBS:
        for p in glob.glob(os.path.join(HELM_DIR, g)):
            seen.setdefault(os.path.relpath(p, HELM_DIR), p)
    for d in CODE_DIRS:
        for p in glob.glob(os.path.join(HELM_DIR, d, "**", "*.*"), recursive=True):
            if "__pycache__" not in p:
                seen.setdefault(os.path.relpath(p, HELM_DIR), p)
    # snapshot guardian itself too (when run from the external store it's outside HELM_DIR)
    me = os.path.abspath(__file__)
    if os.path.isfile(me):
        seen.setdefault(os.path.basename(me), me)
    return [(ap, arc) for arc, ap in seen.items()]


def snapshot(label="auto"):
    os.makedirs(SNAPS, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)[:40]
    path = os.path.join(SNAPS, f"{datetime.now():%Y%m%d-%H%M%S}_{safe}.zip")
    files = _code_files()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for ap, arc in files:
            try:
                z.write(ap, arc)
            except Exception:
                pass
    # prune to KEEP most-recent
    snaps = sorted(glob.glob(os.path.join(SNAPS, "*.zip")), key=os.path.getmtime)
    for old in snaps[:-KEEP]:
        try: os.remove(old)
        except Exception: pass
    _log(f"SNAPSHOT {os.path.basename(path)} ({len(files)} files)")
    return path


def _http_ok(timeout=3):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/mode", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _compile_ok():
    """Do all of HELM's .py files parse? This is the #1 'bad fix bricks it' failure — caught WITHOUT
    importing them (import could execute code or fail on env). Returns (ok, first_bad_file, error)."""
    for p in glob.glob(os.path.join(HELM_DIR, "*.py")) + glob.glob(os.path.join(HELM_DIR, "integrations", "*.py")):
        try:
            compile(open(p, encoding="utf-8", errors="replace").read(), p, "exec")
        except SyntaxError as e:
            return False, os.path.basename(p), f"{e.msg} (line {e.lineno})"
        except Exception as e:
            return False, os.path.basename(p), str(e)[:120]
    return True, None, None


def health():
    comp_ok, bad, err = _compile_ok()
    http = _http_ok()
    ok = comp_ok and http
    return {"ok": ok, "http": http, "compiles": comp_ok, "badFile": bad, "error": err,
            "detail": ("healthy" if ok else
                       (f"SYNTAX BROKEN in {bad}: {err}" if not comp_ok else "server not responding")),
            "ts": int(time.time() * 1000)}


def _latest(label=None):
    snaps = sorted(glob.glob(os.path.join(SNAPS, "*.zip")), key=os.path.getmtime, reverse=True)
    if label and label not in ("latest", "auto"):
        snaps = [s for s in snaps if label in os.path.basename(s)]
    return snaps[0] if snaps else None


def restore(which="latest"):
    zpath = which if (which and which.endswith(".zip") and os.path.isfile(which)) else _latest(which)
    if not zpath:
        _log("RESTORE FAILED — no snapshot found"); return {"ok": False, "error": "no snapshot"}
    # make the revert itself reversible: cold-zip the current (broken) tree first
    broken = os.path.join(SNAPS, f"{datetime.now():%Y%m%d-%H%M%S}_broken.zip")
    try:
        with zipfile.ZipFile(broken, "w", zipfile.ZIP_DEFLATED) as z:
            for ap, arc in _code_files():
                try: z.write(ap, arc)
                except Exception: pass
    except Exception:
        broken = None
    # extract the good snapshot over the repo (code files only)
    with zipfile.ZipFile(zpath) as z:
        z.extractall(HELM_DIR)
    _log(f"RESTORE from {os.path.basename(zpath)} (broken tree saved to {os.path.basename(broken) if broken else 'n/a'})")
    return {"ok": True, "restored": os.path.basename(zpath), "brokenSaved": os.path.basename(broken) if broken else None}


def _restart_server():
    vbs = os.path.join(HELM_DIR, "start-server-hidden.vbs")
    try:
        if os.path.exists(vbs):
            subprocess.Popen(["wscript", vbs], cwd=HELM_DIR)
        else:
            subprocess.Popen([sys.executable, os.path.join(HELM_DIR, "server.py")], cwd=HELM_DIR,
                             creationflags=0x08000000)
        _log("server (re)start issued")
    except Exception as e:
        _log(f"restart failed: {e}")


def _write_heartbeat(state, extra=None):
    try:
        os.makedirs(STORE, exist_ok=True)
        snaps = glob.glob(os.path.join(SNAPS, "*.zip"))
        last = max(snaps, key=os.path.getmtime) if snaps else None
        json.dump({"ts": int(time.time() * 1000), "state": state,
                   "lastSnapshot": os.path.basename(last) if last else None,
                   "lastSnapshotAgeSec": int(time.time() - os.path.getmtime(last)) if last else None,
                   "snapshotCount": len(snaps), **(extra or {})},
                  open(HEARTBEAT, "w", encoding="utf-8"))
    except Exception:
        pass


def _assert_real_python():
    """FAIL LOUD if we're running under the Windows Store python alias.

    Store-packaged Python gets filesystem write virtualization: every write to %LOCALAPPDATA%\\helm-guardian
    is silently redirected into
      %LOCALAPPDATA%\\Packages\\PythonSoftwareFoundation.Python.3.x_...\\LocalCache\\Local\\helm-guardian
    So the watchdog would run happily while its snapshots, heartbeat and log land in a shadow folder that
    the dashboard and REVERT-HELM.bat never read — an armed-looking safety net that cannot actually save you.
    A guardian that might be mis-writing must refuse to run, not guess. (Found live 2026-07-10.)"""
    if "WindowsApps" in sys.executable or "\\Packages\\PythonSoftwareFoundation" in sys.executable:
        _log("FATAL: running under the Windows Store Python alias (%s).\n"
             "       Its filesystem virtualization would redirect snapshots/heartbeat to a shadow folder.\n"
             "       Re-run with a real interpreter, e.g.:  py -3 guardian.py watch\n"
             "       or  %%LOCALAPPDATA%%\\Programs\\Python\\Python312\\python.exe guardian.py watch"
             % sys.executable)
        sys.exit(2)


def watch(interval=20, fails_before_act=3):
    _assert_real_python()
    """The last-resort net: poll health; on sustained brick, restart, then restore+restart. Independent
    of HELM — runs even if every HELM subsystem is dead. Takes a snapshot whenever health is freshly good."""
    _log(f"WATCH start — HELM_DIR={HELM_DIR} port={PORT} store={STORE}")
    snapshot("watch-start")
    bad = 0
    prev_ok = True
    while True:
        h = health()
        _write_heartbeat("watching", {"helm": h})
        if h["ok"]:
            if not prev_ok:
                _log("HELM recovered"); snapshot("post-recovery")
            bad, prev_ok = 0, True
        else:
            bad += 1; prev_ok = False
            _log(f"UNHEALTHY ({bad}/{fails_before_act}): {h['detail']}")
            if bad == fails_before_act:
                if not h["compiles"]:
                    _log("code is broken -> RESTORING last good snapshot"); restore("latest"); _restart_server()
                else:
                    _log("code ok but server down -> restarting"); _restart_server()
            elif bad > fails_before_act + 3:
                _log("still down after restore+restart -> restoring again"); restore("latest"); _restart_server(); bad = fails_before_act
        time.sleep(interval)


def _usage():
    print("guardian.py — HELM external watchdog + full-revert (imports no HELM code)\n"
          "  python guardian.py snapshot [label]   take a cold code snapshot now\n"
          "  python guardian.py health             is HELM alive + do all .py compile?\n"
          "  python guardian.py list               list snapshots (newest first)\n"
          "  python guardian.py revert [latest|<substr>|<path.zip>]   full restore + saves broken tree\n"
          "  python guardian.py watch              run the watchdog loop (GUARDIAN.bat runs this)\n"
          f"  store: {STORE}")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    a = sys.argv[1:]
    if not a:
        _usage(); sys.exit(0)
    cmd = a[0]
    if cmd == "snapshot":
        print(snapshot(a[1] if len(a) > 1 else "manual"))
    elif cmd == "health":
        print(json.dumps(health(), indent=1))
    elif cmd == "list":
        for s in sorted(glob.glob(os.path.join(SNAPS, "*.zip")), key=os.path.getmtime, reverse=True):
            print(f"  {datetime.fromtimestamp(os.path.getmtime(s)):%Y-%m-%d %H:%M:%S}  {os.path.basename(s)}")
    elif cmd == "revert":
        print(json.dumps(restore(a[1] if len(a) > 1 else "latest"), indent=1))
    elif cmd == "watch":
        try: watch()
        except KeyboardInterrupt: _log("WATCH stopped")
    else:
        _usage()
