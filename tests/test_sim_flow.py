"""The simulator follows the Dot flow (no network): python tests/test_sim_flow.py

On pairing the sim runs flow.Session.negotiate_first (no "morning, noon, or night?"
up front). While the agents renegotiate (session lock held), a poll shows the live
state with nothing to answer, not the old question. The page has no red LED and no
time-of-day question.
"""
import os
import sys
import threading
import time
from pathlib import Path

os.environ["KETCHUP_CALENDAR"] = "mock"
os.environ["KETCHUP_TTS"] = "off"
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "brain"))
import flow  # noqa: E402
import sim  # noqa: E402

ran = []
flow.Session.start = lambda self, *a, **k: ran.append(("start", k))
flow.Session.negotiate_first = lambda self: ran.append(("negotiate_first",))
s = sim.Sim("let's hang out next week")
for _ in range(50):
    if ran:
        break
    time.sleep(0.02)
assert ran == [("negotiate_first",)], ran

# a time is pencilled in and both Dots are asked to keep it or change it
s._p = {"state": "pencilled_in", "questions": {d: {"say": "How about Tuesday at 7:30 PM? Want to change it?", "kind": "confirm"}
                                               for d in sim.DEV.values()},
        "speak": {}, "hold": None}
s.s.state = "pencilled_in"
s.s.public = lambda: s._p
v = s.view()
assert v["state"] == "pencilled_in" and all(p["question"] for p in v["pebbles"].values()), v

# Mayo asks for a change: the agents hold the lock and go back to proposing
held, release = threading.Event(), threading.Event()
def negotiate():
    with s.s.lock:
        s.s.state = "proposing"
        held.set()
        release.wait(5)
t = threading.Thread(target=negotiate)
t.start()
held.wait(5)
v = s.view()
release.set()
t.join()
assert v["state"] == "proposing" and not any(p["question"] for p in v["pebbles"].values()), v
assert "seconds_left" not in v and "change_round" in v

html = (ROOT / "web" / "sim.html").read_text()
assert "Morning, noon, or night" not in html and "asking_time_of_day" not in html
assert "led-red" not in html and "--led-red" not in html
for cls in ("led-bright", "led-breathe", "led-blue", "led-yellow", "led-green"):
    assert f".pebble.{cls}" in html, cls
assert "You and Frankie mentioned hanging out next week." in html and "let's hang out next week" in html
print("ok: sim pairs with negotiate_first; busy polls show no stale question; page LEDs match the Dot")
