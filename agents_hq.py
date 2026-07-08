# agents_hq.py - Agent Library / Recommender / Training-Archive engine for Mission Control.
# Agents are markdown defs in ~/.claude/agents (frontmatter: name/description/tools/model).
# "Training" here = prompt editing + learned-rules + versioned archive (prompt IS the trainable
# surface for Claude agents; no weight fine-tuning exists — the UI says so honestly).
import json, re, time
from pathlib import Path

AGENTS_DIR = Path.home() / ".claude" / "agents"
ARCHIVE_DIR = Path(__file__).parent / "agent-archive"

_SAFE = re.compile(r"[^A-Za-z0-9_-]")

def _safe_name(name):
    n = _SAFE.sub("", name or "")
    if not n:
        raise ValueError("bad agent name")
    return n

def _agent_path(name):
    p = AGENTS_DIR / f"{_safe_name(name)}.md"
    if not p.resolve().is_relative_to(AGENTS_DIR.resolve()):
        raise ValueError("path escape")
    return p

def _parse(md):
    """frontmatter dict + body from an agent .md"""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", md, re.S)
    fm, body = {}, md
    if m:
        body = m.group(2)
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                fm[k.strip()] = v.strip()
    return fm, body

def list_agents():
    out = []
    if not AGENTS_DIR.is_dir():
        return out
    for f in sorted(AGENTS_DIR.glob("*.md")):
        try:
            fm, body = _parse(f.read_text(encoding="utf-8", errors="replace"))
            arc = ARCHIVE_DIR / f.stem
            out.append({
                "name": fm.get("name", f.stem),
                "file": f.stem,
                "description": fm.get("description", "")[:400],
                "tools": fm.get("tools", ""),
                "model": fm.get("model", "inherit"),
                "bodyChars": len(body),
                "lessons": body.count("\n- ", body.find("## Learned rules")) if "## Learned rules" in body else 0,
                "versions": len(list(arc.glob("*.md"))) if arc.is_dir() else 0,
                "modified": f.stat().st_mtime,
            })
        except Exception:
            continue
    return out

# ---------- recommender (lexical, transparent scoring; deep matching = ask Claude in chat) ----------
_STOP = set("the a an and or for with of to in on at is are be this that it as by from i my me want need".split())

def _tokens(s):
    return [w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if len(w) > 2 and w not in _STOP]

def recommend(task, top=5):
    toks = _tokens(task)
    if not toks:
        return []
    scored = []
    for f in AGENTS_DIR.glob("*.md") if AGENTS_DIR.is_dir() else []:
        try:
            md = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        fm, body = _parse(md)
        name_t = set(_tokens(fm.get("name", f.stem)))
        desc_t = set(_tokens(fm.get("description", "")))
        body_t = set(_tokens(body[:4000]))
        score, hits = 0.0, []
        for t in set(toks):
            if t in name_t:
                score += 5; hits.append(t)
            elif t in desc_t:
                score += 3; hits.append(t)
            elif t in body_t:
                score += 1
        if score > 0:
            scored.append({
                "name": fm.get("name", f.stem), "file": f.stem,
                "score": round(score, 1), "matched": hits[:6],
                "description": fm.get("description", "")[:200],
                "invocation": f"Use the Agent tool with subagent_type '{fm.get('name', f.stem)}'",
            })
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top]

# ---------- training / archive ----------
def get_agent(name):
    p = _agent_path(name)
    return {"name": name, "content": p.read_text(encoding="utf-8", errors="replace")}

def _backup(name):
    p = _agent_path(name)
    arc = ARCHIVE_DIR / _safe_name(name)
    arc.mkdir(parents=True, exist_ok=True)
    stem = time.strftime("%Y%m%d-%H%M%S")
    ver, i = arc / f"{stem}.md", 1
    while ver.exists():                       # two edits in the same second must NOT overwrite a version
        ver = arc / f"{stem}-{i}.md"; i += 1
    ver.write_text(p.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
    return ver.name

def save_agent(name, content):
    if not re.match(r"^---\s*\n.*?name:", content, re.S):
        raise ValueError("refusing save: content lacks frontmatter with a name: field")
    p = _agent_path(name)
    ver = _backup(name) if p.exists() else None
    p.write_text(content, encoding="utf-8")
    return {"ok": True, "backedUpAs": ver}

def add_lesson(name, lesson):
    """Append a learned rule — the honest 'training' increment. Versioned like any edit."""
    lesson = (lesson or "").strip().replace("\n", " ")
    if not lesson:
        raise ValueError("empty lesson")
    p = _agent_path(name)
    md = p.read_text(encoding="utf-8", errors="replace")
    ver = _backup(name)
    stamp = time.strftime("%Y-%m-%d")
    if "## Learned rules" in md:
        md = md.rstrip() + f"\n- ({stamp}) {lesson}\n"
    else:
        md = md.rstrip() + f"\n\n## Learned rules\n*(appended via Agent HQ — each line is a trained-in correction)*\n- ({stamp}) {lesson}\n"
    p.write_text(md, encoding="utf-8")
    return {"ok": True, "backedUpAs": ver}

def list_versions(name):
    arc = ARCHIVE_DIR / _safe_name(name)
    if not arc.is_dir():
        return []
    return sorted([v.name for v in arc.glob("*.md")], reverse=True)

def restore(name, version):
    ver = ARCHIVE_DIR / _safe_name(name) / re.sub(r"[^0-9A-Za-z.-]", "", version)
    if not ver.is_file():
        raise ValueError("version not found")
    cur = _backup(name)  # archive current before rolling back
    _agent_path(name).write_text(ver.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
    return {"ok": True, "restored": version, "previousSavedAs": cur}
