"""Tiny tap-pairing server (standard library only).

    python server/tap_server.py [--port 8787] [--dry-run]
    python server/tap_server.py --skip-to-end-of-day   # confirm every open hold now, then exit

Both pebbles are the same: mic + speaker. They:
1. POST /tap {"device_id": "pebble-mayo", "phrase": "let's catch up Thursday"}
   Waits up to 2.0 s for a tap from the other pebble. Both get the same
   {"status": "paired", "session_id": ...}, or {"status": "expired"}.
2. Poll GET /sessions/<id>. "questions" holds, per device, the text to say out
   loud ("say") while the session waits for that person.
3. POST /sessions/<id>/answer {"device_id": ..., "answer": "<voice transcript>"}
   Muse Spark Open maps it to morning/noon/night or keep/change (with the
   reason: "not thursday, something later"). Plain keywords work too.
   Every answer response has "say": the text to speak back right away.
   GET /sessions/<id> has "speak": {device_id: text to say now}.
4. After the agents agree, the time is a tentative hold on both calendars.
   It confirms when both keep it, or at the deadline (10 PM ET that day,
   or 30 min before it starts). A timer checks deadlines every
   hold_sweep_s and at startup (state/holds.json, idempotent).
   POST /sessions/<id>/skip_to_end_of_day {} confirms that hold now (demo).

States: paired -> asking_time_of_day -> proposing -> pencilled_in ->
booked | agreed (dry run, not booked) | failed.
"""
import argparse
import json
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "brain"))
import holds  # noqa: E402
from flow import Session  # noqa: E402
from gcal import PEOPLE, calendar  # noqa: E402

PAIR_WINDOW = 2.0
DEVICES = {p["device_id"]: key for key, p in PEOPLE.items()}

lock = threading.Condition()
pending = []   # open taps: {"device_id", "phrase", "at", "session_id"}
sessions = {}  # session_id -> Session
DRY_RUN = False


def run_session(s):
    s.start()
    while s.state == "asking_time_of_day":  # people who don't answer get their habit
        time.sleep(max(0.0, s.deadline - time.monotonic()) + 0.1)
        s.advance()


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
                return {"status": "expired", "waited_s": PAIR_WINDOW}
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
        if self.path == "/tap":
            return self._send(200, tap(device, data.get("phrase")))
        if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "answer":
            s = sessions.get(parts[1])
            if not s:
                return self._send(404, {"error": "no such session"})
            r = s.answer(device, str(data.get("answer", "")))
            if r["ok"]:
                threading.Thread(target=s.advance, daemon=True).start()
            return self._send(200 if r["ok"] else 409, {**r, "state": s.state})
        self._send(404, {"error": "not found"})

    def do_GET(self):
        if self.path.startswith("/sessions/"):
            s = sessions.get(self.path.removeprefix("/sessions/"))
            return self._send(200, s.public()) if s else self._send(404, {"error": "no such session"})
        self._send(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        if "GET /sessions" not in (fmt % args):  # pebbles poll a lot
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
    holds.start_timer(calendar, log=lambda m: print(m, flush=True))
    print(f"Ketchup tap server on {args.host}:{args.port} (dry run: {DRY_RUN})", flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
