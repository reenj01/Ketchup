"""Ketchup Dot on an Arduino UNO Q: same Dot as pebble/pi_pebble.py, different pins.

The UNO Q has two halves. The microcontroller runs ketchup-dot/sketch/sketch.ino (LED, Hall
sensor, FSR). This script runs on the board's Linux side and does everything else exactly the
way the Pi does it, by importing pi_pebble.py and reusing its code: tap -> POST /tap, poll
GET /sessions/<id>, LED colours (dim white, bright white, yellow breathing, blue, solid
yellow, green), the mic with arecord, Muse Voice Transcribe, POST /sessions/<id>/answer,
pipelined MMS-TTS voice with espeak-ng fallback. The pins are different: instead of gpiozero, the
LED and the two inputs are reached over the Arduino Bridge (arduino-router, MessagePack-RPC):

    Bridge.call("set_led", r, g, b)   0..255 each (solid colour)
    Bridge.call("breathe_rgb", ms, r, g, b)  yellow breathing while the agents decide, run on
                                      the MCU (an older sketch without it: Linux steps set_led)
    Bridge.call("get_inputs")         -> taps * 2 + pressed   (polled every 25 ms)

The pad (FSR) is the one other difference: a press is short or long here (the Pi toggles the mic).
  long press  = held PEBBLE_LONG_PRESS_S (default 1.0 s): keep the proposed time (approve it). When
                the hold reaches it the LED flashes green, the terminal says "LONG PRESS: keep", and
                "keep it" goes to POST /sessions/<id>/answer (a recording in progress is dropped).
  short press = never approves. With a mic (PEBBLE_MIC=auto finds the USB card with arecord -l;
                1 / 0 forces it): what a press does on the Pi (hear a new time, else mic on / off,
                and it cuts the voice off). Without a mic: says the proposed time again.
Short presses act on release, so a press can still turn into a long one.

Same environment variables as the Pi: KETCHUP_SERVER, PEBBLE_ID (defaults to frankie here),
PEBBLE_AUDIO, PEBBLE_PHRASE, PEBBLE_GREEN_S, PEBBLE_TTS, PEBBLE_ANNOUNCE (default tap),
PEBBLE_SAY_CONNECTED (default 0 here: Frankie's Dot is silent on pairing), META_API_KEY. Extra: APP_SOCKET
(router address, default unix:///var/run/arduino-router.sock), DOT_POLL_MS (default 25).

    python3 unoq_dot.py          # on the UNO Q, with the ketchup-dot App running (sketch flashed)
    python3 unoq_dot.py --mock   # anywhere: fake board, type t = tap, p = press, r = release

Bridge docs: https://docs.arduino.cc/software/app-lab/bridge/bridge-api/
Python package: https://pypi.org/project/arduino-router-bridge/
"""
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault("PEBBLE_ID", "frankie")  # this board is Frankie's Dot
sys.path.insert(0, os.environ.get("PEBBLE_DIR", str(Path(__file__).resolve().parent.parent)))
import pi_pebble as pb  # noqa: E402  (reads the env vars above at import, like on the Pi)

POLL_S = float(os.environ.get("DOT_POLL_MS", 25)) / 1000
LONG_PRESS_S = float(os.environ.get("PEBBLE_LONG_PRESS_S", 1.0))  # hold this long = keep it
MIC = os.environ.get("PEBBLE_MIC", "auto").lower()  # auto | 1 | 0
FLASH_S = 0.4  # the green flash when a hold becomes a long press
KEEP_TEXT = "keep it"  # what a long press answers (the server reads it as keep without a model)
press = {"down": None, "long": False, "edge": -1.0, "gen": 0, "mic": None, "flash_until": 0.0}


