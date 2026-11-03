"""Run one physical Dot's real code with fake hardware, against a running tap server.

    .venv/bin/python server/tap_server.py --dry-run &
    python scripts/sim_dots.py                      # both Dots at once (Mayo = Pi, Frankie = UNO Q)
    python scripts/sim_dot.py --board pi --owner mayo --say "no, pick a time at night" --at <epoch>

--board pi:   pebble/pi_pebble.py with gpiozero mock pins (Hall 17, FSR 24, PWM LED).
--board unoq: pebble/unoq/unoq_dot.py with FakeBoard standing in for sketch.ino
              (--router: through the real arduino-router-bridge client and a fake router socket).
Stubs (no hardware): arecord "records" a 1 s silent WAV until stopped, espeak-ng/aplay only log,
and Muse Voice Transcribe is replaced by the next --say line, so the Dot sends no audio anywhere.
The flow it drives: tap at --at -> agents talk -> a time is ready -> a short FSR press hears it ->
hold the FSR and say the next line (then "keep" for every later time) -> ... -> booked.
Prints the LED states the Dot logged and checks they follow
white -> white breathing -> yellow -> ... -> green -> white.
"""
import argparse
import io
import os
import sys
import threading
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STUB = {
    "arecord": '#!/bin/sh\nfor a; do out="$a"; done\ncp %s/silence.wav "$out"\n'
               'echo "arecord $*" >> %s/calls.log\ntrap "exit 0" TERM INT\nwhile :; do sleep 0.05; done\n',
    "espeak-ng": '#!/bin/sh\necho "espeak $*" >> %s/calls.log\n',
    "aplay": '#!/bin/sh\ncat > /dev/null\necho "aplay $*" >> %s/calls.log\n',
}


def fake_router(board, path):
    """A stand-in for arduino-router + sketch.ino: MessagePack-RPC over a unix socket. Requests
    for set_led / get_inputs are answered by the FakeBoard; anything else gets the router's
    'method not available' error. Enough to drive the real arduino.router_bridge.Bridge client."""
    import msgpack
    import socket
    if os.path.exists(path):
        os.remove(path)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen()

    def serve(conn):
        unpacker = msgpack.Unpacker(raw=False)
        while data := conn.recv(4096):
            unpacker.feed(data)
            for msg in unpacker:
                if msg[0] != 0:  # notifications: nothing to answer
                    continue
                _, msgid, method, params = msg
                try:
                    if method.startswith("$/"):
                        err, res = None, True  # $/register etc.
                    else:
                        err, res = None, board.call(method, *params)
                except ValueError as e:
                    err, res = [0x02, str(e)], None
                conn.sendall(msgpack.packb([1, msgid, err, res]))

    def accept():
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=serve, args=(conn,), daemon=True).start()

    threading.Thread(target=accept, daemon=True).start()


