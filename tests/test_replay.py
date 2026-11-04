"""No keys, no accounts: the two-Dot sim (normal, then --renegotiate) runs end to end on the
labelled replay of the recorded Muse Spark Open and Muse Voice Transcribe calls (replays/demo).
Starts a dry-run tap server and scripts/sim_dots.py with every key stripped from the environment.
A request that was not recorded gives a clear REPLAY MISS, not a crash."""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
ENV = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/tmp")}  # no keys, no Google SA

with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
server = subprocess.Popen([PY, str(ROOT / "server/tap_server.py"), "--dry-run", "--port", str(port)], env=ENV,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
try:
    time.sleep(3)
    for extra in ([], ["--renegotiate"]):
        r = subprocess.run([PY, str(ROOT / "scripts/sim_dots.py"), "--url", f"http://127.0.0.1:{port}", *extra],
                           env=ENV, capture_output=True, text=True, timeout=300)
        tail = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr[-500:]
        print("sim_dots", *extra, "->", tail)
        assert r.returncode == 0 and tail == "both Dots: ALL GOOD", r.stdout[-3000:] + r.stderr[-2000:]
        assert r.stdout.startswith("REPLAY: recorded real Muse Spark Open calls from 2026-11-03, no keys needed")
        assert "[replay] muse-voice-transcribe-1.0 (recorded 2026-11-03): speech to text" in r.stdout
finally:
    server.terminate()
    out = server.communicate(timeout=10)[0]
assert out.startswith("REPLAY: recorded real Muse Spark Open calls"), out[:500]
assert out.count("[replay] meta-models/muse-spark-open-1026 (recorded 2026-11-03):") >= 10, out[-2000:]
assert "REPLAY MISS" not in out and "Traceback" not in out, out[-3000:]

miss = subprocess.run([PY, "-c", "import replay; replay.lookup(replay.key('not', 'recorded'), 'a new request')"],
                      cwd=ROOT / "brain", env={**ENV, "KETCHUP_REPLAY_PACE": "0"}, capture_output=True, text=True)
assert "REPLAY MISS at step 'a new request'" in miss.stderr and "BASETEN_API_KEY" in miss.stderr, miss.stderr
print("PASS test_replay")
