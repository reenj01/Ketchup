"""The physical Dots' LED flow and mic toggle (pi_pebble.py, also run by the UNO Q's unoq_dot.py):
dim white at rest -> bright white on the tap -> yellow breathing while the agents decide ->
orange breathing while a time waits for the owner -> blue while the Dot talks (then orange again) -> solid yellow while the mic is
on (press once = on, press again = off and send; 50 ms debounce; auto-stop) -> yellow breathing from mic off
until the Dot answers (never green, never solid yellow) -> yellow breathing again after a change -> green when booked, then dim white. A press while the Dot talks stops the
voice and opens the mic. MMS silence trimming. Also: the tap server answers polls while the agents
hold the session, and flow.negotiate_first asks the owners nothing before the agents pick a time.
Server, LED, mic (arecord) and speaker (aplay/espeak) are stubbed: no network, hardware or models."""
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STUBS = Path(tempfile.mkdtemp(prefix="dot_stubs_"))
for name, body in {
    "arecord": '#!/bin/sh\nfor a; do out="$a"; done\nhead -c 32044 /dev/zero > "$out"\n'
               'trap "exit 0" TERM INT\nwhile :; do sleep 0.02; done\n',
    "espeak-ng": "#!/bin/sh\nhead -c 4000 /dev/zero\n",
    "aplay": "#!/bin/sh\ncat > /dev/null\nexec sleep ${FAKE_PLAY_S:-0}\n",
}.items():
    (STUBS / name).write_text(body)
    (STUBS / name).chmod(0o755)
os.environ.update(PEBBLE_ID="mayo", PEBBLE_TALK_MIN_S="0.3", PEBBLE_GREEN_S="0.3", PEBBLE_TTS="espeak",
                  PATH=f"{STUBS}:{os.environ['PATH']}")
sys.path.insert(0, str(ROOT / "pebble"))
sys.path.insert(0, str(ROOT / "pebble" / "unoq"))
import pi_pebble as pb  # noqa: E402
import unoq_dot  # noqa: E402
unoq_dot.LED_ORDER = "rgb"  # board.writes read as (r, g, b) here; the grb remap is in test_unoq_press.py

assert not hasattr(pb, "RED"), "red is gone"
# imported (as unoq_dot does): the UNO Q's mix. Run as the Pi's script: less green, since the Pi LED's
# green die swamps red (255,255,0 read green there while the mic was on).
assert (pb.YELLOW, pb.ORANGE) == ((1, 1, 0), (1, 90 / 255, 0)), (pb.YELLOW, pb.ORANGE)
import subprocess  # noqa: E402
FAKE_GPIO = STUBS / "gpio"
FAKE_GPIO.mkdir()
(FAKE_GPIO / "gpiozero.py").write_text(  # main() builds the LED first: print the Pi's colours and stop
    "import sys\nButton = None\nclass RGBLED:\n    def __init__(self, **k):\n        g = sys._getframe(1).f_globals\n"
    "        print([round(v * 255) for c in (g['YELLOW'], g['ORANGE']) for v in c])\n        raise SystemExit(0)\n")
pi = subprocess.run([sys.executable, str(ROOT / "pebble" / "pi_pebble.py")], capture_output=True, text=True,
                    env={**os.environ, "PYTHONPATH": str(FAKE_GPIO)}, timeout=60)
assert pi.stdout.strip().splitlines()[-1] == "[255, 90, 0, 255, 32, 0]", (pi.stdout, pi.stderr[-500:])
pi = subprocess.run([sys.executable, str(ROOT / "pebble" / "pi_pebble.py")], capture_output=True, text=True,
                    env={**os.environ, "PYTHONPATH": str(FAKE_GPIO), "PEBBLE_YELLOW": "255,120,0"}, timeout=60)
assert pi.stdout.strip().splitlines()[-1] == "[255, 120, 0, 255, 32, 0]", pi.stdout
print("OK Pi colours: yellow 255,90,0 and orange 255,32,0 on the Pi (PEBBLE_YELLOW / PEBBLE_ORANGE); UNO Q keeps 255,255,0 / 255,90,0")
assert abs(pb.breath_level(0) - 1) < 1e-9 and abs(pb.breath_level(pb.BREATHE_S / 2) - 0.1) < 1e-9
assert all(0.1 - 1e-9 <= pb.breath_level(t / 10) <= 1 + 1e-9 for t in range(100))
REAL_SAY, REAL_PRINT = pb.say, print


