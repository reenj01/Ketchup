"""Tiny tap-pairing server (standard library only).

    python server/tap_server.py [--port 8787] [--dry-run]

POST /tap {"device_id": "pebble-mayo", "phrase": "let's catch up Thursday"}
    Waits up to 2.0 s for a tap from a different pebble. Both requests get the
    same {"status": "paired", "session_id": ...}; a tap nobody matches comes
    back {"status": "expired"}. On pairing, the agents negotiate and book in
    the background, using the phrase heard by the listening pebble.
GET /sessions/<id>
    {"status": "negotiating" | "booked" | "agreed (dry run, not booked)" | "failed", ...}
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
from gcal import PEOPLE  # noqa: E402
from pipeline import run  # noqa: E402

PAIR_WINDOW = 2.0
DEVICES = {p["device_id"]: key for key, p in PEOPLE.items()}
LISTENER = "pebble-mayo"  # the Pi pebble with the mic

lock = threading.Condition()
pending = []   # open taps: {"id", "device_id", "phrase", "at", "session_id"}
sessions = {}  # session_id -> public status dict
DRY_RUN = False


def run_session(sid, phrase, proposer):
    log = []
    sessions[sid]["log"] = log
    try:
        trace = run(phrase, proposer, dry_run=DRY_RUN, log=log.append, source=f"tap-{sid}")
        sessions[sid].update(
            status=trace["status"], agreed_slot=trace.get("agreed_slot"),
            agent_turns=[{"round": t["round"], "agent": t["agent"], **t["reply"]}
                         for t in trace.get("agent_turns", [])],
            created_events=trace.get("created_events"), trace_file=trace["trace_file"])
    except Exception as e:
        sessions[sid].update(status="failed", error=f"{type(e).__name__}: {e}")


def tap(device_id, phrase):
    me = {"id": uuid.uuid4().hex[:8], "device_id": device_id, "phrase": phrase,
          "at": time.monotonic(), "session_id": None}
    with lock:
        now = me["at"]
        pending[:] = [t for t in pending if now - t["at"] <= PAIR_WINDOW and not t["session_id"]]
        partner = next((t for t in pending if t["device_id"] != device_id), None)
        if partner:
            pending.remove(partner)
            sid = uuid.uuid4().hex[:8]
            taps = {t["device_id"]: t for t in (partner, me)}
            heard = taps.get(LISTENER) or partner
            phrase = heard["phrase"] or partner["phrase"] or me["phrase"]
            gap = round(me["at"] - partner["at"], 2)
            sessions[sid] = {"session_id": sid, "status": "negotiating", "dry_run": DRY_RUN,
                             "devices": [partner["device_id"], device_id],
                             "tap_gap_s": gap, "phrase": phrase}
            partner["session_id"] = me["session_id"] = sid
            lock.notify_all()
            threading.Thread(target=run_session, daemon=True,
                             args=(sid, phrase, DEVICES[heard["device_id"]])).start()
            return {"status": "paired", "session_id": sid, "partner": partner["device_id"],
                    "tap_gap_s": gap}
        pending.append(me)
        lock.wait_for(lambda: me["session_id"], timeout=PAIR_WINDOW)
        if me in pending:
            pending.remove(me)
        if me["session_id"]:
            s = sessions[me["session_id"]]
            return {"status": "paired", "session_id": s["session_id"],
                    "partner": s["devices"][1], "tap_gap_s": s["tap_gap_s"]}
        return {"status": "expired", "waited_s": PAIR_WINDOW}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/tap":
            return self._send(404, {"error": "not found"})
        try:
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "body must be JSON"})
        device = data.get("device_id")
        if device not in DEVICES:
            return self._send(400, {"error": f"unknown device_id, expected one of {list(DEVICES)}"})
        self._send(200, tap(device, data.get("phrase")))

    def do_GET(self):
        if self.path.startswith("/sessions/"):
            s = sessions.get(self.path.removeprefix("/sessions/"))
            return self._send(200, s) if s else self._send(404, {"error": "no such session"})
        self._send(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {fmt % args}", flush=True)


def main():
    global DRY_RUN
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--dry-run", action="store_true", help="negotiate but never book")
    args = ap.parse_args()
    DRY_RUN = args.dry_run
    print(f"Ketchup tap server on {args.host}:{args.port} (dry run: {DRY_RUN})", flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
