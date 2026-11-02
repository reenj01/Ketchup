"""Every model and Google call, timed, so a session can report where time goes."""
import contextvars
import threading
import time
from contextlib import contextmanager

CALLS = []
_lock = threading.Lock()
SID = contextvars.ContextVar("sid", default=None)  # the session a call belongs to (web simulator log)


def record(**row):
    if SID.get() and "sid" not in row:
        row["sid"] = SID.get()
    with _lock:
        CALLS.append({"t": time.time(), **row})


@contextmanager
def timed(kind, label, **extra):
    t0 = time.perf_counter()
    try:
        yield
    finally:
        record(kind=kind, label=label, seconds=round(time.perf_counter() - t0, 2), **extra)


def summary(rows):
    """Totals per kind: count, summed seconds, and tokens for model calls."""
    out = {}
    for r in rows:
        k = out.setdefault(r["kind"], {"calls": 0, "seconds": 0.0, "prompt_tokens": 0,
                                       "completion_tokens": 0, "reasoning_tokens": 0})
        k["calls"] += 1
        k["seconds"] = round(k["seconds"] + r["seconds"], 2)
        for f in ("prompt_tokens", "completion_tokens", "reasoning_tokens"):
            k[f] += r.get(f) or 0
    return out