class LED:  # what gpiozero.RGBLED looks like to pi_pebble
    value = pb.OFF


def wait_for(cond, s=3.0):
    t = time.monotonic() + s
    while not cond():
        assert time.monotonic() < t, "timed out"
        time.sleep(0.01)


def reset(board):
    pb.now.update(sid=None, heard=None, answered=None, pending=None, engaged=False, green_until=0, rec=None,
                  led=None, gen=0, round=0, talk_until=0, mode="ready", speaking=False, hush=None, player=[],
                  edge=-1.0, busy=False, booking=False, greeting=False)
    pb.SAY_CONNECTED = False  # the "Dots connected" line on pairing: tests/test_dot_connected.py
    pb.led = unoq_dot.BridgeLED(board) if board else LED()


def capture(leds, mics):
    def p(*a, **k):
        line = " ".join(map(str, a))
        if line.startswith("LED ") and not line.startswith("LED green for"):
            leds.append(line[4:])
        if line.startswith("MIC "):
            mics.append(line.split(" -")[0].strip())
    pb.print = p


def q(say, rnd=0):
    return {"state": "pencilled_in", "change_round": rnd, "questions": {pb.DEVICE: {"say": say}}}


def press(hold=0.08, gap=0.08):
    """A clean press: down, held, up, then a pause (longer than the 50 ms debounce)."""
    pb.on_press()
    time.sleep(hold)
    pb.on_release()
    time.sleep(gap)


def fake_say(said):
    def say(text):  # what say() does to the LED, without audio
        said.append(text)
        if pb.now["rec"]:
            return
        pb.now["speaking"] = True
        pb.refresh()
        time.sleep(0.05)
        pb.now["speaking"] = False
        pb.refresh()
    return say


def flow(board=None):
    leds, mics, said, polls = [], [], [], []
    capture(leds, mics)
    pb.say = fake_say(said)
    pb.transcribe = lambda wav: "no, pick a time at night"
    answer = {"ok": True, "mapped_to": "change", "say": "Got it, I'll look at night."}
    pb.call = lambda path, body=None: (
        {"status": "paired", "session_id": "s1"} if path == "/tap" else
        answer if path.endswith("/answer") else polls.pop(0))
    try:
        reset(board)
        pb.refresh()  # start: dim white
        pb.on_tap()  # bright white, then paired: yellow breathing
        for reply in [{"state": "proposing", "busy": True, "change_round": 0},
                      q("You and Frankie can do Tuesday at 6 PM. Want to change it?")]:
            polls.append(reply)
            pb.tick()
        assert said == [], "not said until the owner taps or presses"
        press()  # a new time: this press hears it (blue)
        wait_for(lambda: said and not pb.now["speaking"])
        assert said == ["You and Frankie can do Tuesday at 6 PM. Want to change it?"], said
        pb.on_press()  # mic ON, at once
        assert pb.now["rec"], "mic on from the first edge"
        pb.on_release(); time.sleep(0.01); pb.on_press(); time.sleep(0.01); pb.on_release()  # bounce: ignored
        time.sleep(0.4)
        assert pb.now["rec"] and mics == ["MIC ON"], mics
        press()  # mic OFF -> sent -> "Got it" (blue) -> agents decide again (yellow breathing)
        assert not pb.now["rec"] and mics == ["MIC ON", "MIC OFF"], mics
        wait_for(lambda: len(said) == 2 and not pb.now["speaking"])
        for reply in [{"state": "pencilled_in", "change_round": 1, "questions": {}},  # backup: instant
                      q("You said at night. You and Frankie can do Wednesday at 8 PM. Want to change it?", 1)]:
            polls.append(reply)
            pb.tick()
        time.sleep(0.35)
        polls.append(q("You said at night. You and Frankie can do Wednesday at 8 PM. Want to change it?", 1))
        pb.tick()  # engaged: said as soon as it is ready
        polls.append({"state": "booked", "change_round": 1, "speak": {pb.DEVICE: "Plan booked with Frankie."}})
        pb.tick()
        pb.tick()  # still green
        time.sleep(0.35)
        pb.tick()  # green is over: dim white
    finally:
        pb.print, pb.say = REAL_PRINT, REAL_SAY
    return leds, mics, said


