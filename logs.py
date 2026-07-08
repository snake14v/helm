# logs.py - the "what actually happened" engine for the Logs tab. Surfaces (1) full approval
# history + stats, and (2) tails of every log file this stack writes, each with a plain-English
# explanation so Vaishak knows what he's looking at. Read-only, sandboxed to the mission-control
# dir. No subprocess (per the wscript-spawn-chain quirk that bit terminals.py + codex detection).
import json, time, glob, os
from pathlib import Path

ROOT = Path(__file__).parent
APPROVALS = ROOT / "approvals.json"

def _tail(path, n=60, maxbytes=200_000):
    try:
        p = Path(path)
        size = p.stat().st_size
        with open(p, "rb") as f:
            if size > maxbytes:
                f.seek(size - maxbytes)
            data = f.read().decode("utf-8", errors="replace")
        lines = data.splitlines()
        return lines[-n:]
    except Exception:
        return []

def approval_activity():
    try:
        items = json.loads(APPROVALS.read_text(encoding="utf-8")).get("items", [])
    except Exception:
        items = []
    stats = {"total": len(items), "approved": 0, "denied": 0, "pending": 0, "auto": 0}
    for x in items:
        s = x.get("status", "")
        if s == "approved":
            stats["approved"] += 1
            if x.get("autoApproved"):
                stats["auto"] += 1
        elif s == "denied":
            stats["denied"] += 1
        elif s == "pending":
            stats["pending"] += 1
    # newest first, with a human one-liner per row
    rows = []
    for x in sorted(items, key=lambda z: z.get("ts", 0), reverse=True)[:60]:
        s = x.get("status", "?")
        verb = {"approved": "you approved" if not x.get("autoApproved") else "auto-approved",
                "denied": "you denied", "pending": "waiting for you"}.get(s, s)
        wait = ""
        if x.get("respondedTs") and x.get("ts"):
            wait = f" ({int(x['respondedTs'] - x['ts'])}s to decide)"
        rows.append({
            "title": x.get("title", ""), "detail": x.get("detail", ""),
            "who": x.get("who", ""), "status": s, "explain": verb + wait,
            "ts": x.get("ts", 0),
        })
    return {"stats": stats, "rows": rows}

# Known log files + what each one MEANS (so the page explains itself)
LOG_DEFS = [
    {"key": "runner", "glob": "runner-logs/runner-*.log", "latest": True,
     "label": "Mission runner", "explain": "The unattended runner's own diary: which missions it picked up, when it PAUSED for your approval, approved/denied, and pass/fail."},
    {"key": "failover", "glob": "failover-logs/failover-*.log", "latest": True,
     "label": "Agent failover", "explain": "When Claude ran out of tokens, this shows the switch-over chain (Claude->Codex->Gemma) and which switches you approved."},
    {"key": "daylaunch", "glob": "day-launcher.log", "latest": False,
     "label": "Day launcher", "explain": "What started up when you last picked a day-tier (Claude / Codex / Gemma / Full company)."},
    {"key": "mission", "glob": "runner-logs/mission-*.log", "latest": True,
     "label": "Last mission output", "explain": "The raw output of the last headless mission Claude actually executed."},
]

def log_files():
    out = []
    for d in LOG_DEFS:
        matches = sorted(glob.glob(str(ROOT / d["glob"])), key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0)
        if not matches:
            out.append({**{k: d[k] for k in ("key", "label", "explain")}, "file": None, "lines": []})
            continue
        target = matches[-1] if d["latest"] else matches[0]
        out.append({**{k: d[k] for k in ("key", "label", "explain")},
                    "file": os.path.basename(target), "lines": _tail(target)})
    return out
