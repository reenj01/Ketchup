"""Booked while the Dot is still saying its press's reply (the UNO Q says it in a thread): the green
must last the full PEBBLE_GREEN_S after the voice stops, not run out behind the blue.
No network, hardware or models: the server, the LED and the voice are stubbed."""
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.update(PEBBLE_TTS="espeak", PEBBLE_GREEN_S="1.0")
sys.path.insert(0, str(ROOT / "pebble"))
sys.path.insert(0, str(ROOT / "pebble" / "unoq"))
import unoq_dot as uq  # noqa: E402
pb = uq.pb

board = uq.FakeBoard()
pb.led, pb.fsr, pb.hall = uq.BridgeLED(board), uq.BridgeFSR(), None
pb.wanted = uq.wanted
pb.call = lambda path, *a, **k: {"state": "booked", "speak": {}}  # this Dot's line was the press's reply
pb.now.update(sid="s1", pending=None, mode="ready")

REPLY_S = 1.5


def reply():  # stands in for say(r["say"]) after the short press
    with pb.speaking:
        pb.now["speaking"] = True
        pb.refresh()
        time.sleep(REPLY_S)
        pb.now["speaking"] = False
        pb.refresh()


t = threading.Thread(target=reply)
t.start()
time.sleep(0.05)
pb.tick()
quiet_at = time.monotonic()
t.join()
assert pb.wanted() == pb.GREEN, f"green right after the voice stops, got {pb.wanted()}"
left = pb.now["green_until"] - quiet_at
assert left > 0.9, f"green should last the full 1.0 s after the voice, only {left:.2f} s left"
time.sleep(1.1)
assert pb.wanted() == pb.DIM_WHITE, "then dim white"
print("green after reply: OK (full green after the voice, then dim white)")
