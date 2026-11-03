"""The two-person scene: tap both Dots within the 5 s pair window -> both LEDs go bright white,
then breathe yellow while the agents pick a time in the background; Mayo's Dot (the Pi) says
"Dots connected. We will get back to you once we are done." at once (MMS made at startup, slower, a
pause after each sentence) with its LED still bright white, not blue; Frankie's (UNO Q) says
nothing; then neither Dot speaks on its own until its owner presses the pad or taps it.
No network, hardware or models: the session, LED, aplay and the voice are stubbed."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STUBS = Path(tempfile.mkdtemp(prefix="dot_connected_"))
(STUBS / "aplay").write_text('#!/bin/sh\ncat > "${FAKE_APLAY_OUT:-/dev/null}"\n')
(STUBS / "aplay").chmod(0o755)
os.environ.update(PATH=f"{STUBS}:{os.environ['PATH']}")
os.environ.pop("KETCHUP_PAIR_WINDOW_S", None)
for k in ("PEBBLE_SAY_CONNECTED", "PEBBLE_ANNOUNCE", "PEBBLE_CONNECTED_LINE"):
    os.environ.pop(k, None)

# ---- defaults per Dot (fresh interpreters: the module reads its env at import) ----------------
probe = ("import json, sys; sys.path[:0] = [%r, %r]; import {mod} as m; m = getattr(m, 'pb', m); "
         "print(json.dumps([m.OWNER, m.SAY_CONNECTED, m.ANNOUNCE, m.CONNECTED_LINE]))"
         % (str(ROOT / "pebble"), str(ROOT / "pebble" / "unoq")))
env = {k: v for k, v in os.environ.items() if k != "PEBBLE_ID"}
got = {}
for name, mod, extra in [("pi", "pi_pebble", {"PEBBLE_ID": "mayo"}), ("unoq", "unoq_dot", {}),
                         ("pi as frankie", "pi_pebble", {"PEBBLE_ID": "frankie"}),
                         ("override", "unoq_dot", {"PEBBLE_SAY_CONNECTED": "1"})]:
    out = subprocess.run([sys.executable, "-c", probe.format(mod=mod)], env={**env, **extra},
                         capture_output=True, text=True, check=True).stdout
    got[name] = json.loads(out.strip().splitlines()[-1])
LINE = "Dots connected. We will get back to you once we are done."
assert got["pi"] == ["mayo", True, "tap", LINE], got
assert got["unoq"] == ["frankie", False, "tap", LINE], got
assert got["pi as frankie"][1] is False and got["override"][1] is True, got
print("OK defaults: mayo says the connected line, frankie is silent, PEBBLE_ANNOUNCE=tap:", got)

# ---- tap server: 5 s window (env KETCHUP_PAIR_WINDOW_S), taps 4 s apart pair, waited_s is real ---
sys.path.insert(0, str(ROOT / "server"))
import tap_server as ts  # noqa: E402

assert ts.PAIR_WINDOW == 5.0, ts.PAIR_WINDOW
w = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, %r); import tap_server as t; "
                    "print(t.PAIR_WINDOW)" % str(ROOT / "server")],
                   env={**os.environ, "KETCHUP_PAIR_WINDOW_S": "7.5"}, capture_output=True, text=True, check=True)
assert w.stdout.strip().splitlines()[-1] == "7.5", w.stdout


class FakeSession:  # no agents, no calendars
    def __init__(self, *a, **k):
        self.trace = {}


ts.Session, ts.run_session = FakeSession, lambda s: None
ts.print = lambda *a, **k: None
res = {}
t = threading.Thread(target=lambda: res.update(mayo=ts.tap("pebble-mayo", "")))
t.start()
time.sleep(4.0)
res["frankie"] = ts.tap("pebble-frankie", "")
t.join()
assert res["mayo"]["status"] == res["frankie"]["status"] == "paired", res
assert res["mayo"]["session_id"] == res["frankie"]["session_id"] and 3.9 <= res["frankie"]["tap_gap_s"] < 5, res
print("OK taps 4 s apart pair:", res["frankie"])
ts.PAIR_WINDOW = 0.4
t0 = time.monotonic()
r = ts.tap("pebble-mayo", "")
assert r["status"] == "expired" and abs(r["waited_s"] - 0.4) < 0.1 and abs(r["waited_s"] - (time.monotonic() - t0)) < 0.1, r
print("OK no partner:", r)

# ---- the Dot: pairing -> connected line (Mayo only), then silence until a press ---------------
os.environ.update(PEBBLE_ID="mayo", PEBBLE_TTS="espeak")
sys.path.insert(0, str(ROOT / "pebble"))
import pi_pebble as pb  # noqa: E402

REAL_SAY = pb.say


class LED:
    value = pb.OFF


def scene(say_connected):
    leds, said, during = [], [], []
    pb.print = lambda *a, **k: (" ".join(map(str, a)).startswith("LED ") and leds.append(" ".join(map(str, a))[4:]))

    def slow_say(text):  # what say() does to the LED; slow, to prove the tap doesn't wait for it
        said.append((round(time.monotonic() - t0, 2), text))
        pb.now["speaking"] = True
        pb.refresh()
        during.append(pb.NAMES[pb.now["led"]])
        time.sleep(0.6)
        n_leds.append(len(leds))
        pb.now["speaking"] = False
        pb.refresh()

    n_leds = []
    pb.say, pb.SAY_CONNECTED, pb.led = slow_say, say_connected, LED()
    pb.now.update(sid=None, heard=None, answered=None, pending=None, engaged=False, green_until=0, rec=None,
                  led=None, gen=0, round=0, talk_until=0, mode="ready", speaking=False, hush=None, player=[],
                  edge=-1.0, busy=False, booking=False, greeting=False)
    ready = {"state": "pencilled_in", "change_round": 0,
             "questions": {pb.DEVICE: {"say": "How about Tuesday at 6 PM? Want to change it?"}}}
    polls = [{"state": "proposing", "busy": True, "change_round": 0}] * 2 + [ready] * 5
    pb.call = lambda path, body=None: {"status": "paired", "session_id": "s1"} if path == "/tap" else polls.pop(0)
    t0 = time.monotonic()
    pb.refresh()
    pb.on_tap()
    tap_s = time.monotonic() - t0
    for _ in range(2):  # the agents talk; the poll loop keeps going while the line plays
        pb.tick()
    time.sleep(0.8)
    for _ in range(5):  # a time is ready; the people have walked away
        pb.tick()
        time.sleep(0.05)
    quiet = list(said)
    pb.on_press()  # back, filmed alone: the press says the time
    deadline = time.monotonic() + 3
    while len(said) == len(quiet) and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.7)
    pb.print, pb.say = print, REAL_SAY
    return leds, quiet, said, tap_s, during, n_leds


leds, quiet, said, tap_s, during, n_leds = scene(True)
assert [x for _, x in quiet] == [LINE], quiet
assert quiet[0][0] < 0.1 and tap_s < 0.1, (quiet, tap_s)  # spoken at once; the tap didn't wait for it
assert leds == ["dim white", "bright white", "yellow breathing", "bright white",
                "blue (talking)", "bright white"], leds
assert during[0] == "bright white" and during[1] == "blue (talking)", during  # the line: white; the time: blue
assert n_leds[0] == 2, (n_leds, leds)  # while the line played the LED stayed bright white; breathing came after
assert said[-1][1] == "How about Tuesday at 6 PM? Want to change it?", said
print("OK Mayo's Dot:", " -> ".join(leds), "| said:", [x for _, x in said])

leds, quiet, said, _, during, _ = scene(False)
assert quiet == [], quiet
assert leds == ["dim white", "bright white", "yellow breathing", "bright white", "blue (talking)",
                "bright white"], leds
print("OK Frankie's Dot:", " -> ".join(leds), "| said:", [x for _, x in said])

# ---- the connected line plays from the PCM made at startup, not from the model ----------------
pcm = b"\x01\x00" * 8000
pb.mms.update(model=object())  # "loaded"
pb.ready_pcm[LINE] = pcm
pb.mms_pcm = lambda text, gap=None: (_ for _ in ()).throw(AssertionError("made on the spot: " + text))
out = STUBS / "played.raw"
os.environ["FAKE_APLAY_OUT"] = str(out)
pb.print = lambda *a, **k: None
pb.now.update(speaking=False, rec=None, hush=None, player=[])
t0 = time.monotonic()
pb.say(LINE)
pb.print = print
assert out.read_bytes() == pcm and time.monotonic() - t0 < 1.0
print(f"OK connected line played from the startup PCM in {time.monotonic() - t0:.2f} s, no model call")

# ---- how the connected line is made: one sentence at a time, slower, a pause after each ---------
from array import array  # noqa: E402
from types import SimpleNamespace  # noqa: E402

made = []
pb.mms.update(np=SimpleNamespace(int16="h", zeros=lambda n, dtype: array(dtype, [0]) * n))  # no numpy needed
pb.mms_one = lambda c, rate=pb.MMS_RATE: (made.append((c, rate)), array("h", [1]) * 1600)[1]
parts = list(pb.connected_pcm())
# MMS gets RJ's pick (clip A_dawts_connekted_we_will): "dawts connekted" for this line only
assert made == [("dawts connekted.", 0.75), ("we will get back to you once we are done.", 0.75)], made
assert pb.mms_text("Both Dots ask: Morning?") == "both dotts ask, morning?"  # other lines: unchanged
assert pb.CONNECTED_RATE == 0.75 < pb.MMS_RATE == 0.85 and pb.CONNECTED_PAUSE_S == 0.45
assert [len(x) for x in parts] == [2 * (1600 + 7200), 2 * (1600 + 3200)], [len(x) for x in parts]  # 0.45 s pause, 0.2 s tail
line, spoken = pb.CONNECTED_LINE, pb.CONNECTED_SPOKEN
pb.CONNECTED_LINE, pb.CONNECTED_SPOKEN, made[:] = "Dots connected! Hello there.", None, []
list(pb.connected_pcm())  # a custom PEBBLE_CONNECTED_LINE: through mms_text like any line
assert made == [("dotts connected!", 0.75), ("hello there.", 0.75)], made
pb.CONNECTED_LINE, pb.CONNECTED_SPOKEN = line, spoken
print("OK connected line: 'dawts connekted.' + 'we will get back to you once we are done.' at speaking_rate "
      "0.75 (others 0.85), 450 ms between, 200 ms tail; custom lines go through mms_text")

# ---- espeak (before MMS loads): the connected line is slower, other lines aren't --------------
(STUBS / "espeak-ng").write_text('#!/bin/sh\necho "$@" >> "$FAKE_ESPEAK_LOG"\n')
(STUBS / "espeak-ng").chmod(0o755)
os.environ["FAKE_ESPEAK_LOG"] = str(STUBS / "espeak.log")
pb.mms.clear()
pb.print = lambda *a, **k: None
pb.say(LINE)
pb.say("How about Tuesday at 6 PM?")
pb.print = print
calls = (STUBS / "espeak.log").read_text().splitlines()
assert calls == ["-s 130 --stdout " + LINE, "--stdout How about Tuesday at 6 PM?"], calls
print("OK espeak fallback:", calls)
print("test_dot_connected: OK")
