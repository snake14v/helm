# bridge.py — the transfer slot between Claude Code/Desktop and HELM.
#
# Indexes the workflows happening in Claude sessions (~/.claude/projects/**/*.jsonl) into a structured
# list, extracts what each session actually DID (the user intents = the workflow, the files it built,
# the tools it drove), and captures it as a portable spec HELM can act on. The privileged transfer
# endpoints (capture/push) create HELM missions directly — the "full permissions slot" — so work moves
# here -> HELM -> back here without a copy-paste. Reads transcripts cheaply (filters lines before JSON
# parsing) so a 46 MB session doesn't stall. No HELM imports (server wires the state side).
import glob, json, os, time
from pathlib import Path

PROJECTS = Path.home() / ".claude" / "projects"
ROOT = os.path.dirname(os.path.abspath(__file__))
CAPTURES = os.path.join(ROOT, "bridge")           # captured workflow specs land here (gitignored)
FILE_TOOLS = {"Edit", "Write", "Read", "NotebookEdit"}


def _tail(path, nbytes=262144):
    try:
        with open(path, "rb") as f:
            sz = os.fstat(f.fileno()).st_size
            f.seek(max(0, sz - nbytes))
            return f.read().decode("utf-8", "replace")
    except Exception:
        return ""


def _head_first_user(path, nbytes=131072):
    """First real user request in the session — the workflow's opening intent."""
    try:
        with open(path, "rb") as f:
            chunk = f.read(nbytes).decode("utf-8", "replace")
    except Exception:
        return ""
    for ln in chunk.splitlines():
        if '"type":"user"' not in ln:
            continue
        try:
            c = (json.loads(ln).get("message") or {}).get("content")
        except Exception:
            continue
        if isinstance(c, str) and c.strip():
            return c.strip()[:200]
        if isinstance(c, list):
            for x in c:
                if isinstance(x, dict) and x.get("type") == "text" and x.get("text", "").strip():
                    return x["text"].strip()[:200]
    return ""


def _title(path):
    """Prefer Claude Code's own ai-title (latest one), else the first user request."""
    for ln in reversed(_tail(path).splitlines()):
        if '"type":"ai-title"' in ln:
            try:
                o = json.loads(ln)
                t = o.get("title") or (o.get("message") or {}).get("content")
                if isinstance(t, str) and t.strip():
                    return t.strip()[:90]
            except Exception:
                pass
    return (_head_first_user(path) or "(untitled session)")[:90]


def _pretty_project(dirname):
    return dirname.replace("C--Users-VAISHAK-", "").replace("C--", "").replace("-", " ").strip() or "root"


def index_sessions(hours=336, limit=40):
    """The workflow index: every recent Claude session, newest first, with a title + activity."""
    now = time.time()
    out = []
    for f in PROJECTS.glob("*/*.jsonl"):
        try:
            m = f.stat().st_mtime; sz = f.stat().st_size
        except OSError:
            continue
        if now - m > hours * 3600 or sz < 500:
            continue
        out.append({"id": f.stem, "project": _pretty_project(f.parent.name),
                    "title": _title(str(f)), "ageSec": int(now - m),
                    "sizeMB": round(sz / 1e6, 1), "path": str(f),
                    "active": (now - m) < 300})
    out.sort(key=lambda x: x["ageSec"])
    return out[:limit]


def _find(session_id):
    hits = list(PROJECTS.glob(f"*/{session_id}*.jsonl"))
    return str(hits[0]) if hits else None


def session_workflow(session_id, max_intents=40):
    """Extract the WORKFLOW from a session: the ordered user intents (what was asked), the files it
    built/touched, and the tools it drove. This is the portable spec that transfers to HELM."""
    path = _find(session_id)
    if not path:
        return {"error": "session not found"}
    intents, files, tools = [], {}, {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for ln in f:
                # cheap prefilter: only parse lines that carry an intent or a tool call
                if '"type":"user"' not in ln and '"type":"assistant"' not in ln:
                    continue
                try:
                    o = json.loads(ln)
                except Exception:
                    continue
                t = o.get("type"); c = (o.get("message") or {}).get("content")
                if t == "user":
                    txt = c if isinstance(c, str) else next(
                        (x.get("text") for x in c if isinstance(x, dict) and x.get("type") == "text"), None
                    ) if isinstance(c, list) else None
                    # skip tool_result echoes + local-command noise
                    if txt and txt.strip() and not txt.lstrip().startswith(("<", "[Request")):
                        s = " ".join(txt.split())[:180]
                        if s and (not intents or intents[-1] != s):
                            intents.append(s)
                elif t == "assistant" and isinstance(c, list):
                    for x in c:
                        if isinstance(x, dict) and x.get("type") == "tool_use":
                            nm = x.get("name", "?"); tools[nm] = tools.get(nm, 0) + 1
                            if nm in FILE_TOOLS:
                                fp = (x.get("input") or {}).get("file_path")
                                if fp:
                                    files[fp] = files.get(fp, 0) + 1
    except Exception as e:
        return {"error": str(e)[:150]}
    built = sorted(files.items(), key=lambda kv: -kv[1])
    return {"id": session_id, "title": _title(path), "path": path,
            "intents": intents[-max_intents:], "intentCount": len(intents),
            "files": [{"file": k, "edits": v} for k, v in built[:30]],
            "tools": sorted(tools.items(), key=lambda kv: -kv[1])}


def capture_doc(wf):
    """Write a portable markdown workflow spec into bridge/ and return its path + a mission text."""
    os.makedirs(CAPTURES, exist_ok=True)
    sid = wf.get("id", "session")
    p = os.path.join(CAPTURES, f"{sid}.md")
    lines = [f"# Workflow capture — {wf.get('title','')}", "",
             f"Source session `{sid}` · {wf.get('intentCount',0)} intents · captured {time.strftime('%Y-%m-%d %H:%M')}", "",
             "## What was asked (the workflow)"]
    for i, s in enumerate(wf.get("intents", []), 1):
        lines.append(f"{i}. {s}")
    if wf.get("files"):
        lines += ["", "## Files built / touched"] + [f"- `{x['file']}` ({x['edits']}×)" for x in wf["files"]]
    if wf.get("tools"):
        lines += ["", "## Tools used", ", ".join(f"{n}×{c}" for n, c in wf["tools"][:12])]
    open(p, "w", encoding="utf-8").write("\n".join(lines))
    last = (wf.get("intents") or ["(no intents)"])[-1]
    mission = f"Continue the '{wf.get('title','session')}' workflow — last step: {last[:120]}"
    return {"doc": p, "mission": mission, "title": wf.get("title", "")}
