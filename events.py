# events.py — one global change-counter + wakeup for Server-Sent Events. Any HELM state write bumps
# the version; each open /api/events stream blocks on a Condition and wakes to emit a "tick". Stdlib
# only, zero deps. This is what lets the UI hold ONE live connection instead of ~15 polling loops.
import threading

_cond = threading.Condition()
_version = 0
_streams = 0
MAX_STREAMS = 12   # cap concurrent SSE threads — thread-per-request is unbounded on ThreadingHTTPServer


def bump():
    """Signal that HELM state changed — wakes every open /api/events stream to push a tick."""
    global _version
    with _cond:
        _version += 1
        _cond.notify_all()


def version():
    with _cond:
        return _version


def streams():
    """How many SSE clients are connected right now (for the live-push glass panel)."""
    with _cond:
        return _streams


def wait(last, timeout):
    """Block until the version moves past `last` or `timeout` elapses; return the current version."""
    with _cond:
        if _version == last:
            _cond.wait(timeout)
        return _version


def open_stream():
    """Reserve a stream slot; False if we're at the cap (client should fall back to polling)."""
    global _streams
    with _cond:
        if _streams >= MAX_STREAMS:
            return False
        _streams += 1
        return True


def close_stream():
    global _streams
    with _cond:
        _streams = max(0, _streams - 1)