O = "orange breathing"  # a time waits for this owner's answer
want = ["dim white", "bright white", "yellow breathing", O, "blue (talking)", O,
        "yellow (mic on)", "yellow breathing", "blue (talking)", "yellow breathing", O,
        "blue (talking)", O, "blue (talking)", "green", "dim white"]
leds, mics, said = flow()
assert leds == want, leds
assert said[1:] == ["Got it, I'll look at night.", "You said at night. You and Frankie can do Wednesday at 8 PM. Want to change it?",
                    "Plan booked with Frankie."], said
print("OK Pi LED flow:", " -> ".join(leds))
print("OK mic toggle:", mics, "(the bounce inside the press made no extra toggle)")

board = unoq_dot.FakeBoard()
leds, _, _ = flow(board)
assert leds == want, leds
breaths = [w for w in board.writes if w[0] == "breathe"]
assert breaths.count(("breathe", 3000, 255, 255, 0)) == 3 and breaths.count(("breathe", 3000, 255, 90, 0)) == 4, breaths
assert len(breaths) == 7, breaths
assert board.writes[-1] == (31, 31, 31) and (255, 255, 0) in board.writes and (0, 0, 255) in board.writes
assert (0, 255, 0) in board.writes and (255, 255, 255) in board.writes
print("OK UNO Q LED flow: the sketch breathes yellow and orange on its own:", board.writes)

old = unoq_dot.FakeBoard()
real = old.call
old.call = lambda name, *a: (_ for _ in ()).throw(ValueError("no breathe_rgb")) if name == "breathe_rgb" \
    else real(name, *a)
leds, _, _ = flow(old)
assert leds == want, leds
print(f"OK UNO Q old sketch (no breathe_rgb): Linux stepped set_led {len(old.writes)} times instead")

# ---- a press while the Dot talks cuts the voice and opens the mic; auto-stop ----------------
leds, mics = [], []
capture(leds, mics)
reset(None)
pb.mms.clear()  # espeak path, through the stub aplay that "plays" for 5 s
os.environ["FAKE_PLAY_S"] = "5"
sent = []
pb.reply = lambda wav: sent.append(len(wav))
try:
    t = threading.Thread(target=pb.say, args=("A long line the owner talks over.",))
    t.start()
    wait_for(lambda: pb.now["player"] and pb.now["speaking"])
    time.sleep(0.2)
    t0 = time.monotonic()
    pb.on_press()
    t.join(1.0)
    cut = time.monotonic() - t0
    assert not t.is_alive() and cut < 0.5, "the voice stopped at once"
    assert pb.now["rec"] and mics == ["MIC ON"], mics
    pb.on_release()
    press()  # a second hit inside REC_MIN_S (0.3 s) of mic ON is a double hit: still on
    assert pb.now["rec"] and mics == ["MIC ON"], mics
    time.sleep(0.3)
    press()  # now it stops
    wait_for(lambda: not pb.now["rec"])
    pb.REC_MAX_S = 0.3
    assert mics == ["MIC ON", "MIC OFF"], mics
    pb.mic_on()  # nobody presses: it stops on its own
    wait_for(lambda: not pb.now["rec"], 2)
    assert mics[-1] == "MIC OFF" and len(mics) == 4, mics
    wait_for(lambda: len(sent) == 2)
finally:
    pb.print = REAL_PRINT
    os.environ["FAKE_PLAY_S"] = "0"
print(f"OK interrupt: a press stopped the voice in {cut * 1000:.0f} ms and opened the mic; LEDs:", " -> ".join(leds),
      "; double hit ignored; auto-stop works:", mics)

# ---- MMS output: silence trimmed, long pauses shortened ---------------------------------------
try:
    import numpy as np
except ImportError:
    np = None
if np is not None:
    tone = lambda s: 0.5 * np.sin(np.arange(int(16000 * s)) / 3.0)  # noqa: E731
    z = lambda s: np.zeros(int(16000 * s))  # noqa: E731
    a = np.concatenate([z(0.5), tone(1.0), z(0.6), tone(1.0), z(0.4)])
    out = pb.trim(a, np)
    assert abs(len(out) / 16000 - (2.0 + 0.2 + 0.04)) < 0.03, len(out) / 16000
    print(f"OK trim: 3.5 s with 0.5 s lead, 0.6 s pause, 0.4 s tail -> {len(out) / 16000:.2f} s")

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
