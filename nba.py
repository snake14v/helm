# nba.py - Next Best Action engine. PURE function: given the signals the server already
# computes, return a ranked list of concrete actions. Higher score = do it sooner.
# Each action: {score, title, why, how, key}. `key` is a STABLE id (category-based, not the
# dynamic title) so the server can apply the user's manual pin/priority order across polls.

def rank(work, services, state, mem_days, dirty, runs, gov=None):
    A = []
    def add(score, title, why, how, key):
        A.append({"score": score, "title": title, "why": why, "how": how, "key": key})

    # --- token budget (emergency-aware) ---
    if gov and gov.get("severity") == "critical":
        add(92, f"Token budget critical — {gov['today']['pct']:.0f}% of daily used",
            "You're near the day's token ceiling; keep working but cut spend now.",
            "Emergency levers on the Budget tab: optical-compress docs, offload menial work to Gemma, /compact.", "budget")
    elif gov and gov.get("severity") == "warn":
        add(58, f"Token pace high — {gov.get('projectedPct',0):.0f}% projected by midnight",
            "At the current pace you'll approach the daily budget.",
            "Consider Gemma for menial tasks + ctx2img for long docs (Budget tab).", "budget")

    svc = {s["name"]: s.get("up") for s in (services or [])}
    tasks = (state or {}).get("tasks", [])
    doing = [t for t in tasks if t.get("col") == "doing"]
    review = [t for t in tasks if t.get("col") == "review"]

    # --- stuck tasks (silently-stalled missions) ---
    try:
        import taskhealth
        _h = taskhealth.classify(state)
        _hi = _h["counts"].get("high", 0)
        if _hi:
            top = next((s for s in _h["stuck"] if s["severity"] == "high"), None)
            add(90, f"{_hi} task(s) stuck & need you — e.g. \"{(top['text'][:44]) if top else ''}\"",
                (top["reason"] if top else "A mission stalled and won't auto-retry."),
                "Overview → 'Stuck & needs your attention' panel: one-click Retry / Mark done / Remove.", "stuck")
    except Exception:
        pass

    # --- infra fires (highest: nothing else works if infra is down) ---
    if svc.get("n8n") is False or svc.get("crawl4ai") is False:
        add(95, "Bring infra back up",
            "n8n/Crawl4AI are down — automations and scraping can't run.",
            'Run: & "$env:USERPROFILE\\Desktop\\automation\\fix-docker-inference.ps1"  then relaunch Docker Desktop', "infra")

    # --- WIP overload (focus killer) ---
    if len(doing) > 2:
        add(88, f"Finish work — {len(doing)} cards in Doing (limit 2)",
            "Too many parallel starts; throughput dies past a WIP limit.",
            "Move a Doing card to Review/Done before starting anything new (Board tab).", "wip")

    # --- review backlog ---
    if len(review) >= 3:
        add(70, f"Clear the review column ({len(review)} waiting)",
            "Cards stuck in Review are finished work not yet shipped.",
            "Verify + move them to Done, or send codex a diff review via /council.", "review")

    # --- dormant venture (the guilt-trip, data-driven) ---
    for v in (work or {}).get("ventures", []):
        if v.get("week", 0) == 0:
            add(72, f"Decide on {v['name']} — 0 commits in 7 days",
                f"Last commit: {v.get('lastAgo','?')}. Dormant repos rot; commit or archive.",
                f"Open {v['name']}, ship one small commit — or add a 'kill or continue' card to the board.", "dormant:" + v['name'])
        elif v.get("today", 0) == 0 and v.get("week", 0) >= 15:
            add(40, f"Keep {v['name']} moving (hot repo, nothing today)",
                f"{v['week']} commits this week but none today — don't break the streak.",
                f"Ship one commit in {v['name']} to hold momentum.", "hot:" + v['name'])

    # --- uncommitted work (real risk of loss) ---
    for repo in (dirty or []):
        add(66, f"Commit WIP in {repo['name']} ({repo['files']} files dirty)",
            "Uncommitted changes are unbacked-up work — a bad `git checkout` loses them.",
            f"cd into {repo['name']} and commit or stash.", "dirty:" + repo['name'])

    # --- ship-a-commit nudge if nothing today ---
    if (work or {}).get("commitsToday", 0) == 0:
        add(55, "Ship at least one commit today",
            "The ship streak counts days with >=1 commit; today has none yet.",
            "Any real change committed to any venture repo keeps the streak alive.", "ship")

    # --- stale memory ---
    if mem_days is not None and mem_days > 7:
        add(45, f"Retrain memory ({mem_days} days stale)",
            "Claude's domain memory drifts from the code after a sprint.",
            "Run the claude-retrain workflow (Launch tab).", "memory")

    # --- incomplete workflow runs (ties to feature #1) ---
    for r in (runs or []):
        if r.get("resumable") and not r.get("live"):
            add(50, f"Resume workflow {r['id'][:10]} ({r.get('agents',0)} agents)",
                "A multi-agent run stopped mid-flight; resuming reuses cached results (saves tokens).",
                r.get("resumeCmd", "see Agents tab"), "resume")
            break  # only surface the most recent

    A.sort(key=lambda x: x["score"], reverse=True)
    return A[:12]  # server truncates to top 10 AFTER applying manual pins
