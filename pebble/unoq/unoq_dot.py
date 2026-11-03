"""Ketchup Dot on an Arduino UNO Q: same Dot as pebble/pi_pebble.py, different pins.

The UNO Q has two halves. The microcontroller runs ketchup-dot/sketch/sketch.ino (LED, Hall
sensor, FSR). This script runs on the board's Linux side and does everything else exactly the
way the Pi does it, by importing pi_pebble.py and reusing its code: tap -> POST /tap, poll
GET /sessions/<id>, LED colours (dim white, bright white, yellow breathing, blue, solid
yellow, green), press-to-toggle mic with arecord, Muse Voice Transcribe, POST
/sessions/<id>/answer, pipelined MMS-TTS voice with espeak-ng fallback. Only the pins are different: instead of gpiozero, the LED and the
two inputs are reached over the Arduino Bridge (arduino-router, MessagePack-RPC):

    Bridge.call("set_led", r, g, b)   0..255 each (solid colour)
    Bridge.call("breathe_rgb", ms, r, g, b)  yellow breathing while the agents decide, run on
                                      the MCU (an older sketch without it: Linux steps set_led)
    Bridge.call("get_inputs")         -> taps * 2 + pressed   (polled every 25 ms)

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
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault("PEBBLE_ID", "frankie")  # this board is Frankie's Dot
sys.path.insert(0, os.environ.get("PEBBLE_DIR", str(Path(__file__).resolve().parent.parent)))
import pi_pebble as pb  # noqa: E402  (reads the env vars above at import, like on the Pi)

POLL_S = float(os.environ.get("DOT_POLL_MS", 25)) / 1000


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
    """Turn the sketch's counters into the same callbacks gpiozero fires on the Pi: Hall tap ->
    on_tap in a thread, FSR down -> on_press (toggles the mic, 50 ms debounce in pi_pebble),
    FSR up -> on_release."""
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
            (pb.on_press if pressed else pb.on_release)()
        time.sleep(POLL_S)


def main(board=None):
    board = board or RouterBoard()
    if pb.TTS == "mms":
        threading.Thread(target=pb.load_mms, daemon=True).start()  # espeak-ng speaks until it's loaded
    pb.led, pb.fsr, pb.hall = BridgeLED(board), BridgeFSR(), None
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
