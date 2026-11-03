"""Both physical Dots at once against a running tap server (python server/tap_server.py --dry-run):
Mayo taps first, Frankie --gap s later (inside the server's 5 s pair window). On pairing Mayo's Dot
says "Dots connected. We will get back to you once we are done." (LED bright white, not blue, then yellow
breathing); Frankie's stays silent. Both people walk away for
--walk-away s after a time is ready: neither Dot may speak until its owner presses the pad.
While a time waits for an owner, that Dot breathes orange, except Frankie's (UNO Q): it answers after
Mayo, so it breathes yellow while Mayo's Dot is orange and turns orange once Mayo has answered.
Then Mayo on the Pi code (mock pins) talks over the first time (a press cuts the voice and opens the
mic), asks "no, pick a time at night", then keeps the new time;
Frankie on the UNO Q code (fake sketch, no mic) taps the magnet to hear each time and, a few
seconds later, keeps it with a short press. --renegotiate: Frankie's first answer is a long press
instead (find another time): both Dots breathe yellow while the agents pick again, and Mayo's Dot
must say "Frankie wants a different time" with the new time.

    python scripts/sim_dots.py [--url http://localhost:8787] [--green-s 10] [--gap 4] [--walk-away 5]
                               [--renegotiate] [--router] [--old-sketch]
                               [--pi-python /path/to/venv-with-gpiozero/bin/python]
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ap = argparse.ArgumentParser()
ap.add_argument("--url", default="http://localhost:8787")
ap.add_argument("--green-s", default="10")
ap.add_argument("--gap", type=float, default=4, help="seconds between Mayo's tap and Frankie's")
ap.add_argument("--walk-away", default="5", help="seconds both stay away once a time is ready")
ap.add_argument("--router", action="store_true")
ap.add_argument("--renegotiate", action="store_true", help="Frankie long-presses once: find another time")
ap.add_argument("--old-sketch", action="store_true", help="UNO Q sketch without breathe_rgb(): Linux steps set_led")
ap.add_argument("--pi-python", default=sys.executable, help="a Python with gpiozero (pip install gpiozero)")
a = ap.parse_args()
at = time.time() + 4
common = ["--url", a.url, "--green-s", a.green_s, "--walk-away", a.walk_away]
procs = [subprocess.Popen([a.pi_python, str(HERE / "sim_dot.py"), "--board", "pi", "--owner", "mayo",
                           "--say", "no, pick a time at night", "--interrupt", "--at", str(at), *common,
                           *(["--expect-say", "Frankie wants a different time"] if a.renegotiate else [])]),
         subprocess.Popen([sys.executable, str(HERE / "sim_dot.py"), "--board", "unoq", "--owner", "frankie",
                           "--wait", "8", "--at", str(at + a.gap), *common, *(["--router"] if a.router else []),
                           *(["--old-sketch"] if a.old_sketch else []),
                           *(["--long-press-change", "1"] if a.renegotiate else [])])]
codes = [p.wait() for p in procs]
print("both Dots:", "ALL GOOD" if codes == [0, 0] else f"FAILED {codes}")
sys.exit(0 if codes == [0, 0] else 1)
