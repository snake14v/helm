# budget.py - Token Governor: usage meter, budgets, burn-rate projection, emergency measures.
# HONESTY: everything is in TOKENS (real, from transcripts), never invented $ costs. Per-task
# "budgets" are PLANNING targets — transcripts aren't tagged per task, so actual per-task spend
# isn't measurable; the UI says so. Burn rate = today's tokens / hours-since-local-midnight
# (average pace, not instantaneous) — honest and enough for an end-of-day projection.
import json, time
from pathlib import Path

CFG = Path(__file__).parent / "budgets.json"
# Defaults sit ABOVE Vaishak's observed pace (~1M out/day, ~22M/week as of Jul 2026) so the
# meter starts healthy; tune DOWN on the Budget tab to make it bite.
DEFAULTS = {"dailyTokens": 3_000_000, "weeklyTokens": 40_000_000, "warnPct": 75, "critPct": 90}

def load():
    try:
        d = json.loads(CFG.read_text(encoding="utf-8"))
        return {**DEFAULTS, **d}
    except Exception:
        return dict(DEFAULTS)

def save(cfg):
    keep = {}
    for k in DEFAULTS:
        if k in cfg:
            v = int(cfg[k])
            if v <= 0:
                raise ValueError(f"{k} must be > 0")
            keep[k] = v
    merged = {**load(), **keep}
    CFG.write_text(json.dumps(merged, indent=1), encoding="utf-8")
    return merged

def _measures(sev):
    """Token-reduction levers, ranked. `active` marks the ones to push at this severity."""
    M = [
        ("Optical compression", "Render long docs/logs/reports to images Claude reads at fewer tokens.",
         "python ctx2img.py --dense <file>   (pages in ctx-pages/, attach those instead of the text)", "warn"),
        ("Offload menial work to local Gemma", "Zero cloud tokens for summaries, extraction, sanity checks.",
         "/council  (or ollama run gemma3:4b)  — free & local", "warn"),
        ("Compact the conversation", "Drop stale context so every following turn costs fewer input tokens.",
         "/compact", "warn"),
        ("Right-size the model", "Use Haiku for well-specified mechanical edits; reserve Opus/Fable for hard reasoning.",
         "switch model per task; workflows already route Fable=think / Opus=execute", "warn"),
        ("Batch, don't drip", "One turn with N questions reuses the prompt cache; N separate turns re-pay it.",
         "combine asks into a single message", "warn"),
        ("Pause background fleets", "The mission runner, fable-loop and deep-research spawn many agents each.",
         "stop MISSION-RUNNER.bat / hold /mission until the window resets", "critical"),
    ]
    order = {"ok": 0, "warn": 1, "critical": 2}
    return [{"title": t, "why": w, "how": h, "active": order[sev] >= order[trig]} for (t, w, h, trig) in M]

def governor(summary):
    cfg = load()
    today = (summary or {}).get("today", {}) or {}
    week = (summary or {}).get("week", {}) or {}
    # "usage" = output + cache-creation (the tokens you actually generate/pay attention-compute for);
    # input+cache-read is mostly cache hits, so output is the honest budget signal. Track output.
    dOut, wOut = today.get("out", 0), week.get("out", 0)
    lt = time.localtime()
    hrs_elapsed = max(0.15, lt.tm_hour + lt.tm_min / 60.0)  # since local midnight
    pace = dOut / hrs_elapsed                                # tokens/hour, average
    proj_eod = int(pace * 24)
    pctDay = round(100 * dOut / cfg["dailyTokens"], 1) if cfg["dailyTokens"] else 0
    pctWeek = round(100 * wOut / cfg["weeklyTokens"], 1) if cfg["weeklyTokens"] else 0
    pctProj = round(100 * proj_eod / cfg["dailyTokens"], 1) if cfg["dailyTokens"] else 0
    worst = max(pctDay, pctWeek)
    sev = "critical" if worst >= cfg["critPct"] else ("warn" if worst >= cfg["warnPct"] else "ok")
    # projection can flip severity to warn even if current is fine
    if sev == "ok" and pctProj >= cfg["critPct"]:
        sev = "warn"
    return {
        "budget": cfg,
        "today": {"out": dOut, "pct": pctDay},
        "week": {"out": wOut, "pct": pctWeek},
        "pacePerHour": int(pace), "projectedEod": proj_eod, "projectedPct": pctProj,
        "severity": sev,
        "headline": (f"{pctDay:.0f}% of today's token budget used"
                     + (f" · on pace for {pctProj:.0f}% by midnight" if pctProj > pctDay else "")),
        "measures": _measures(sev),
    }
