"""Live hardware demo feed: the tap server pushes what it does to the web simulator's
/live/<session> endpoint, where the "Connect to hardware demo" page shows it. The website
on Modal can't reach the Mac's LAN, so the Mac pushes out; nothing ever connects in.

Opt-in. Off unless both are set (scripts/demo_up.sh sets them when KETCHUP_LIVE_BASE is in .env):
    KETCHUP_LIVE_URL    https://<sim site>/live/<session>   (session: random, letters/digits)
    KETCHUP_LIVE_TOKEN  random, 32+ characters: posting and reading both need it

Sent, in batches every 0.5 s (POST JSON {"events": [...]}, header X-Live-Token):
    {"kind": "log",   "text": every line the tap server prints (stdout and stderr)}
    {"kind": "model", "model", "label", "what", "seconds", "ok", "in", "out"}  every model call
    {"kind": "plan",  "status" (pencilled | booked), "start" (ISO), "minutes", "title", "slot",
                      "session", "until" (pencilled: when it confirms on its own)}
                      the newest session's time on the calendar, whenever it changes
Each event also has "t" (epoch seconds). Text passes redact() first: key values from the
environment, Bearer headers, sk-/key-looking strings and long random-looking tokens become
[redacted]. A push that fails is dropped (the demo never waits on the website); one line on the
real stderr says so.
"""
import json
import os
import re
import sys
import threading
import time
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "brain"))
import timing  # noqa: E402

URL = os.environ.get("KETCHUP_LIVE_URL", "").rstrip("/")
TOKEN = os.environ.get("KETCHUP_LIVE_TOKEN", "")
MAX_QUEUE = 2000  # events waiting to go out; older ones are dropped if the website is down
SECRET_ENV = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", re.I)
BEARER = re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]{6,}")
KEY_VALUE = re.compile(r"(?i)(\b(?:authorization|x-live-token|api[_-]?key|token|secret|password)"
                       r"[\"']?\s*[:=]\s*[\"']?)[A-Za-z0-9._~+/=-]{6,}")
PATTERNS = [re.compile(r"\b(sk|pk|rk|key|ghp|gho|xox[abp])[-_][A-Za-z0-9_-]{10,}"),
            re.compile(r"\b(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{32,}\b")]  # random-looking
_queue, _lock, _wake = [], threading.Lock(), threading.Event()
_state = {"on": False, "failing": False, "real_err": sys.stderr}


def secrets():
    """Values of key-like environment variables (and the live token) at least 8 long."""
    return sorted({v for k, v in os.environ.items() if SECRET_ENV.search(k) and len(v) >= 8}, key=len, reverse=True)


def redact(text):
    text = str(text)
    for v in secrets():
        text = text.replace(v, "[redacted]")
    text = BEARER.sub(lambda m: m.group(1) + "[redacted]", text)
    text = KEY_VALUE.sub(lambda m: m.group(1) + "[redacted]", text)
    for p in PATTERNS:
        text = p.sub("[redacted]", text)
    return text


def push(kind, **fields):
    if not _state["on"]:
        return
    ev = {"kind": kind, "t": round(time.time(), 3),
          **{k: (redact(v)[:400] if isinstance(v, str) else v) for k, v in fields.items()}}
    with _lock:
        _queue.append(ev)
        del _queue[:-MAX_QUEUE]
    _wake.set()


def _sender():
    while True:
        _wake.wait(2)
        time.sleep(0.5)  # batch
        _wake.clear()
        with _lock:
            batch, _queue[:] = _queue[:200], _queue[200:]
        if not batch:
            continue
        req = urllib.request.Request(URL, data=json.dumps({"events": batch}).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "X-Live-Token": TOKEN})
        try:
            urllib.request.urlopen(req, timeout=5).read()
            if _state["failing"]:
                _state["real_err"].write("live: pushing again\n")
            _state["failing"] = False
        except Exception as e:
            if not _state["failing"]:
                _state["real_err"].write(f"live: push failed ({type(e).__name__}), dropping events until it works\n")
            _state["failing"] = True
        if _queue:
            _wake.set()


class _Tee:
    """Writes through to the terminal and pushes each complete line (one buffer per thread, so
    two threads printing at once don't glue their lines together)."""

    def __init__(self, out):
        self.out, self.local = out, threading.local()

    def write(self, s):
        buf = getattr(self.local, "buf", "") + s
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            if line.strip():
                push("log", text=line)
        self.local.buf = buf
        return self.out.write(s)

    def flush(self):
        self.out.flush()

    def __getattr__(self, name):
        return getattr(self.out, name)


def _on_call(row):
    if row.get("kind") == "model":
        push("model", model=row.get("model") or "?", label=row.get("label", ""), what=row.get("what", ""),
             seconds=row.get("seconds", 0), ok=row.get("ok", True), error=row.get("error", ""),
             **{"in": row.get("input", ""), "out": row.get("output", "")})


def start():
    """Turn the feed on if KETCHUP_LIVE_URL and KETCHUP_LIVE_TOKEN are set. True if on."""
    if not URL or not TOKEN:
        return False
    if len(TOKEN) < 32 or not re.search(r"/live/[A-Za-z0-9]{6,40}$", URL):
        raise SystemExit("KETCHUP_LIVE_URL must end in /live/<session> and KETCHUP_LIVE_TOKEN be 32+ characters")
    _state["on"] = True
    _state["real_err"] = sys.stderr
    sys.stdout, sys.stderr = _Tee(sys.stdout), _Tee(sys.stderr)
    timing.HOOKS.append(_on_call)
    threading.Thread(target=_sender, daemon=True).start()
    print(f"LIVE feed on: pushing to {URL.rsplit('/live/', 1)[0]}/live/<session>", flush=True)
    return True