class Tee(io.TextIOBase):
    """Keeps every 'LED ...' line the Dot prints, with a timestamp."""

    def __init__(self, out, t0):
        self.out, self.t0, self.buf, self.leds = out, t0, "", []

    def write(self, s):
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            if line.startswith("LED "):
                self.leds.append((round(time.monotonic() - self.t0, 1), line[4:]))
        return self.out.write(s)

    def flush(self):
        self.out.flush()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--board", choices=["pi", "unoq"], required=True)
    ap.add_argument("--owner", required=True)
    ap.add_argument("--url", default="http://localhost:8787")
    ap.add_argument("--say", action="append", default=[], help="answers in order; 'keep' after they run out")
    ap.add_argument("--wait", type=float, default=0, help="seconds to wait after hearing a time, before answering")
    ap.add_argument("--at", type=float, default=0, help="epoch time to tap the magnet (both Dots within 2 s)")
    ap.add_argument("--green-s", default="6", help="how long green holds (Dot default 60)")
    ap.add_argument("--router", action="store_true", help="unoq: go through arduino-router-bridge + a fake router")
    ap.add_argument("--old-sketch", action="store_true", help="unoq: sketch without breathe(), Linux steps set_led")
    args = ap.parse_args()

    stubs = Path(f"/tmp/ketchup_dot_stubs_{args.owner}")
    stubs.mkdir(exist_ok=True)
    (stubs / "calls.log").write_text("")
    with wave.open(str(stubs / "silence.wav"), "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000), w.writeframes(b"\0" * 32000)
    for name, body in STUB.items():
        (stubs / name).write_text(body.replace("%s", str(stubs)))
        (stubs / name).chmod(0o755)
    os.environ.update(PATH=f"{stubs}:{os.environ['PATH']}", KETCHUP_SERVER=args.url, PEBBLE_ID=args.owner,
                      PEBBLE_GREEN_S=args.green_s, PEBBLE_TTS="espeak")
    tee = sys.stdout = Tee(sys.stdout, time.monotonic())

    if args.board == "pi":
        os.environ.update(GPIOZERO_PIN_FACTORY="mock", GPIOZERO_MOCK_PIN_CLASS="mockpwmpin")
        sys.path.insert(0, str(ROOT / "pebble"))
        import pi_pebble as pb
        from gpiozero import Device
        threading.Thread(target=pb.main, daemon=True).start()
        time.sleep(1.0)
        pin = Device.pin_factory.pin
        tap = lambda: (pin(17).drive_low(), time.sleep(0.2), pin(17).drive_high())  # noqa: E731
        press, release = (lambda: pin(24).drive_high()), (lambda: pin(24).drive_low())
    else:
        sys.path.insert(0, str(ROOT / "pebble" / "unoq"))
        import unoq_dot
        pb = unoq_dot.pb
        board = target = unoq_dot.FakeBoard()
        if args.old_sketch:
            real = board.call
            board.call = lambda name, *a: (_ for _ in ()).throw(ValueError("method not available")) \
                if name == "breathe" else real(name, *a)
        if args.router:
            sock = f"/tmp/ketchup_fake_router_{args.owner}.sock"
            fake_router(board, sock)
            os.environ["APP_SOCKET"] = f"unix://{sock}"
            target = unoq_dot.RouterBoard()
        threading.Thread(target=unoq_dot.main, args=(target,), daemon=True).start()
        time.sleep(1.0)
        tap, press, release = board.tap, board.press, board.release

    heard = list(args.say)  # what the owner "says" on each hold, replaces the ASR call
    pb.transcribe = lambda wav: (heard.pop(0) if heard else "keep")

    time.sleep(max(0.0, args.at - time.time()))
    print(f"\n== {args.owner}: magnet tap", flush=True)
    tap()
    deadline = time.monotonic() + 30
    while not pb.now["sid"] and time.monotonic() < deadline:
        time.sleep(0.1)
    if not pb.now["sid"]:
        raise SystemExit("never paired")
    holds_ = 0
    deadline = time.monotonic() + 300
    while pb.now["sid"] and time.monotonic() < deadline:
        p = pb.now["pending"]
        if not p:
            time.sleep(0.2)
            continue
        if p != pb.now["heard"]:
            print(f"\n== {args.owner}: short FSR press to hear the time", flush=True)
            press(); time.sleep(0.15); release()
        while pb.now["heard"] != p or pb.speaking.locked():
            time.sleep(0.1)
        time.sleep(args.wait)
        if pb.now["pending"] != p or not pb.now["sid"]:
            continue  # withdrawn (the other person asked for a change) or done
        line = heard[0] if heard else "keep"
        print(f'\n== {args.owner}: holds the FSR and says "{line}"', flush=True)
        press(); time.sleep(0.2); release(); time.sleep(0.1)  # a flicker inside the grace window
        press(); time.sleep(1.0); release(); holds_ += 1
        while pb.now["pending"] == p and pb.now["sid"]:
            time.sleep(0.2)
    print(f"\n== {args.owner}: session over; waiting out green", flush=True)
    time.sleep(float(args.green_s) + 2.5)

    leds = [x for x in tee.leds if not x[1].startswith("green for")]
    seq = [c for i, (_, c) in enumerate(leds) if i == 0 or leds[i - 1][1] != c]
    sys.stdout = tee.out
    print(f"LED sequence ({args.owner}, {args.board}):", " -> ".join(seq))
    print("LED log with seconds:", ", ".join(f"{t}s {c}" for t, c in tee.leds if not c.startswith("green for")))
    if args.board == "unoq":
        kinds = [w[0] if isinstance(w[0], str) else w for w in board.writes]
        print("sketch got:", sum(k == "breathe" for k in kinds), "breathe calls,",
              sum(k != "breathe" for k in kinds), "set_led calls")
    log = (stubs / "calls.log").read_text().splitlines()
    print("stub calls:", sum(x.startswith("arecord") for x in log), "arecord,",
          sum(x.startswith("espeak") for x in log), "espeak")
    # this Dot's owner asked for a change: they held the FSR (yellow) before the agents talked again
    change = ["yellow", "white"] if args.say else []
    want = ["white", "white breathing", "white", *change, "white breathing", "white", "yellow", "white",
            "green", "white"]
    it = iter(seq)
    ok = all(w in it for w in want) and "red" not in seq and holds_ >= 1 and not pb.now["rec"]
    print("ALL GOOD" if ok else "SOMETHING FAILED", flush=True)
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
