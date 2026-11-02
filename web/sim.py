"""Ketchup web simulator: two virtual pebbles (Mayo and Frankie) in a browser.

One process. The real brain (flow.Session: Muse Spark Open on Baseten, with
Muse Spark 1.3 as the backup) runs against seeded mock calendars, never
Google. The browser records hold-to-talk answers as 16 kHz mono WAV; this
server sends them to Muse Voice Transcribe, and speaks replies with Meta
MMS-TTS (loaded once at startup; text only if it fails). Every model call
is logged with its model name and time, per session.

  PORT=8000 python web/sim.py          # then open http://localhost:8000
  /classic  is the older three-column web page (same server).

Env: BASETEN_API_KEY (brain), META_API_KEY (backup brain + speech to text),
PORT, HOST (default 0.0.0.0), KETCHUP_TTS=off to skip loading MMS,
KETCHUP_MAX_SESSIONS, KETCHUP_MAX_CALLS, KETCHUP_STARTS_PER_HOUR,
KETCHUP_ANSWER_S. Keys stay on the server; the page never sees them.
"""
import importlib.util
import io
import json
import os
import re
import sys
import threading
import time
import uuid
import wave
from collections import OrderedDict
from pathlib import Path
from urllib.parse import parse_qs, urlparse

os.environ.setdefault("KETCHUP_CALENDAR", "mock")  # the simulator never touches Google
os.environ.setdefault("OMP_NUM_THREADS", "1")       # before torch loads
HERE = Path(__file__).parent
ROOT = HERE.parent

# the classic page's server: its .env loader, Handler and API (served at /classic)
_spec = importlib.util.spec_from_file_location("classic_server", HERE / "server.py")
classic = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(classic)
sys.path.insert(0, str(ROOT / "pebble"))

from datetime import datetime, timedelta  # noqa: E402

import flow  # noqa: E402
import holds  # noqa: E402
import mock_calendar  # noqa: E402
import timing  # noqa: E402
from gcal import PEOPLE, calendar  # noqa: E402
from parse_plan import TZ  # noqa: E402

import pi_pebble as voice  # noqa: E402  (mms_text, load_mms, mms_pcm, transcribe: shared with the Pi)

holds.STORE = ROOT / "state/sim_holds.json"  # never mixed with the real pebbles' holds
flow.TRACES = ROOT / "state/sim-traces"

ENV = os.environ.get
MAX_SESSIONS = int(ENV("KETCHUP_MAX_SESSIONS", 6))      # live at once
MAX_CALLS = int(ENV("KETCHUP_MAX_CALLS", 40))           # model calls per session
STARTS_PER_HOUR = int(ENV("KETCHUP_STARTS_PER_HOUR", 60))
ANSWER_S = float(ENV("KETCHUP_ANSWER_S", 90))           # judges need time to find the mic
AUDIO_MAX_S = 15
MAX_ANSWERS = 16                                        # spoken + typed answers per session
MAX_TTS = 40
SESSION_TTL = 30 * 60
DEV = {k: p["device_id"] for k, p in PEOPLE.items()}    # mayo -> pebble-mayo
TTS_ID = "facebook/mms-tts-eng"
ASR_ID = "muse-voice-transcribe-1.0"

SIMS = OrderedDict()
_lock = threading.Lock()
_starts = []
VOICE = {"state": "off" if ENV("KETCHUP_TTS", "mms").lower() == "off" else "loading", "error": None}
_tts_lock = threading.Lock()   # one torch synthesis at a time (1 thread)
_tts_cache = OrderedDict()     # text -> wav bytes


