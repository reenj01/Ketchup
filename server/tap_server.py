"""Tiny tap-pairing server (standard library only).

    python server/tap_server.py [--port 8787] [--dry-run]
    python server/tap_server.py --skip-to-end-of-day   # confirm every open hold now, then exit

Both pebbles are the same: mic + speaker. They:
1. POST /tap {"device_id": "pebble-mayo", "phrase": "let's catch up Thursday"}
   Waits up to 5 s (KETCHUP_PAIR_WINDOW_S) for a tap from the other pebble. Both get the
   same {"status": "paired", "session_id": ...}, or {"status": "expired", "waited_s": ...}.
2. Right after pairing the agents pick a time from both calendars (no question
   first). Poll GET /sessions/<id>: state "paired"/"proposing" = agents talking;
   "questions" then holds, per device, the time to say out loud ("say"):
   "How about Tuesday at 7 PM? ... Want to change it?". While the agents hold the
   session lock the poll answers at once with {"busy": true} and no questions.
3. POST /sessions/<id>/answer {"device_id": ..., "answer": "<voice transcript>"}
   Muse Spark Open maps it to keep/change (with the reason: "no, pick a time
   at night"). A change sends both agents back to talking; the other Dot only
   hears that a different time was wanted, never the words. Repeats until both
   keep it. Plain keywords work too.
   Every answer response has "say": the text to speak back right away.
   GET /sessions/<id> has "speak": {device_id: text to say now}.
4. After the agents agree, the time is a tentative hold on both calendars.
   It confirms when both keep it, or at the deadline (10 PM ET that day,
   or 30 min before it starts). A timer checks deadlines every
   hold_sweep_s and at startup (state/holds.json, idempotent).
   POST /sessions/<id>/skip_to_end_of_day {} confirms that hold now (demo).
5. Check-in: each Dot's service (deploy/dots/dot_heartbeat.py) POSTs /hello
   {"device_id": ...} every few seconds while the Dot runs. The server prints
   "DOT ONLINE ..." / "BOTH DOTS ONLINE" / "DOT OFFLINE ..." once per change.
   GET /health -> {"ok": true, "dots": {device_id: seconds since last seen}}.

6. Live demo log (opt-in, server/live.py): with KETCHUP_LIVE_URL and KETCHUP_LIVE_TOKEN set
   (scripts/demo_up.sh does it when KETCHUP_LIVE_BASE is in .env), every line printed here and
   every model call is pushed to the web simulator's /live page. Lines include HEARD (each
   answer a Dot sends) and LED (the LED each Dot should show for the session).

States: paired -> proposing -> pencilled_in (-> proposing -> pencilled_in ...) ->
booked | agreed (dry run, not booked) | failed.
"""
import argparse
import json
import os
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "brain"))
import holds  # noqa: E402
import live  # noqa: E402  (server/: opt-in feed for the "Connect to hardware demo" page)
import timing  # noqa: E402
from flow import Session  # noqa: E402
from gcal import PEOPLE, calendar  # noqa: E402

PAIR_WINDOW = float(os.environ.get("KETCHUP_PAIR_WINDOW_S", 5))  # seconds between the two taps
DEVICES = {p["device_id"]: key for key, p in PEOPLE.items()}

lock = threading.Condition()
pending = []   # open taps: {"device_id", "phrase", "at", "session_id"}
sessions = {}  # session_id -> Session
DRY_RUN = False
OFFLINE_S = 15.0  # no check-in for this long = offline
seen = {}         # device_id -> monotonic time of its last /hello or /tap
online = set()    # device_ids announced online


def check_in(device_id, now=None):
    """Record a Dot's check-in; print a line when it comes online (and when both are)."""
    now = time.monotonic() if now is None else now
    with lock:
        seen[device_id] = now
        if device_id in online:
            return
        online.add(device_id)
        both = set(DEVICES) <= online
    name = DEVICES[device_id].capitalize()
    print(f"DOT ONLINE  {device_id} ({name}'s Dot)", flush=True)
    if both:
        print("BOTH DOTS ONLINE: ready to film", flush=True)


