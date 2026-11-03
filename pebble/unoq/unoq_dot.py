"""Ketchup Dot on an Arduino UNO Q: same Dot as pebble/pi_pebble.py, different pins.

The UNO Q has two halves. The microcontroller runs ketchup-dot/sketch/sketch.ino (LED, Hall
sensor, FSR). This script runs on the board's Linux side and does everything else exactly the
way the Pi does it, by importing pi_pebble.py and reusing its code: tap -> POST /tap, poll
GET /sessions/<id>, LED colours (dim white, bright white, yellow breathing, orange breathing,
blue, green), POST /sessions/<id>/answer, pipelined MMS-TTS voice with espeak-ng fallback. The pins
are different: instead of gpiozero, the LED and the two inputs are reached over the Arduino Bridge
(arduino-router, MessagePack-RPC):

    Bridge.call("set_led", r, g, b)   0..255 each (solid colour)
    Bridge.call("breathe_rgb", ms, r, g, b)  breathing, run on the MCU: yellow while the agents
                                      decide, orange while a time waits for Frankie's answer (an
                                      older sketch without it: Linux steps set_led instead)
    Bridge.call("get_inputs")         -> taps * 2 + pressed   (polled every 25 ms)

LED wiring: PEBBLE_LED_ORDER says which colour each of the sketch's pins D3, D5, D6 lights, and
the (r, g, b) Linux wants is remapped before it goes over the Bridge (set_led and breathe_rgb).
Default "grb": Frankie's LED has its red and green legs on each other's pins (green showed red,
orange showed green, yellow / white / blue looked right). Set "rgb" if the LED is rewired
R=D3, G=D5, B=D6. No sketch change either way.

Turn order: Frankie answers after Mayo (PEBBLE_ANSWER_SECOND=1, the default). While the time also
waits on the other person's answer, this Dot keeps breathing yellow and presses are ignored
("waiting for the other person to answer first"); once they keep it, it breathes orange and the
press counts. PEBBLE_ANSWER_SECOND=0: orange as soon as the time is ready, like the Pi.

The pad (FSR) is the other difference. The UNO Q has no mic: no arecord, no speech to text. The pad
answers the time waiting for Frankie (the LED breathes orange), with one press:
  short press = keep it: "SHORT PRESS: keep", a short green flash, "keep it" to /answer.
  long press  = held PEBBLE_LONG_PRESS_S (default 1.0 s): find another time. At the threshold
                the LED flashes yellow, the terminal says "LONG PRESS: find another time", and
                "pick a different time" goes to /answer: a change with no reason, so Mayo's Dot only
                hears that Frankie wants a different time. Both Dots breathe yellow again while the
                agents pick a new one.
With no time waiting (agents still talking, already answered, no session) a press does nothing but
log "SHORT PRESS: ignored ..." / "LONG PRESS: ignored ...". A press while the Dot talks stops the
voice. Short presses act on release, so a press can still turn into a long one. A magnet tap during
a session says the time again.

Same environment variables as the Pi: KETCHUP_SERVER, PEBBLE_ID (defaults to frankie here),
PEBBLE_AUDIO, PEBBLE_PHRASE, PEBBLE_GREEN_S, PEBBLE_TTS, PEBBLE_ANNOUNCE (default tap),
PEBBLE_SAY_CONNECTED (default 0 here: Frankie's Dot is silent on pairing), PEBBLE_ORANGE (default
255,90,0). META_API_KEY isn't used here (no speech to text). Extra: PEBBLE_LED_ORDER (grb),
PEBBLE_ANSWER_SECOND (1), PEBBLE_LONG_PRESS_S (1.0), APP_SOCKET
(router address, default unix:///var/run/arduino-router.sock), DOT_POLL_MS (default 25).

    python3 unoq_dot.py          # on the UNO Q, with the ketchup-dot App running (sketch flashed)
    python3 unoq_dot.py --mock   # anywhere: fake board, type t = tap, p = press, r = release

Bridge docs: https://docs.arduino.cc/software/app-lab/bridge/bridge-api/
Python package: https://pypi.org/project/arduino-router-bridge/
"""
import os
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault("PEBBLE_ID", "frankie")  # this board is Frankie's Dot
sys.path.insert(0, os.environ.get("PEBBLE_DIR", str(Path(__file__).resolve().parent.parent)))
import pi_pebble as pb  # noqa: E402  (reads the env vars above at import, like on the Pi)