def wav_bytes(pcm, rate=16000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(rate), w.writeframes(pcm)
    return buf.getvalue()


def load_voice():
    if VOICE["state"] == "off":
        return
    try:
        voice.load_mms()
        if not voice.mms:
            raise RuntimeError("MMS-TTS did not load")
        synth("Morning, noon, or night?")  # the first question: ready before anyone taps
        VOICE["state"] = "ready"
    except Exception as e:  # text only
        VOICE.update(state="off", error=f"{type(e).__name__}: {e}")
        print("VOICE off, text only:", VOICE["error"], flush=True)


def synth(text):
    """-> (wav bytes, seconds spent, cached?)"""
    if text in _tts_cache:
        _tts_cache.move_to_end(text)
        return _tts_cache[text], 0.0, True
    with _tts_lock:
        t0 = time.monotonic()
        data = wav_bytes(b"".join(voice.mms_pcm(text)))
        took = time.monotonic() - t0
    _tts_cache[text] = data
    while len(_tts_cache) > 200:
        _tts_cache.popitem(last=False)
    return data, took, False


WHAT = [  # system-prompt first line -> what the call did, for the page's log
    ("You are a scheduling agent. Someone said a plan phrase", "Turned the spoken plan into dates, length, activity"),
    ("You study one person's calendar", "Read one calendar's habits (backup if no answer)"),
    ("A person heard the question", "Understood a spoken answer"),
    ("A person has a meeting pencilled in", "Understood keep-or-change answer"),
    ("personal scheduling agent", "{who} agent negotiated a slot"),
]


def what_it_did(c):
    label = c.get("label", "")
    if c.get("what"):
        return c["what"]
    for key, text in WHAT:
        if key in label:
            m = re.match(r"You are (\w+)'s", label)
            who = f"{m[1]}'s" if m else ""
            return text.format(who=who).replace("  ", " ")
    return label[:60]


class Sim:
    def __init__(self, phrase):
        self.id = uuid.uuid4().hex[:8]
        self.made = time.time()
        self.answers = 0
        self.tts = 0
        self.heard = {"mayo": None, "frankie": None}
        self.replies = {"mayo": [], "frankie": []}  # what each pebble said back after an answer
        self.allowed = set()                         # lines this session may ask MMS to speak
        self._p = None                               # last s.public() snapshot
        self.s = flow.Session(phrase, "mayo", dry_run=False, source=f"sim-{self.id}", sid=self.id,
                              log=lambda line: print(f"[sim {self.id}] {line}", flush=True))
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        timing.SID.set(self.id)
        s = self.s
        s.start()
        if s.state == "asking_time_of_day":
            s.deadline = time.monotonic() + ANSWER_S
        while s.state == "asking_time_of_day":  # people who don't answer get their habit
            time.sleep(max(0.0, s.deadline - time.monotonic()) + 0.1)
            s.advance()

    def calls(self):
        return [c for c in timing.CALLS if c.get("sid") == self.id and c.get("kind") == "model"]

    def live(self):
        return self.s.state not in flow.DONE and time.time() - self.made < SESSION_TTL

    def view(self, since=0):
        s = self.s
        # The session lock is held while the agents negotiate (several seconds). Don't make
        # polls wait for it: reuse the last snapshot; s.lines (append-only) still streams.
        if s.lock.acquire(blocking=False):
            try:
                self._p = s.public()
            finally:
                s.lock.release()
        p = self._p or {"questions": {}, "speak": {}}
        pebbles = {}
        for key, dev in DEV.items():
            q = p["questions"].get(dev)
            speak = p["speak"].get(dev)
            if speak:
                self.allowed.add(speak)
            pebbles[key] = {"name": PEOPLE[key]["name"], "question": q and q["say"],
                            "kind": q and q["kind"], "speak": speak, "heard": self.heard[key],
                            "replies": self.replies[key]}
        left = None
        if p.get("state") == "asking_time_of_day" and s.deadline:
            left = max(0, round(s.deadline - time.monotonic()))
        calls = [{"model": c.get("model") or "?", "ok": c.get("ok", True), "seconds": round(c.get("seconds", 0), 2),
                  "what": what_it_did(c), "cached": c.get("cached", False),
                  "at": datetime.fromtimestamp(c["t"], TZ).strftime("%H:%M:%S")} for c in self.calls()]
        hold = p.get("hold") and {**p["hold"], "start": (s.hold or {}).get("start"),
                                  "minutes": (s.hold or {}).get("minutes"), "title": (s.hold or {}).get("title")}
        return {"session_id": self.id, "state": p.get("state", s.state), "phrase": s.phrase, "pebbles": pebbles,
                "seconds_left": left, "hold": hold, "agreed_slot": p.get("agreed_slot"),
                "nudge": p.get("nudge"), "error": p.get("error"),
                "lines": [l.replace("Google status", "mock calendar status").removesuffix(" None")
                          for l in s.lines[since:]],
                "line_count": len(s.lines), "calls": calls,
                "limits": {"calls_left": max(0, MAX_CALLS - len(calls)), "answers_left": MAX_ANSWERS - self.answers}}

    def answer(self, key, text, how):
        if key not in DEV:
            return 400, {"ok": False, "error": "unknown pebble"}
        if self.answers >= MAX_ANSWERS or len(self.calls()) >= MAX_CALLS:
            return 429, {"ok": False, "error": "This session hit its limit. Press New session to start again."}
        text = (text or "").strip()[:200]
        if not text:
            return 400, {"ok": False, "error": "Nothing heard. Hold the button while you talk."}
        self.answers += 1
        self.heard[key] = {"text": text, "how": how}
        token = timing.SID.set(self.id)
        try:
            r = self.s.answer(DEV[key], text)
        finally:
            timing.SID.reset(token)
        if r.get("say"):
            self.replies[key].append(r["say"])
            self.allowed.add(r["say"])
        if r["ok"]:
            def go():
                timing.SID.set(self.id)
                self.s.advance()
            threading.Thread(target=go, daemon=True).start()
        return (200 if r["ok"] else 409), {**r, "heard": text, "state": self.s.state}


def prune():
    now = time.time()
    with _lock:
        for k in [k for k, v in SIMS.items() if now - v.made > SESSION_TTL * 2]:
            SIMS.pop(k, None)
        _starts[:] = [t for t in _starts if now - t < 3600]


def week(sim):
    """Seven days from today, per person: seeded busy blocks plus this session's hold."""
    today = datetime.now(TZ).date()
    start = datetime.combine(today, datetime.min.time(), tzinfo=TZ)
    end = start + timedelta(days=7)
    out = {"days": [(today + timedelta(days=i)).isoformat() for i in range(7)], "people": {}}
    hold = sim and sim.s.hold  # the holds.py record: start, minutes, title, status
    for key, p in PEOPLE.items():
        busy = [{"start": a.isoformat(), "end": b.isoformat(), "title": t}
                for a, b, t in mock_calendar.seeded(p["calendar_id"], start, end)]
        mine = []
        if hold and hold.get("start"):
            mine.append({"start": hold["start"], "minutes": hold.get("minutes"), "title": hold.get("title"),
                         "status": hold.get("status", "tentative")})
        out["people"][key] = {"name": p["name"], "busy": busy, "ketchup": mine}
    return out


class Handler(classic.Handler):
    def log_message(self, fmt, *args):
        if self.command == "GET" and ("/api/" in self.path):
            return
        sys.stderr.write(f"{self.command} {self.path}\n")

    def raw(self, limit):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            self.rfile.read(min(n, limit + 1))
            return None
        return self.rfile.read(n)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/", "/index.html", "/sim"):
            return self.send(200, (HERE / "sim.html").read_bytes(), "text/html; charset=utf-8")
        if u.path in ("/classic", "/classic/"):
            return self.send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        parts = u.path.strip("/").split("/")
        if parts[:3] == ["sim", "api", "session"] and len(parts) == 4:
            sim = SIMS.get(parts[3])
            if not sim:
                return self.send(404, {"error": "That session ended. Press New session."})
            return self.send(200, sim.view(int(q.get("since", ["0"])[0] or 0)))
        if u.path == "/sim/api/calendar":
            return self.send(200, week(SIMS.get(q.get("session", [""])[0])))
        if u.path == "/sim/api/health":
            return self.send(200, {"brain": bool(ENV("BASETEN_API_KEY") or ENV("META_API_KEY")),
                                   "speech_to_text": bool(ENV("META_API_KEY")),
                                   "voice": VOICE["state"], "calendar": "mock",
                                   "live_sessions": sum(v.live() for v in SIMS.values()),
                                   "max_sessions": MAX_SESSIONS})
        return super().do_GET()

    def do_POST(self):
        u = urlparse(self.path)
        parts = u.path.strip("/").split("/")
        if parts == ["api", "start"]:  # the classic page: same session cap
            prune()
            if sum(v.live() for v in SIMS.values()) >= MAX_SESSIONS:
                return self.send(503, {"error": "Busy right now, try again in a minute."})
        if parts[:2] != ["sim", "api"]:
            return super().do_POST()
        try:
            if parts == ["sim", "api", "tap"]:
                return self.tap()
            if parts == ["sim", "api", "tts"]:
                return self.tts()
            if len(parts) == 5 and parts[2] == "session":
                sim = SIMS.get(parts[3])
                if not sim:
                    return self.send(404, {"error": "That session ended. Press New session."})
                if parts[4] == "voice":
                    return self.voice(sim, parse_qs(u.query).get("pebble", [""])[0])
                if parts[4] == "say":
                    b = self.body()
                    return self.send(*sim.answer(b.get("pebble", ""), b.get("text", ""), "typed"))
                if parts[4] == "skip_to_end_of_day":
                    token = timing.SID.set(sim.id)
                    try:
                        done = sim.s.skip_to_end_of_day()
                    finally:
                        timing.SID.reset(token)
                    return self.send(200, {"ok": bool(done), "state": sim.s.state})
        except json.JSONDecodeError:
            return self.send(400, {"error": "bad JSON"})
        self.send(404, {"error": "not found"})

    def tap(self):
        b = self.body()
        if not (ENV("BASETEN_API_KEY") or ENV("META_API_KEY")):
            return self.send(503, {"error": "The server has no model key set (BASETEN_API_KEY)."})
        prune()
        with _lock:
            if sum(v.live() for v in SIMS.values()) >= MAX_SESSIONS:
                return self.send(503, {"error": "Busy: other people are trying it. Try again in a minute."})
            if len(_starts) >= STARTS_PER_HOUR:
                return self.send(429, {"error": "Too many sessions this hour. Try again later."})
            _starts.append(time.time())
            old = SIMS.get(b.get("replaces") or "")
        phrase = re.sub(r"\s+", " ", str(b.get("phrase") or "let's catch up this week"))[:120].strip()
        sim = Sim(phrase or "let's catch up this week")
        if old and old.s.hold and old.s.state == "pencilled_in":  # New session: drop the old hold
            try:
                holds.release(calendar(), old.s.hold["id"])
            except Exception:
                pass
        with _lock:
            SIMS[sim.id] = sim
        return self.send(200, {"session_id": sim.id})

    def voice(self, sim, key):
        if not ENV("META_API_KEY"):
            return self.send(503, {"error": "Speech to text is off on this server. Type your answer instead."})
        wav = self.raw(16000 * 2 * AUDIO_MAX_S + 4096)
        if wav is None:
            return self.send(413, {"error": f"Too long. Keep answers under {AUDIO_MAX_S} seconds."})
        if len(wav) < 44 + 3200 or wav[:4] != b"RIFF":
            return self.send(400, {"error": "Too short. Hold the button while you talk."})
        if key not in DEV:
            return self.send(400, {"error": "unknown pebble"})
        if sim.answers >= MAX_ANSWERS or len(sim.calls()) >= MAX_CALLS:
            return self.send(429, {"error": "This session hit its limit. Press New session to start again."})
        t0 = time.monotonic()
        try:
            text = voice.transcribe(wav)
            ok, err = True, None
        except Exception as e:
            text, ok, err = "", False, f"{type(e).__name__}"
        timing.record(kind="model", model=ASR_ID, ok=ok, seconds=time.monotonic() - t0, sid=sim.id,
                      label="asr", what=f"Speech to text on {PEOPLE[key]['name']}'s pebble"
                      + (f': "{text[:40]}"' if text else ""))
        if not ok:
            return self.send(502, {"error": f"Speech to text failed ({err}). Try again or type it."})
        if not text:
            return self.send(409, {"ok": False, "error": "I didn't catch any words. Try again, a little closer."})
        code, r = sim.answer(key, text, "voice")
        return self.send(code, r)

    def tts(self):
        b = self.body()
        sim, text = SIMS.get(b.get("session_id") or ""), str(b.get("text") or "")
        if VOICE["state"] != "ready":
            return self.send(503, {"error": f"voice {VOICE['state']}"})
        if not sim:
            return self.send(404, {"error": "no such session"})
        if text not in sim.allowed:
            sim.view()  # refresh the allowed lines
        if text not in sim.allowed:
            return self.send(403, {"error": "only this session's own lines are spoken"})
        if sim.tts >= MAX_TTS:
            return self.send(429, {"error": "voice limit for this session"})
        sim.tts += 1
        data, took, cached = synth(text)
        timing.record(kind="model", model=TTS_ID, ok=True, seconds=took, sid=sim.id, cached=cached,
                      label="tts", what=f'Spoke "{text[:44]}"' + (" (cached)" if cached else ""))
        self.send(200, data, "audio/wav")


def main():
    port = int(ENV("PORT", 8000))
    host = ENV("HOST", "0.0.0.0")
    threading.Thread(target=load_voice, daemon=True).start()
    holds.start_timer(calendar, log=lambda m: print(m, flush=True))  # mock calendar: 10 PM auto-confirm
    keys = [k for k in ("BASETEN_API_KEY", "META_API_KEY") if ENV(k)]
    print(f"Ketchup simulator on http://{host}:{port}  (calendar: mock; keys set: {', '.join(keys) or 'none'})",
          flush=True)
    classic.ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