def has_mic():
    """PEBBLE_MIC=1/0 decides; auto: arecord is there and `arecord -l` lists the PEBBLE_AUDIO card."""
    if MIC in ("1", "on", "yes", "true"):
        return True
    if MIC in ("0", "off", "no", "false"):
        return False
    if not shutil.which("arecord"):
        return False
    try:
        out = subprocess.run(["arecord", "-l"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return False
    card = re.search(r"CARD=([^,]+)", pb.CARD)
    return "card " in out and (not card or card[1] in out)


_wanted = pb.wanted


def wanted():
    """pi_pebble's colour, except for the short green flash of a long press."""
    return pb.GREEN if time.monotonic() < press["flash_until"] else _wanted()


def on_down():
    """FSR down: start timing. Edges within pi_pebble's DEBOUNCE_S of the last one are bounce."""
    t = time.monotonic()
    quiet, press["edge"] = t - press["edge"] >= pb.DEBOUNCE_S, t
    if not quiet or press["down"] is not None:
        return
    press.update(down=t, long=False, gen=press["gen"] + 1)
    timer = threading.Timer(LONG_PRESS_S, long_press, args=(press["gen"],))
    timer.daemon = True
    timer.start()


def on_up():
    """FSR up: a press that never reached LONG_PRESS_S is a short press."""
    t = time.monotonic()
    quiet, press["edge"] = t - press["edge"] >= pb.DEBOUNCE_S, t
    if not quiet or press["down"] is None:
        return
    was_long = press["long"]
    press.update(down=None, long=False)
    if not was_long:
        short_press()


def short_press():
    """Never approves. Mic: the Pi's press (hear a new time, else mic on/off, cuts the voice off).
    No mic: stop the voice, or say the proposed time again."""
    if press["mic"] is None:
        press["mic"] = has_mic()
    if press["mic"]:
        print("SHORT PRESS: mic", flush=True)
        return pb.toggle()
    if pb.hush():
        print("SHORT PRESS: stopped the voice", flush=True)
        return
    if pb.now["pending"]:
        print("SHORT PRESS: the time again", flush=True)
        threading.Thread(target=pb.announce, kwargs={"again": True}, daemon=True).start()
    else:
        print("SHORT PRESS: no time to say yet", flush=True)


def long_press(gen):
    """Still held at LONG_PRESS_S: keep the proposed time."""
    if gen != press["gen"] or press["down"] is None or press["long"] or not pb.fsr.is_pressed:
        return
    press["long"] = True
    print("LONG PRESS: keep", flush=True)
    press["flash_until"] = time.monotonic() + FLASH_S
    pb.refresh()  # green flash
    threading.Timer(FLASH_S + 0.02, pb.refresh).start()
    keep()


def drop_recording():
    """A long press while the mic is on: stop it and throw the audio away (nothing is sent)."""
    with pb.mic_lock:
        rec, path = pb.now["rec"], pb.now["wav"]
        if not rec:
            return
        rec.terminate()
        rec.wait()
        pb.now["rec"] = None
    try:
        os.remove(path)
    except OSError:
        pass
    print("MIC OFF - dropped (long press)", flush=True)


def keep():
    drop_recording()
    pb.hush()
    sid = pb.now["sid"]
    if not sid:
        print("LONG PRESS: nothing to keep (no time proposed)", flush=True)
        return pb.refresh()
    try:
        r = pb.call(f"/sessions/{sid}/answer", {"device_id": pb.DEVICE, "answer": KEEP_TEXT})
    except Exception as e:
        r = {"ok": False, "error": str(e)}
    print("ANSWER", {k: r.get(k) for k in ("ok", "mapped_to", "error", "state")}, flush=True)
    if not r.get("ok") or r.get("mapped_to") != "keep":
        print("LONG PRESS: not kept -", r.get("error") or r.get("mapped_to"), flush=True)
        return pb.refresh()
    pb.now.update(answered=pb.now["pending"], pending=None, engaged=True)
    if r.get("say"):
        threading.Thread(target=pb.say, args=(r["say"],), daemon=True).start()
    pb.refresh()


class RouterBoard:
    """The real sketch, over arduino-router. One lock: calls come from the poll loop, the
    tick loop and reply threads."""

    def __init__(self):
        from arduino.router_bridge import DEFAULT_ADDRESS, Bridge
        self.address = os.environ.get("APP_SOCKET", DEFAULT_ADDRESS)
        self.bridge = Bridge(self.address)
        if not self.bridge.connect(timeout=5):
            raise SystemExit(f"Cannot reach arduino-router at {self.address}. Is it running? "
                             "systemctl status arduino-router   (and is the ketchup-dot App started?)")
        self.lock = threading.Lock()

    def call(self, name, *args):
        with self.lock:
            return self.bridge.call(name, *args, timeout=2)


class FakeBoard:
    """Stands in for sketch.ino: same three functions, same packed reply, same errors for a
    function the sketch doesn't provide (ValueError, like Bridge.call). Drive it with
    tap() / press() / release()."""

    def __init__(self):
        self.taps, self.pressed, self.rgb, self.writes = 0, False, (0, 0, 0), []

    def call(self, name, *args):
        if name == "set_led":
            self.rgb = tuple(max(0, min(255, int(v))) for v in args)
            self.writes.append(self.rgb)
            return None
        if name == "breathe_rgb":
            self.rgb = "breathe"
            self.writes.append(("breathe", int(args[0]), *(int(v) for v in args[1:4])))
            return None
        if name == "get_inputs":
            return self.taps * 2 + int(self.pressed)
        raise ValueError(f"method {name} not available")

    def tap(self):
        self.taps += 1

    def press(self):
        self.pressed = True

    def release(self):
        self.pressed = False


class BridgeLED:
    """What pi_pebble expects from gpiozero.RGBLED: .value is an (r, g, b) tuple of 0..1."""

    def __init__(self, board):
        self.board, self._value, self.can_breathe = board, None, True

    @property
    def value(self):
        return self._value or pb.OFF

    @value.setter
    def value(self, rgb):
        self._value = tuple(rgb)
        self.board.call("set_led", *(round(v * 255) for v in rgb))

    def breathe(self, period_s, rgb=(1, 1, 0)):
        """pi_pebble.refresh() asks first: True = the sketch breathes on its own."""
        if not self.can_breathe:
            return False
        try:
            self.board.call("breathe_rgb", round(period_s * 1000), *(round(v * 255) for v in rgb))
            self._value = None
            return True
        except Exception as e:  # sketch not re-flashed yet: pi_pebble steps set_led instead
            print("bridge: no breathe on the sketch (", type(e).__name__, e, ") - stepping set_led", flush=True)
            self.can_breathe = False
            return False

    def off(self):
        self.value = pb.OFF


class BridgeFSR:
    """What pi_pebble expects from gpiozero.Button: .is_pressed (the last polled state)."""
    is_pressed = False


def poll(board, fsr):
    """Turn the sketch's counters into callbacks: Hall tap -> pi_pebble's on_tap in a thread,
    FSR down -> on_down, FSR up -> on_up (short press / long press, above)."""
    last_taps, errors = None, 0
    while True:
        try:
            v = int(board.call("get_inputs"))
            errors = 0
        except Exception as e:  # router restart, sketch re-flash: keep going
            errors += 1
            if errors in (1, 40) or errors % 400 == 0:
                print("bridge error", type(e).__name__, e, flush=True)
            time.sleep(0.25)
            continue
        taps, pressed = v >> 1, bool(v & 1)
        if last_taps is not None and taps != last_taps:
            threading.Thread(target=pb.on_tap, daemon=True).start()
        last_taps = taps
        if pressed != fsr.is_pressed:
            fsr.is_pressed = pressed
            (on_down if pressed else on_up)()
        time.sleep(POLL_S)


def main(board=None):
    board = board or RouterBoard()
    if pb.TTS == "mms":
        threading.Thread(target=pb.load_mms, daemon=True).start()  # espeak-ng speaks until it's loaded
    pb.led, pb.fsr, pb.hall = BridgeLED(board), BridgeFSR(), None
    pb.wanted = wanted  # the long-press green flash (pi_pebble.refresh looks wanted up at call time)
    press["mic"] = has_mic()
    print(f"FSR: long press ({LONG_PRESS_S:g} s) = keep it; short press = "
          + ("mic on / off" if press["mic"] else "say the time again (no mic found)"), flush=True)
    threading.Thread(target=poll, args=(board, pb.fsr), daemon=True).start()
    print(f"Ketchup Dot {pb.DEVICE} -> {pb.SERVER} (UNO Q, Bridge)", flush=True)
    pb.refresh()  # dim white
    while True:
        try:
            pb.tick()
        except Exception as e:  # server down or Wi-Fi blip: keep going
            print("poll error", e, flush=True)
        time.sleep(1)


def mock_console():
    board = FakeBoard()
    threading.Thread(target=main, args=(board,), daemon=True).start()
    print("mock board: t = magnet tap, p = press FSR, r = release FSR, q = quit", flush=True)
    for line in sys.stdin:
        c = line.strip().lower()
        if c == "q":
            break
        {"t": board.tap, "p": board.press, "r": board.release}.get(c, lambda: print("t / p / r / q"))()


if __name__ == "__main__":
    try:
        mock_console() if "--mock" in sys.argv else main()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            pb.led and pb.led.off()
        except Exception:
            pass
