# taskhealth.py - detect & explain STUCK tasks, so nothing silently rots in limbo again.
# The trigger case (2026-07-07): an AutoPalette mission went 'partial - WhatsApp send paused for
# confirmation' and sat in backlog forever, because the runner's own eligibility filter EXCLUDES
# 'partial'/'blocked'/'held' - correct (don't auto-rerun a half-done mission) but it left the task
# invisible with no "waiting on you" surface. This classifies every stuck task, says WHY in plain
# English, and hands the UI one-click mitigations (retry / mark-done / remove).
import time

# The runner's eligibility rule, mirrored so we can explain exactly why a mission is skipped.
_SKIP_STATUS = ("blocked", "dispatched", "partial", "held", "awaiting", "not approved", "needs human", "runner-failed", "cancelled")

def _is_runner_eligible(t):
    if not t.get("mission") or t.get("col") != "backlog":
        return False
    st = (t.get("status") or "").lower()
    if any(k in st for k in _SKIP_STATUS):
        return False
    att = t.get("runnerAttempts") or 0
    return att < 2

def classify(state, now=None):
    now = now or time.time()
    tasks = (state or {}).get("tasks", [])
    by_id = {t.get("id"): t for t in tasks}
    stuck = []

    def flag(t, category, severity, reason, mitig):
        stuck.append({
            "id": t.get("id"), "text": t.get("text", ""), "col": t.get("col"),
            "status": t.get("status", ""), "attempts": t.get("runnerAttempts") or 0,
            "category": category, "severity": severity, "reason": reason,
            "mitigations": mitig,  # list of {label, action} the UI turns into buttons
        })

    RETRY = {"label": "↻ Retry", "action": "retry"}
    DONE = {"label": "✓ Mark done", "action": "done"}
    REMOVE = {"label": "🗑 Remove", "action": "remove"}

    for t in tasks:
        st = (t.get("status") or "").lower()
        col = t.get("col")
        att = t.get("runnerAttempts") or 0

        # --- missions ---
        if t.get("mission"):
            if col == "done":
                continue  # healthy
            if col == "backlog":
                if "partial" in st or "paused" in st or "awaiting" in st or "confirmation" in st:
                    flag(t, "awaiting-human", "high",
                         f"Half-finished and waiting on YOU - the runner will never auto-retry it (status: '{t.get('status')}'). Something in this mission needs your confirmation.",
                         [DONE, RETRY, REMOVE])
                elif "blocked" in st:
                    flag(t, "blocked", "high",
                         f"Blocked and skipped by the runner (status: '{t.get('status')}'). Resolve the blocker, then Retry.",
                         [RETRY, DONE, REMOVE])
                elif "held" in st or "not approved" in st:
                    flag(t, "held", "med",
                         "You (or a timeout) denied its approval, so it's holding. Retry to re-queue it.",
                         [RETRY, REMOVE])
                elif att >= 2 or "runner-failed" in st:
                    flag(t, "retries-exhausted", "high",
                         f"Failed {att} times and gave up (status: '{t.get('status')}'). Check the mission log, then Retry or Remove.",
                         [RETRY, REMOVE])
                # else: a clean queued mission - healthy, will be picked up
            elif col in ("doing", "review"):
                age_min = int((now - (t.get("ts") or now)) / 60)
                if col == "doing":
                    flag(t, "in-progress", "low",
                         "In progress. If no runner/agent is actually active, it may be stranded here - Retry re-queues it.",
                         [RETRY, DONE])
                else:  # review
                    flag(t, "awaiting-review", "med",
                         "Done by an agent but parked in Review awaiting your verify. Mark done to close it.",
                         [DONE, RETRY])

        # --- orphan jobs: a child whose parent mission is missing or stuck ---
        else:
            parent = t.get("parent")
            if parent and col != "done":
                pm = by_id.get(parent)
                if pm is None:
                    flag(t, "orphan-job", "med",
                         "This job's parent mission is gone from the board - it's orphaned. Mark done or Remove.",
                         [DONE, REMOVE])

    order = {"high": 0, "med": 1, "low": 2}
    stuck.sort(key=lambda x: order.get(x["severity"], 3))
    counts = {"high": 0, "med": 0, "low": 0}
    for s in stuck:
        counts[s["severity"]] = counts.get(s["severity"], 0) + 1
    return {"stuck": stuck, "counts": counts,
            "eligibleQueued": sum(1 for t in tasks if _is_runner_eligible(t))}
