"""The physical Dots' LED flow (pi_pebble.py, also run by the UNO Q's unoq_dot.py):
white -> white breathing while the agents talk -> white when a time is ready (said on a tap or
press) -> yellow only while the FSR is held -> breathing again after a change -> green when
booked, then white. No red. Also: the tap server answers polls while the agents hold the
session, and flow.negotiate_first asks the owners nothing before the agents pick a time.
Server responses, the LED, and speech are stubbed: no network, no hardware, no model calls."""
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.update(PEBBLE_ID="mayo", PEBBLE_TALK_MIN_S="0.3", PEBBLE_GREEN_S="0.3")
sys.path.insert(0, str(ROOT / "pebble"))
sys.path.insert(0, str(ROOT / "pebble" / "unoq"))
import pi_pebble as pb  # noqa: E402
import unoq_dot  # noqa: E402

assert not hasattr(pb, "RED"), "red is gone"
assert abs(pb.breath_level(0) - 1) < 1e-9 and abs(pb.breath_level(pb.BREATHE_S / 2) - 0.1) < 1e-9
assert all(0.1 - 1e-9 <= pb.breath_level(t / 10) <= 1 + 1e-9 for t in range(100))


class LED:  # what gpiozero.RGBLED looks like to pi_pebble
    value = pb.OFF


def reset(said, board):
    pb.now.update(sid="s1", heard=None, answered=None, pending=None, engaged=False, green_until=0, rec=None,
                  led=None, gen=0, round=0, talk_until=0)
    pb.led = unoq_dot.BridgeLED(board) if board else LED()
    pb.say = said.append


def run(leds, said, server, board=None):
    """Feed tick() one fake server reply per poll."""
    for reply in server:
        pb.call = lambda path, body=None, r=reply: r
        pb.tick()
        if callable(reply.get("then")):
            reply["then"]()
        time.sleep(reply.get("sleep", 0))
    return leds


def q(say, rnd=0):
    return {"state": "pencilled_in", "change_round": rnd, "questions": {pb.DEVICE: {"say": say}}}


def flow(board=None):
    leds, said = [], []
    real_print = pb.print if hasattr(pb, "print") else print
    def capture(*a, **k):
        line = " ".join(map(str, a))
        if line.startswith("LED ") and not line.startswith("LED green for"):
            leds.append(line[4:])
    pb.print = capture
    try:
        reset(said, board)
        pb.breathe()  # what on_tap does once paired
        run(leds, said, [
            {"state": "proposing", "busy": True, "change_round": 0},  # agents talking, lock held
            q("How about Tuesday at 6 PM? Want to change it?"),  # ready: white, waits for a tap/press
        ], board)
        assert said == [], "not said until the owner taps or presses"
        pb.announce(again=True)  # a tap
        assert said == ["How about Tuesday at 6 PM? Want to change it?"], said
        pb.now["rec"] = object()  # FSR held: yellow, and nothing else may show over it
        pb.show(pb.YELLOW)
        pb.breathe()
        pb.show(pb.WHITE)
        pb.now["rec"] = None
        pb.show(pb.WHITE)
        pb.now.update(answered=pb.now["pending"], pending=None, engaged=True)  # reply(): a change
        pb.breathe()
        run(leds, said, [
            {"state": "pencilled_in", "change_round": 1, "questions": {}},  # backup: instant, still breathe
            q("You said at night. How about Wednesday at 8 PM? Want to change it?", 1),  # inside TALK_MIN_S
            {**q("You said at night. How about Wednesday at 8 PM? Want to change it?", 1), "sleep": 0.35},
            q("You said at night. How about Wednesday at 8 PM? Want to change it?", 1),  # engaged: said now
            {"state": "booked", "change_round": 1, "speak": {pb.DEVICE: "Plan booked with Frankie."}},
        ], board)
        pb.now["sid"] = None
        pb.tick()  # still green
        time.sleep(0.35)
        pb.tick()  # green is over: white
    finally:
        pb.print = real_print
    return leds, said


leds, said = flow()
want = ["white breathing", "white", "yellow", "white", "white breathing", "white", "green", "white"]
assert leds == want, leds
assert said[1:] == ["You said at night. How about Wednesday at 8 PM? Want to change it?",
                    "Plan booked with Frankie."], said
print("OK Pi LED flow:", " -> ".join(leds))

board = unoq_dot.FakeBoard()
leds, _ = flow(board)
assert leds == want, leds
assert [w for w in board.writes if w[0] == "breathe"] == [("breathe", 3000)] * 2, board.writes
assert board.writes[-1] == (255, 255, 255) and (255, 255, 0) in board.writes and (0, 255, 0) in board.writes
print("OK UNO Q LED flow: the sketch breathes on its own:", board.writes)

old = unoq_dot.FakeBoard()
real = old.call
old.call = lambda name, *a: (_ for _ in ()).throw(ValueError("no breathe")) if name == "breathe" else real(name, *a)
leds, _ = flow(old)
assert leds == want, leds
print(f"OK UNO Q old sketch (no breathe): Linux stepped set_led {len(old.writes)} times instead")

# ---- tap server: a poll while the agents hold the lock answers at once ----------
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "brain"))
import tap_server  # noqa: E402


class S:
    sid, state, change_round = "s1", "proposing", 0
    lock = threading.RLock()

    def public(self):
        return {"session_id": self.sid, "state": self.state, "questions": {"x": 1}}


s = S()
assert tap_server.snapshot(s) == s.public()
got, held = {}, threading.Event()
t = threading.Thread(target=lambda: (s.lock.acquire(), held.set(), time.sleep(0.5), s.lock.release()))
t.start()
held.wait()
t0 = time.monotonic()
got = tap_server.snapshot(s)
assert time.monotonic() - t0 < 0.1 and got["busy"] and got["state"] == "proposing" and got["questions"] == {}
t.join()
print("OK tap server: polls answer at once while the agents talk:", got)

# ---- flow: Dots negotiate first, no time-of-day question --------------------------
import flow  # noqa: E402

calls = []
sess = flow.Session.__new__(flow.Session)
sess.lock, sess.state, sess.questions = threading.RLock(), "paired", {}
sess.start = lambda ask=True: calls.append(("start", ask))
sess.propose = lambda: calls.append(("propose", sess.lock._is_owned()))
sess.negotiate_first()
assert calls == [("start", False), ("propose", True)] and sess.questions == {}, calls
print("OK flow.negotiate_first: plan without asking, then the agents propose (under the lock)")
