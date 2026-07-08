# model_rank.py - empirical, self-adapting model ranking. Instead of a hardcoded "best model" list
# frozen at the moment someone wrote the code, HELM records how every model ACTUALLY performs
# (success/failure + latency) every time it's used, and ranks candidates by that live track record.
# A model with no history yet gets a neutral "unproven" score so new/newly-available models get a
# fair try instead of being locked out forever by stale ordering - this is what makes the choice of
# "best" change with time as models are deprecated, rate-limited, or simply get better. (2026-07-08)
import json, os, re, tempfile, threading, time

ROOT = os.path.dirname(os.path.abspath(__file__))
SCORES_FILE = os.path.join(ROOT, "model-scores.json")
_LOCK = threading.RLock()

MAX_HISTORY = 40          # per-model rolling window - bounds file growth (mirrors the audit-log rotation pattern)
UNPROVEN_SCORE = 0.55     # no track record yet: tried before known-mediocre models, after known-solid ones
DECAY = 0.92              # each older sample counts for DECAY x the next-newer one (recency-weighted)


def _read():
    with _LOCK:
        try:
            return json.loads(open(SCORES_FILE, encoding="utf-8").read())
        except Exception:
            return {}


def _write(d):
    with _LOCK:
        fd, tmp = tempfile.mkstemp(dir=ROOT, suffix=".scores.tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(d, indent=1))
            os.replace(tmp, SCORES_FILE)
        except Exception:
            try: os.remove(tmp)
            except Exception: pass


def record(model_id, ok, ms=None, kind="cloud"):
    """Log one real call's outcome. This is the ONLY thing that makes ranking change over time -
    every real use nudges the score; nothing here is a fixed opinion frozen at authoring time."""
    if not model_id:
        return
    with _LOCK:
        d = _read()
        key = f"{kind}:{model_id}"
        entry = d.setdefault(key, {"kind": kind, "history": []})
        entry["history"].append({"ts": int(time.time() * 1000), "ok": bool(ok), "ms": ms})
        entry["history"] = entry["history"][-MAX_HISTORY:]
        _write(d)


def _param_hint(model_id):
    """Rough capability proxy from a local model's name (e.g. '12b' -> 12.0). Used ONLY to break
    ties among models with NO track record yet - real empirical data always wins once it exists."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*[bB]\b", model_id or "")
    return float(m.group(1)) if m else 0.0


def score(model_id, kind="cloud"):
    """Recency-weighted success rate in [0,1], with a mild penalty for very slow responses."""
    d = _read()
    entry = d.get(f"{kind}:{model_id}")
    hist = (entry or {}).get("history") or []
    if not hist:
        return UNPROVEN_SCORE
    w_sum = ok_sum = ms_sum = ms_w = 0.0
    weight = 1.0
    for h in reversed(hist):  # most recent first -> highest weight
        w_sum += weight
        ok_sum += weight * (1.0 if h.get("ok") else 0.0)
        if h.get("ms"):
            ms_sum += weight * h["ms"]; ms_w += weight
        weight *= DECAY
    rate = (ok_sum / w_sum) if w_sum else UNPROVEN_SCORE
    avg_ms = (ms_sum / ms_w) if ms_w else 0
    penalty = min(0.1, max(0.0, (avg_ms - 8000) / 80000)) if avg_ms else 0.0  # >8s avg shaves up to 0.1
    return max(0.0, min(1.0, rate - penalty))


def rank(candidates, kind="cloud", fallback_order=None):
    """Sort candidate model/provider ids best-first. Ties (e.g. all unproven on a cold start) keep
    the caller's given fallback order, so day-1 behavior is sane before any real data exists."""
    if not candidates:
        return []
    order_key = {c: i for i, c in enumerate(fallback_order or candidates)}
    scored = [(c, score(c, kind)) for c in candidates]
    scored.sort(key=lambda cs: (-cs[1], order_key.get(cs[0], 999)))
    return [c for c, _ in scored]


def best(candidates, kind="cloud", fallback_order=None, param_hint=False):
    """Top-ranked candidate, or None. param_hint=True breaks ties among equally-unproven LOCAL
    models by preferring the larger one - a reasonable capability guess before any real data exists."""
    if not candidates:
        return None
    ranked = rank(candidates, kind, fallback_order)
    if param_hint and len(ranked) > 1:
        top = score(ranked[0], kind)
        tied = [c for c in ranked if abs(score(c, kind) - top) < 1e-9]
        if len(tied) > 1:
            tied.sort(key=lambda c: -_param_hint(c))
            return tied[0]
    return ranked[0]


def snapshot(kind=None):
    """All tracked scores - for UI transparency (why is X currently preferred)."""
    d = _read()
    out = []
    for key, entry in d.items():
        k, _, mid = key.partition(":")
        if kind and k != kind:
            continue
        hist = entry.get("history") or []
        out.append({"model": mid, "kind": k, "score": round(score(mid, k), 3),
                    "samples": len(hist), "lastOk": hist[-1]["ok"] if hist else None})
    out.sort(key=lambda x: -x["score"])
    return out
