"""Run a Dot and check in with the Mac's tap server while it runs (standard library only).

    python dot_heartbeat.py <dot command...>
    e.g. ~/pebvenv/bin/python ~/dot_heartbeat.py ~/pebvenv/bin/python ~/pi_pebble.py

Starts the command, then POSTs {"device_id": "pebble-<PEBBLE_ID>"} to $KETCHUP_SERVER/hello
every DOT_HELLO_S seconds (default 5) for as long as the command is alive, so the Mac prints
"DOT ONLINE ..." without anyone touching the board. A server that is down (or too old to know
/hello) is ignored: the Dot keeps running and the next check-in tries again. SIGTERM/SIGINT go
to the Dot, and this exits with the Dot's exit code, so systemd's Restart= sees the real result.
"""
import json
import os
import signal
import subprocess
import sys
import threading
import urllib.request

SERVER = os.environ.get("KETCHUP_SERVER", "http://localhost:8787").rstrip("/")
OWNER = os.environ.get("PEBBLE_ID", "mayo").lower()
DEVICE = OWNER if OWNER.startswith("pebble-") else f"pebble-{OWNER}"
EVERY = float(os.environ.get("DOT_HELLO_S", 5))


def hello():
    req = urllib.request.Request(f"{SERVER}/hello", data=json.dumps({"device_id": DEVICE}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=3) as r:
        return r.status


def beat(child, stop):
    was_up = None
    while child.poll() is None and not stop.is_set():
        try:
            hello()
            up = True
        except Exception as e:  # Mac not up yet, Wi-Fi blip, or an old server
            up, err = False, e
        if up != was_up:  # one log line per change, not every 5 s
            print(f"heartbeat {DEVICE} -> {SERVER}/hello: " + ("ok" if up else f"not reachable yet ({err})"),
                  flush=True)
            was_up = up
        stop.wait(EVERY)


def main(argv):
    if not argv:
        sys.exit(__doc__)
    child = subprocess.Popen(argv)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda s, f: (stop.set(), child.send_signal(s)))
    threading.Thread(target=beat, args=(child, stop), daemon=True).start()
    code = child.wait()
    stop.set()
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