def sweep_offline(now=None):
    """Print a line for each Dot that stopped checking in."""
    now = time.monotonic() if now is None else now
    with lock:
        gone = [d for d in online if now - seen.get(d, 0) > OFFLINE_S]
        online.difference_update(gone)
    for d in gone:
        print(f"DOT OFFLINE {d} (no check-in for {OFFLINE_S:.0f} s)", flush=True)
    return gone


def health(now=None):
    now = time.monotonic() if now is None else now
    with lock:
        return {"ok": True, "dry_run": DRY_RUN,
                "dots": {d: round(now - t, 1) for d, t in seen.items()},
                "online": sorted(online), "both_online": set(DEVICES) <= online}


def run_session(s):
    s.negotiate_first()  # agents decide a time first; owners keep it or ask for another


def snapshot(s):
    """GET /sessions/<id>. The agents hold the session lock while they talk (seconds);
    answer at once instead of making the Dots wait: state only, nothing to say yet."""
    if not s.lock.acquire(blocking=False):
        return {"session_id": s.sid, "state": s.state, "busy": True, "questions": {}, "speak": {},
                "confirms": {}, "change_round": s.change_round}
    try:
        return s.public()
    finally:
        s.lock.release()


def tap(device_id, phrase):
    me = {"device_id": device_id, "phrase": phrase, "at": time.monotonic(), "session_id": None}
    with lock:
        now = me["at"]
        pending[:] = [t for t in pending if now - t["at"] <= PAIR_WINDOW and not t["session_id"]]
        partner = next((t for t in pending if t["device_id"] != device_id), None)
        if not partner:
            pending.append(me)
            lock.wait_for(lambda: me["session_id"], timeout=PAIR_WINDOW)
            if me in pending:
                pending.remove(me)
            if not me["session_id"]:
                return {"status": "expired", "waited_s": round(time.monotonic() - me["at"], 2)}
            return {"status": "paired", "session_id": me["session_id"],
                    "partner": me["partner"], "tap_gap_s": me["gap"]}
        pending.remove(partner)
        sid = uuid.uuid4().hex[:8]
        gap = round(me["at"] - partner["at"], 2)
        # Whoever's pebble heard the phrase is the proposer.
        heard = partner if partner["phrase"] else me
        s = Session(heard["phrase"] or "let's catch up this week", DEVICES[heard["device_id"]],
                    DRY_RUN, log=lambda line: print(f"[{sid}] {line}", flush=True),
                    source=f"tap-{sid}", sid=sid)
        s.trace["tap"] = {"devices": [partner["device_id"], device_id], "gap_s": gap}
        sessions[sid] = s
        partner.update(session_id=sid, partner=device_id, gap=gap)
        lock.notify_all()
        threading.Thread(target=run_session, args=(s,), daemon=True).start()
        return {"status": "paired", "session_id": sid, "partner": partner["device_id"],
                "tap_gap_s": gap}


def heard(s, device, text, via, data):
    """One line per answer a Dot sends (the transcript, or the press), plus the Dot's speech-to-text
    call (timed on the Pi) in the model-call record."""
    name = DEVICES[device].capitalize()
    asr = data.get("asr_s")
    print(f'HEARD {name}\'s Dot ({via}{f", speech to text {asr} s" if asr is not None else ""}): "{text[:200]}"',
          flush=True)
    if isinstance(asr, (int, float)):
        token = timing.SID.set(s.sid)
        try:
            timing.record(kind="model", label="asr", model=str(data.get("asr_model") or "speech to text")[:60],
                          what=f"Speech to text on {name}'s Dot", ok=True, seconds=round(asr, 2),
                          input="(audio)", output=text[:300])
        finally:
            timing.SID.reset(token)


LED_SEEN = {}  # device_id -> last LED line printed


