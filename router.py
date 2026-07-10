# router.py — task router + provider relay for HELM.
#
# The point: stop defaulting to Claude. A cheap LOCAL model (Gemma via Ollama) classifies each task,
# picks the cheapest CAPABLE provider (ranked live by model_rank), runs it, then RELAYS the output to
# the next step carrying context: the project's code-wiki (CLAUDE.md/README), git state (what's left),
# and a running ledger (what's done). Work gets done on the right model, not the expensive default.
#
# Not a new framework — it assembles primitives that already exist:
#   ablit.classify (local router brain) · model_rank (which provider wins) · llm_providers.call
#   (call anything) · the git helpers (repo state) · agent-failover.ps1 (the file-editing agents).
# No new deps. CLI ("one terminal, all providers") + importable + wired to /api/route,/api/relay.
import json, os, subprocess, sys, time
import llm_providers as llm
import model_rank
import ablit

ROOT = os.path.dirname(os.path.abspath(__file__))

# Capability class -> candidate cloud providers, cheapest/most-appropriate first (model_rank re-orders
# live by real success/latency; unknown or unkeyed providers are skipped, so adding 'xai' later Just Works).
TIERS = {
    "code":   ["xai", "groq", "cerebras", "openrouter", "sambanova"],   # write/review code (as text)
    "reason": ["openrouter", "xai", "gemini", "moonshot", "mistral"],   # plan, analyze, trade-offs, R&D
    "cheap":  ["groq", "cerebras", "openrouter"],                       # summarize, classify, short answers
    "vision": ["gemini"],                                               # image understanding
}
AGENTIC = ["codex", "claude"]   # the ONLY ones that edit files/run commands — Codex first, Claude last.

_CLASSES = [
    {"id": "agentic", "label": "edit the repo", "desc": "actually change files, run commands, build/fix code in the project"},
    {"id": "code",    "label": "write or review code", "desc": "generate a function, review a diff, write a snippet as text (no file edits)"},
    {"id": "reason",  "label": "plan / analyze / research", "desc": "design, architecture, trade-offs, R&D, explain, decide"},
    {"id": "cheap",   "label": "simple / mechanical", "desc": "summarize, rename, classify, one-line answer, format"},
    {"id": "vision",  "label": "look at an image", "desc": "understand a screenshot, diagram or photo"},
]


def classify_task(task):
    """Local Gemma picks the capability class (free, on-device). Falls back to 'reason' if Ollama is down."""
    r = ablit.classify(task, _CLASSES) if ablit.ollama_up() else None
    cls = (r or {}).get("venture")
    return cls if cls in {c["id"] for c in _CLASSES} else "reason"


def pick_provider(cls):
    """Cheapest CAPABLE, currently-available provider for the class — ranked live, never Claude for advisory."""
    if cls == "agentic":
        return {"mode": "agentic", "candidates": AGENTIC}
    # 'cheap' work goes to free local Gemma first when Ollama is up — max efficiency.
    if cls == "cheap" and ablit.ollama_up():
        return {"mode": "local", "provider": "gemma(local)"}
    keyed = [p for p in TIERS.get(cls, TIERS["reason"]) if p in llm.PROVIDERS and llm._key(llm.PROVIDERS[p]["env"])]
    if not keyed:
        return {"mode": "local", "provider": "gemma(local)"} if ablit.ollama_up() else {"mode": "none"}
    best = model_rank.rank(keyed, kind="cloud", fallback_order=keyed)[0]
    return {"mode": "cloud", "provider": best}


def run(task, context="", max_tokens=1500):
    """Route ONE task to the picked provider and return {class, provider, ok, text, ms}."""
    cls = classify_task(task)
    pick = pick_provider(cls)
    prompt = f"{context}\n\n=== YOUR TASK ===\n{task}" if context else task
    if pick["mode"] == "local":
        m = ablit.pick_model()
        r = ablit.chat(m, prompt, timeout=120)
        return {"class": cls, "provider": f"gemma:{m}", "mode": "local",
                "ok": r.get("ok"), "text": r.get("text"), "ms": r.get("ms")}
    if pick["mode"] == "agentic":
        # Advisory return here is a PLAN + the exact failover command to actually edit files (Codex-first,
        # Claude skipped). Real file edits run out-of-band through the already-verified chain, not inline.
        cmd = f'agent-failover.ps1 -SkipClaude -Cwd "<repo>" -Mission "router" -Prompt "{task[:80]}..."'
        plan = run(f"Break this coding task into an ordered checklist a coding agent can execute:\n{task}",
                   context)  # cheap model drafts the checklist
        return {"class": "agentic", "provider": "codex (via failover)", "mode": "agentic",
                "ok": True, "text": (plan.get("text") or ""), "handoff": cmd,
                "note": "file-editing task — run the handoff command to execute via Codex."}
    if pick["mode"] == "none":
        return {"class": cls, "provider": None, "ok": False,
                "text": None, "error": "no provider available (no cloud key + Ollama down)"}
    r = llm.call(pick["provider"], prompt, max_tokens=max_tokens, timeout=90)
    return {"class": cls, "provider": pick["provider"], "mode": "cloud",
            "ok": r.get("ok"), "text": r.get("text"), "ms": r.get("ms"), "error": r.get("error")}


