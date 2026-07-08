# approvals.py - human-in-the-loop approval queue for anything running OUTSIDE an interactive
# chat (mission runner, agent-failover, any background script). Interactive Claude Code
# sessions already ask permission per action - this fills the gap for headless/unattended runs,
# which previously used --permission-mode acceptEdits and never paused for Vaishak at all.
# A caller POSTs a request, then BLOCKS (polling this file) until he clicks Approve/Deny on
# the dashboard. Denied-or-timed-out NEVER silently proceeds - safe default is "don't."
import json, time, uuid
from pathlib import Path

FILE = Path(__file__).parent / "approvals.json"

def _load():
    try:
        d = json.loads(FILE.read_text(encoding="utf-8"))
        d.setdefault("items", [])
        d.setdefault("autoApprove", False)
        return d
    except Exception:
        return {"items": [], "autoApprove": False}

def get_config():
    d = _load()
    return {"autoApprove": bool(d.get("autoApprove"))}

def set_auto(on):
    d = _load()
    d["autoApprove"] = bool(on)
    _save(d)
    return {"autoApprove": d["autoApprove"]}

def approve_all():
    d = _load()
    n = 0
    for x in d["items"]:
        if x["status"] == "pending":
            x["status"] = "approved"
            x["respondedTs"] = time.time()
            n += 1
    _save(d)
    return {"approved": n}

def _save(d):
    FILE.write_text(json.dumps(d, indent=1), encoding="utf-8")

def list_pending():
    return [x for x in _load()["items"] if x["status"] == "pending"]

def list_all(limit=40):
    return sorted(_load()["items"], key=lambda x: x["ts"], reverse=True)[:limit]

def create(title, detail, who="system"):
    d = _load()
    # auto-approve mode: the gate is intentionally OFF - resolve immediately as approved.
    auto = bool(d.get("autoApprove"))
    item = {"id": uuid.uuid4().hex[:10], "title": title, "detail": detail, "who": who,
            "status": "approved" if auto else "pending", "ts": time.time()}
    if auto:
        item["respondedTs"] = time.time()
        item["autoApproved"] = True
    d["items"].append(item)
    d["items"] = d["items"][-200:]  # cap unbounded growth
    _save(d)
    return item

def respond(item_id, approved):
    d = _load()
    for x in d["items"]:
        if x["id"] == item_id and x["status"] == "pending":
            x["status"] = "approved" if approved else "denied"
            x["respondedTs"] = time.time()
            _save(d)
            return True
    return False

def get(item_id):
    for x in _load()["items"]:
        if x["id"] == item_id:
            return x
    return None