POLL_S = float(os.environ.get("DOT_POLL_MS", 25)) / 1000
LONG_PRESS_S = float(os.environ.get("PEBBLE_LONG_PRESS_S", 1.0))  # hold this long = find another time
FLASH_S = 0.4  # the flash that says a press counted
KEEP_TEXT = "keep it"  # short press (the server reads both as keep / change by keyword, no model)
CHANGE_TEXT = "pick a different time"  # long press: a change with no reason; the other Dot hears only that
FLASH_KEEP, FLASH_CHANGE = "green flash (keep)", "yellow flash (find another time)"
pb.SOLID.update({FLASH_KEEP: pb.GREEN, FLASH_CHANGE: pb.YELLOW})
# which colour each of the sketch's pins D3, D5, D6 lights (Frankie's LED: red and green swapped)
LED_ORDER = os.environ.get("PEBBLE_LED_ORDER", "grb").strip().lower()
if sorted(LED_ORDER) != ["b", "g", "r"]:
    raise SystemExit(f"PEBBLE_LED_ORDER={LED_ORDER!r}: use the letters r, g, b once each (e.g. grb)")
ANSWER_SECOND = os.environ.get("PEBBLE_ANSWER_SECOND", "1").strip().lower() not in ("0", "no", "off", "false")
press = {"down": None, "long": False, "edge": -1.0, "gen": 0, "flash": None, "flash_until": 0.0}

_wanted = pb.wanted


def pins(rgb):
    """(r, g, b) 0..1 -> the sketch's (D3, D5, D6) 0..255, in LED_ORDER."""
    v = dict(zip("rgb", rgb))
    return tuple(round(v[c] * 255) for c in LED_ORDER)


def other_first():
    """The time also waits on the other person's answer, and this Dot answers after them."""
    return bool(ANSWER_SECOND and pb.now.get("others_asked"))


def wanted():
    """pi_pebble's colour, except for the short flash after a press counts, and yellow (not orange)
    breathing while the other person still has to answer first."""
    if time.monotonic() < press["flash_until"]:
        return press["flash"]
    w = _wanted()
    return pb.BREATHE if w == pb.ORANGE_BREATHE and other_first() else w


def flash(name):
    press.update(flash=name, flash_until=time.monotonic() + FLASH_S)
    pb.refresh()
    t = threading.Timer(FLASH_S + 0.02, pb.refresh)
    t.daemon = True
    t.start()


def time_waiting():
    """A time the agents picked is waiting for Frankie's answer (the LED breathes orange)."""
    return bool(pb.now["sid"] and pb.now["pending"] and pb.now["mode"] != "talking" and not other_first())


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


def ignored(kind):
    why = ("no session" if not pb.now["sid"] else "the agents are still deciding" if pb.now["mode"] == "talking"
           else "waiting for the other person to answer first" if pb.now["pending"] and other_first()
           else "already answered, waiting on the other person")
    print(f"{kind}: ignored (no time waiting: {why})", flush=True)


def short_press():
    """Keep the time waiting for Frankie. Ignored (logged) when no time is waiting."""
    if not time_waiting():
        return ignored("SHORT PRESS")
    print("SHORT PRESS: keep", flush=True)
    flash(FLASH_KEEP)
    answer(KEEP_TEXT, "keep")


def long_press(gen):
    """Still held at LONG_PRESS_S: ask for another time. Ignored (logged) when no time is waiting."""
    if gen != press["gen"] or press["down"] is None or press["long"] or not pb.fsr.is_pressed:
        return
    press["long"] = True
    if not time_waiting():
        return ignored("LONG PRESS")
    print("LONG PRESS: find another time", flush=True)
    flash(FLASH_CHANGE)
    answer(CHANGE_TEXT, "change")


def answer(text, want):
    """POST the press's answer; on success the reply ("say") is spoken. A change sends both Dots'
    agents back to deciding (yellow breathing once the reply is said)."""
    pb.hush()
    sid = pb.now["sid"]
    try:
        r = pb.call(f"/sessions/{sid}/answer", {"device_id": pb.DEVICE, "answer": text,
                                                "via": "short press" if want == "keep" else "long press"})
    except Exception as e:
        r = {"ok": False, "error": str(e)}
    print("ANSWER", {k: r.get(k) for k in ("ok", "mapped_to", "error", "state")}, flush=True)
    if not r.get("ok") or r.get("mapped_to") != want:
        print(f"PRESS: not sent as {want} -", r.get("error") or r.get("mapped_to"), flush=True)
        return pb.refresh()
    pb.now.update(answered=pb.now["pending"], pending=None, engaged=True)
    if want == "change":
        pb.now["mode"] = "talking"
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
        self.board.call("set_led", *pins(rgb))

    def breathe(self, period_s, rgb=(1, 1, 0)):
        """pi_pebble.refresh() asks first: True = the sketch breathes on its own."""
        if not self.can_breathe:
            return False
        try:
            self.board.call("breathe_rgb", round(period_s * 1000), *pins(rgb))
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
    FSR down -> on_down, FSR up -> on_up (short press = keep, long press = another time, above)."""
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
    pb.wanted = wanted  # the press flashes (pi_pebble.refresh looks wanted up at call time)
    print(f"FSR: short press = keep the time; long press ({LONG_PRESS_S:g} s) = find another time; "
          "no mic", flush=True)
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
