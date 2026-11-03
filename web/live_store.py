"""In-memory store behind the "Connect to hardware demo" page (GET /live).

POST /live/<session>  {"events": [...]}  header X-Live-Token  <- server/live.py on the Mac
GET  /live/<session>?since=N             header X-Live-Token  <- web/live.html, polled every second

The first POST with a token claims the session; after that, posts and reads need that token
(only its SHA-256 is kept). Strangers can't read or post without it. Caps: MAX_EVENTS per
session (oldest dropped), MAX_LIVE sessions (one idle for IDLE_S can be replaced), 200 events
and 256 KB per POST, 400 characters per field. Nothing touches disk; a redeploy clears it.
"""
import hashlib
import hmac
import re
import threading
import time
from collections import deque

MAX_EVENTS = 3000
MAX_LIVE = 8
IDLE_S = 2 * 3600
MAX_BODY = 256 * 1024
SID = re.compile(r"^[A-Za-z0-9]{6,40}$")
FIELDS = {"kind", "t", "text", "model", "label", "what", "seconds", "ok", "error", "in", "out",
          "status", "start", "minutes", "title", "slot", "session", "until"}
KINDS = {"log", "model", "plan"}  # plan: the time on the calendar (pencilled or booked), for the live page

_lock = threading.Lock()
_live = {}  # session -> {"hash", "events": deque, "seq", "touched"}


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _clean(ev, describe):
    ev = {k: (v[:400] if isinstance(v, str) else v) for k, v in ev.items()
          if k in FIELDS and isinstance(v, (str, int, float, bool))}
    if ev.get("kind") not in KINDS:
        return None
    if ev["kind"] == "model" and describe:
        ev["what"] = describe(ev)
    ev.setdefault("t", time.time())
    return ev


def post(sid, token, events, describe=None, now=None):
    """Returns (http code, body)."""
    now = now or time.time()
    if not SID.match(sid or "") or not (32 <= len(token or "") <= 128):
        return 400, {"error": "session must be 6-40 letters/digits and the token 32-128 characters"}
    if not isinstance(events, list) or len(events) > 200:
        return 400, {"error": "events must be a list of at most 200"}
    with _lock:
        s = _live.get(sid)
        if s is None:
            if len(_live) >= MAX_LIVE:
                idle = [k for k, v in _live.items() if now - v["touched"] > IDLE_S]
                if not idle:
                    return 503, {"error": "too many live sessions"}
                del _live[min(idle, key=lambda k: _live[k]["touched"])]
            s = _live[sid] = {"hash": _hash(token), "events": deque(maxlen=MAX_EVENTS), "seq": 0, "touched": now}
        elif not hmac.compare_digest(s["hash"], _hash(token)):
            return 403, {"error": "wrong token"}
        n = 0
        for ev in events:
            ev = isinstance(ev, dict) and _clean(ev, describe)
            if ev:
                s["seq"] += 1
                s["events"].append({**ev, "seq": s["seq"]})
                n += 1
        s["touched"] = now
    return 200, {"ok": True, "stored": n}


def get(sid, token, since=0):
    if not SID.match(sid or "") or not token:
        return 400, {"error": "need the session and token from the link demo_up.sh printed"}
    with _lock:
        s = _live.get(sid)
        if s is None:  # nothing pushed yet: don't say whether it exists, just wait
            return 200, {"events": [], "next": 0, "waiting": True}
        if not hmac.compare_digest(s["hash"], _hash(token)):
            return 403, {"error": "wrong token"}
        events = [e for e in s["events"] if e["seq"] > since]
        return 200, {"events": events, "next": s["seq"], "waiting": False,
                     "dropped": max(0, s["seq"] - len(s["events"]))}