# ---- context carriers: the "code wiki + git + what's left" the baton carries ----
def wiki_context(repo, limit=4000):
    """The project's code-wiki context — first of CLAUDE.md / AGENTS.md / docs/wiki / README."""
    if not repo:
        return ""
    for p in ("CLAUDE.md", "AGENTS.md", os.path.join("docs", "wiki", "README.md"), "README.md"):
        fp = os.path.join(repo, p)
        if os.path.isfile(fp):
            try:
                return f"[{p}]\n" + open(fp, encoding="utf-8", errors="replace").read()[:limit]
            except Exception:
                pass
    return ""


def git_context(repo):
    """Git state = 'what's left': branch, uncommitted files, recent commits."""
    def g(*a):
        try:
            return subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True,
                                  timeout=10, encoding="utf-8", errors="replace").stdout.strip()
        except Exception:
            return ""
    if not repo or not os.path.isdir(os.path.join(repo, ".git")):
        return ""
    dirty = g("status", "--porcelain")
    return (f"GIT: branch {g('rev-parse', '--abbrev-ref', 'HEAD') or '?'}; "
            + (f"{len(dirty.splitlines())} uncommitted file(s)" if dirty else "clean") + ".\n"
            + "recent:\n" + (g("log", "--oneline", "-5") or "(no commits)"))


def _relay_context(ledger):
    parts = []
    if ledger.get("wiki"):
        parts.append("CODE / WIKI CONTEXT:\n" + ledger["wiki"])
    if ledger.get("git"):
        parts.append(ledger["git"])
    if ledger["done"]:
        parts.append("DONE SO FAR (previous providers' output):\n" +
                     "\n".join(f"[{d['provider']}] {d['step']}\n{(d['output'] or '')[:600]}" for d in ledger["done"]))
    if ledger["remaining"]:
        parts.append("STILL TO DO AFTER THIS STEP:\n" + "\n".join(f"- {s}" for s in ledger["remaining"]))
    return "\n\n".join(parts)


def relay(goal, steps, repo=None):
    """Run steps in sequence, each on its best-picked provider, passing context forward. Returns the ledger."""
    ledger = {"goal": goal, "repo": repo, "wiki": wiki_context(repo), "git": git_context(repo),
              "done": [], "remaining": list(steps), "ts": int(time.time() * 1000)}
    for i, step in enumerate(steps):
        ledger["remaining"] = steps[i + 1:]
        out = run(step, _relay_context(ledger))
        ledger["done"].append({"step": step, "provider": out.get("provider"), "class": out.get("class"),
                               "ok": out.get("ok"), "output": out.get("text"), "ms": out.get("ms")})
    return ledger


def plan_and_relay(goal, repo=None, max_steps=6):
    """Full automation: a cheap model decomposes the goal into steps, then relay() routes + chains them."""
    decomp = run(f"Break this goal into {max_steps} or fewer concrete, ordered steps. "
                 f"Reply with ONLY a JSON array of short step strings, nothing else.\nGOAL: {goal}",
                 wiki_context(repo))
    txt = (decomp.get("text") or "").strip()
    i, j = txt.find("["), txt.rfind("]")
    try:
        steps = [str(s) for s in json.loads(txt[i:j + 1])][:max_steps] if i >= 0 else []
    except Exception:
        steps = []
    if not steps:
        steps = [ln.strip("-*0123456789. ") for ln in txt.splitlines() if ln.strip()][:max_steps]
    if not steps:
        steps = [goal]
    led = relay(goal, steps, repo)
    led["decomposedBy"] = decomp.get("provider")
    return led


# ---- CLI: one terminal, all providers ----
def _print_step(d):
    print(f"\n  ── [{d.get('class')}] → {d.get('provider')} ({d.get('ms', '?')}ms) ──")
    print("  " + (d.get("output") or d.get("text") or "(no output)").replace("\n", "\n  ")[:1600])


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")   # Windows console is cp1252; the box glyphs need utf-8
    except Exception:
        pass
    a = sys.argv[1:]
    repo = None
    if "--repo" in a:
        k = a.index("--repo"); repo = a[k + 1]; del a[k:k + 2]
    if not a:
        print("router.py — route/relay tasks across providers (never defaults to Claude).\n"
              "  python router.py route  \"<task>\" [--repo PATH]\n"
              "  python router.py relay  \"<goal>\" \"step1\" \"step2\" ... [--repo PATH]\n"
              "  python router.py plan   \"<goal>\" [--repo PATH]   # auto-decompose + route + relay\n"
              "  python router.py why    \"<task>\"                 # show the routing decision only")
        sys.exit(0)
    cmd = a[0]
    if cmd == "why":
        cls = classify_task(a[1]); print(json.dumps({"class": cls, "pick": pick_provider(cls)}, indent=1))
    elif cmd == "route":
        r = run(a[1], (wiki_context(repo) + "\n" + git_context(repo)).strip())
        print(f"routed → [{r.get('class')}] {r.get('provider')}"); _print_step(r)
    elif cmd == "relay":
        led = relay(a[1], a[2:], repo)
        for d in led["done"]:
            _print_step(d)
        print(f"\n✓ relay done — {len(led['done'])} steps across "
              f"{len({d['provider'] for d in led['done']})} providers.")
    elif cmd == "plan":
        led = plan_and_relay(a[1], repo)
        print(f"decomposed by {led.get('decomposedBy')} → {len(led['done'])} steps:")
        for d in led["done"]:
            _print_step(d)
    else:
        print(f"unknown command '{cmd}' — try route|relay|plan|why")
