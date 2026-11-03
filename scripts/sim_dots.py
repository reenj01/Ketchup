"""Both physical Dots at once against a running tap server (python server/tap_server.py --dry-run):
Mayo on the Pi code (mock pins) asks "no, pick a time at night", then keeps the new time;
Frankie on the UNO Q code (fake sketch) keeps every time it hears, a few seconds later.

    python scripts/sim_dots.py [--url http://localhost:8787] [--green-s 6] [--router] [--old-sketch]
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
ap.add_argument("--green-s", default="6")
ap.add_argument("--router", action="store_true")
ap.add_argument("--old-sketch", action="store_true", help="UNO Q sketch without breathe(): Linux steps set_led")
ap.add_argument("--pi-python", default=sys.executable, help="a Python with gpiozero (pip install gpiozero)")
a = ap.parse_args()
at = str(time.time() + 4)
common = ["--url", a.url, "--green-s", a.green_s, "--at", at]
procs = [subprocess.Popen([a.pi_python, str(HERE / "sim_dot.py"), "--board", "pi", "--owner", "mayo",
                           "--say", "no, pick a time at night", *common]),
         subprocess.Popen([sys.executable, str(HERE / "sim_dot.py"), "--board", "unoq", "--owner", "frankie",
                           "--wait", "8", *common, *(["--router"] if a.router else []),
                           *(["--old-sketch"] if a.old_sketch else [])])]
codes = [p.wait() for p in procs]
print("both Dots:", "ALL GOOD" if codes == [0, 0] else f"FAILED {codes}")
sys.exit(0 if codes == [0, 0] else 1)