def dot_leds(snap):
    """The LED each Dot should show for this session snapshot (what pebble/pi_pebble.wanted() shows
    between its own taps, speech and flashes): printed as LED lines for the live demo log."""
    st, q = snap.get("state"), snap.get("questions") or {}
    out = {}
    for dev in DEVICES:
        if snap.get("busy") or st in ("paired", "proposing"):
            out[dev] = "yellow breathing (agents deciding)"
        elif st == "pencilled_in":
            out[dev] = "orange breathing (time waiting for an answer)" if dev in q else "bright white (answered)"
        elif st in ("booked", "agreed (dry run, not booked)"):
            out[dev] = "green (booked)"
        else:
            out[dev] = "dim white"
    return out


def watch_leds():
    """Print a line whenever the newest session's expected LED changes on a Dot."""
    with lock:
        s = list(sessions.values())[-1] if sessions else None
    if not s:
        return
    for dev, led in dot_leds(snapshot(s)).items():
        if LED_SEEN.get(dev) != (s.sid, led):
            LED_SEEN[dev] = (s.sid, led)
            print(f"LED {DEVICES[dev].capitalize()}'s Dot: {led} [{s.sid}]", flush=True)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj, indent=2, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")

    def do_POST(self):
        try:
            data = self._body()
        except json.JSONDecodeError:
            return self._send(400, {"error": "body must be JSON"})
        parts = self.path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "skip_to_end_of_day":
            s = sessions.get(parts[1])
            if not s:
                return self._send(404, {"error": "no such session"})
            done = s.skip_to_end_of_day()
            return self._send(200, {"ok": True, "confirmed": [h["id"] for h in done],
                                    "state": s.state, "speak": s.public()["speak"]})
        device = data.get("device_id")
        if device not in DEVICES:
            return self._send(400, {"error": f"unknown device_id, expected one of {list(DEVICES)}"})
        if self.path == "/hello":
            check_in(device)
            return self._send(200, {"ok": True})
        if self.path == "/tap":
            check_in(device)
            return self._send(200, tap(device, data.get("phrase")))
        if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "answer":
            s = sessions.get(parts[1])
            if not s:
                return self._send(404, {"error": "no such session"})
            text, via = str(data.get("answer", "")), str(data.get("via") or "answer")[:20]
            heard(s, device, text, via, data)
            r = s.answer(device, text)
            if r["ok"]:
                threading.Thread(target=s.advance, daemon=True).start()
            return self._send(200 if r["ok"] else 409, {**r, "state": s.state})
        self._send(404, {"error": "not found"})

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, health())
        if self.path.startswith("/sessions/"):
            s = sessions.get(self.path.removeprefix("/sessions/"))
            return self._send(200, snapshot(s)) if s else self._send(404, {"error": "no such session"})
        self._send(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        if not any(p in (fmt % args) for p in ("GET /sessions", "POST /hello", "GET /health")):  # polled a lot
            print(f"[{self.log_date_time_string()}] {fmt % args}", flush=True)


def main():
    global DRY_RUN
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--dry-run", action="store_true", help="never book")
    ap.add_argument("--skip-to-end-of-day", action="store_true",
                    help="confirm every open tentative hold now (as if the day ended), then exit")
    args = ap.parse_args()
    if args.skip_to_end_of_day:
        done = holds.sweep(calendar, hold_ids="all")
        print(f"Confirmed {len(done)} hold(s): " + ", ".join(f"{h['title']} {h['start']}" for h in done))
        return
    DRY_RUN = args.dry_run
    live.start()  # only if KETCHUP_LIVE_URL and KETCHUP_LIVE_TOKEN are set
    holds.start_timer(calendar, log=lambda m: print(m, flush=True))
    def offline_timer():
        while True:
            time.sleep(5)
            sweep_offline()
    def led_timer():
        while True:
            time.sleep(0.5)
            watch_leds()
    threading.Thread(target=offline_timer, daemon=True).start()
    threading.Thread(target=led_timer, daemon=True).start()
    print(f"Ketchup tap server on {args.host}:{args.port} (dry run: {DRY_RUN}, "
          f"pair window: {PAIR_WINDOW:g} s)", flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
