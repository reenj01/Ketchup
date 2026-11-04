"""Run one physical Dot's real code with fake hardware, against a running tap server.

    .venv/bin/python server/tap_server.py --dry-run &
    python scripts/sim_dots.py                      # both Dots at once (Mayo = Pi, Frankie = UNO Q)
    python scripts/sim_dot.py --board pi --owner mayo --say "no, pick a time at night" --interrupt --at <epoch>

--board pi:   pebble/pi_pebble.py with gpiozero mock pins (Hall 17, FSR 24, PWM LED).
--board unoq: pebble/unoq/unoq_dot.py with FakeBoard standing in for sketch.ino
              (--router: through the real arduino-router-bridge client and a fake router socket).
Stubs (no hardware): arecord "records" a 1 s silent WAV until stopped, espeak-ng only logs, aplay
"plays" for 0.6 s, and Muse Voice Transcribe is replaced by the next --say line, so the Dot sends
no audio anywhere. The flow it drives: tap at --at -> agents decide (Mayo's Dot says the connected
line, its LED bright white, not blue) -> a time is ready -> the owner is away for --walk-away s and the Dot must say nothing -> a press
hears it (with --interrupt, a second press while it talks cuts it off and opens the mic) ->
press once (mic ON, with a 10 ms bounce that must not toggle twice), say the next line (then
"keep" for every later time), press again (mic OFF, sent) -> ... -> booked.
UNO Q (no mic, arecord must never run, --say is not used): the owner taps the magnet to hear a
time, then answers with one press: short (released at 0.08 s) = keep ("SHORT PRESS: keep", green
flash, "keep it"); with --long-press-change N the first N answers are long presses instead (held
PEBBLE_LONG_PRESS_S + 0.3 s: "LONG PRESS: find another time", yellow flash, "pick a different
time", then yellow breathing while the agents pick again).
Prints the LED states and press / MIC lines the Dot logged and checks they follow
dim white -> bright white -> yellow breathing -> orange breathing (a time waits) -> blue ->
orange breathing -> ... -> green -> dim white, with one MIC ON and one MIC OFF per spoken answer on
the Pi and one SHORT / LONG PRESS line per UNO Q answer. --expect-say TEXT: some spoken line must
contain TEXT (Mayo's Dot hearing "Frankie wants a different time").
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
    "aplay": '#!/bin/sh\ncat > /dev/null\necho "aplay $*" >> %s/calls.log\nexec sleep 0.6\n',
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
    """Keeps every 'LED ...' and 'MIC ...' line the Dot prints, with a timestamp."""

    def __init__(self, out, t0):
        self.out, self.t0, self.buf, self.leds, self.mics, self.says = out, t0, "", [], [], []
        self.first_press = None  # seconds in, when the owner first pressed the pad
        self.said_before = None  # how many SAY lines came before that press
        self.leds_before = None  # how many LED lines came before that press
        self.ignored = self.changes = 0  # UNO Q presses ignored; answers the server read as a change

    def write(self, s):
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            if line.startswith("LED "):
                self.leds.append((round(time.monotonic() - self.t0, 1), line[4:]))
            if line.startswith('SAY "'):
                self.says.append((round(time.monotonic() - self.t0, 1), line[5:-1]))
            if line.startswith("MIC ON") or line.startswith("MIC OFF"):
                self.mics.append(line.split(" -")[0].strip())
            if line in ("SHORT PRESS: keep", "LONG PRESS: find another time"):
                self.mics.append(line)
            if "PRESS: ignored" in line:
                self.ignored += 1
            if line.startswith("ANSWER ") and "'mapped_to': 'change'" in line:
                self.changes += 1
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
    ap.add_argument("--at", type=float, default=0, help="epoch time to tap the magnet (both Dots within 5 s)")
    ap.add_argument("--green-s", default="10", help="how long green holds (Dot default 10)")
    ap.add_argument("--router", action="store_true", help="unoq: go through arduino-router-bridge + a fake router")
    ap.add_argument("--old-sketch", action="store_true", help="unoq: sketch without breathe_rgb(), Linux steps set_led")
    ap.add_argument("--long-press-change", type=int, default=0,
                    help="unoq: answer the first N times with a long press (find another time)")
    ap.add_argument("--expect-say", default="", help="some spoken line must contain this")
    ap.add_argument("--interrupt", action="store_true", help="press while the Dot says the first time")
    ap.add_argument("--say-wav", default="", help="pi: the first answer is this recorded WAV, sent to speech to text "
                    "(Muse Voice Transcribe; its recorded transcript in a replay) instead of the first --say line")
    ap.add_argument("--walk-away", type=float, default=0,
                    help="after a time is ready, stay away this long first (the Dot must stay silent)")
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
                if name == "breathe_rgb" else real(name, *a)
        if args.router:
            sock = f"/tmp/ketchup_fake_router_{args.owner}.sock"
            fake_router(board, sock)
            os.environ["APP_SOCKET"] = f"unix://{sock}"
            target = unoq_dot.RouterBoard()
        threading.Thread(target=unoq_dot.main, args=(target,), daemon=True).start()
        time.sleep(1.0)
        tap, press, release = board.tap, board.press, board.release

    heard = list(args.say)  # what the owner "says" each time the mic is on, replaces the ASR call
    real_asr = pb.transcribe
    pb.transcribe = lambda wav: (heard.pop(0) if heard else "keep")
    if args.say_wav and args.board == "pi":  # the first answer is real audio: the stub mic "records" it
        sys.path.insert(0, str(ROOT / "brain"))
        import replay
        (stubs / "silence.wav").write_bytes(Path(args.say_wav).read_bytes())
        stub_asr = pb.transcribe

        def first_clip(wav):
            pb.transcribe = stub_asr
            with wave.open(str(stubs / "silence.wav"), "wb") as w:  # later answers: silence + the --say text
                w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000), w.writeframes(b"\0" * 32000)
            heard[:1] = []
            return replay.transcribe(wav, real_asr, pb.ASR_MODEL)
        pb.transcribe = first_clip

    time.sleep(max(0.0, args.at - time.time()))
    print(f"\n== {args.owner}: magnet tap", flush=True)
    tap()
    deadline = time.monotonic() + 30
    while not pb.now["sid"] and time.monotonic() < deadline:
        time.sleep(0.1)
    if not pb.now["sid"]:
        raise SystemExit("never paired")
    print(f"== {args.owner}: paired", flush=True)
    unoq = args.board == "unoq"
    if unoq:  # no mic on the UNO Q
        args.say, heard[:] = [], []
    holds_, longs, shorts, cut = 0, 0, 0, not (args.interrupt and not unoq)
    waited = set()  # UNO Q: the LED names seen while the other person's answer still waited
    if args.walk_away:
        print(f"== {args.owner}: walks away", flush=True)
        deadline = time.monotonic() + 120
        while not pb.now["pending"] and time.monotonic() < deadline:
            time.sleep(0.1)
        print(f"== {args.owner}: (away) a time is ready, LED {pb.NAMES[pb.wanted()]}; staying away "
              f"{args.walk_away:g} s more", flush=True)
        time.sleep(args.walk_away)
        print(f"== {args.owner}: comes back", flush=True)

    def mark():  # the owner is back and touches the Dot for the first time
        if tee.first_press is None:
            tee.first_press, tee.said_before = round(time.monotonic() - tee.t0, 1), len(tee.says)
            tee.leds_before = len(tee.leds)

    def click(bounce=False, hold=0.08):
        mark()
        press()
        if bounce:  # the FSR chatters 10 ms into the press: must not count as a second press
            time.sleep(0.01); release(); time.sleep(0.01); press()
        time.sleep(hold); release(); time.sleep(0.1)

    deadline = time.monotonic() + 300
    while pb.now["sid"] and time.monotonic() < deadline:
        p = pb.now["pending"]
        if not p:
            time.sleep(0.2)
            continue
        if unoq:  # magnet tap to hear it, then one press answers: short = keep, long = another time
            if p != pb.now["heard"]:
                print(f"\n== {args.owner}: taps the magnet to hear the time", flush=True)
                mark()
                tap()
            t_end = time.monotonic() + 20
            while (pb.now["heard"] != p or pb.speaking.locked()) and pb.now["pending"] == p \
                    and time.monotonic() < t_end:
                time.sleep(0.1)
            t_end = time.monotonic() + 120  # Frankie answers after Mayo: yellow until Mayo has
            while not unoq_dot.time_waiting() and pb.now["pending"] == p and pb.now["sid"] \
                    and time.monotonic() < t_end:
                if unoq_dot.other_first():
                    waited.add(pb.NAMES[pb.wanted()])
                time.sleep(0.1)
            time.sleep(args.wait)
            if pb.now["pending"] != p or not pb.now["sid"]:
                continue  # withdrawn (the other person asked for a change) or done
            if longs < args.long_press_change:
                print(f"\n== {args.owner}: holds the pad {unoq_dot.LONG_PRESS_S + 0.3:g} s "
                      "(long press = find another time)", flush=True)
                click(bounce=True, hold=unoq_dot.LONG_PRESS_S + 0.3)
                longs += 1
            else:
                print(f"\n== {args.owner}: short press (keep)", flush=True)
                click(bounce=True)
                shorts += 1
            while pb.now["pending"] == p and pb.now["sid"]:
                time.sleep(0.2)
            continue
        if p != pb.now["heard"]:
            print(f"\n== {args.owner}: press to hear the time", flush=True)
            click()
        if not cut:  # talk over the Dot: the press stops the voice and opens the mic
            while not pb.now["speaking"]:
                time.sleep(0.02)
            time.sleep(0.2)
            print(f"\n== {args.owner}: presses while the Dot is talking", flush=True)
            click(bounce=True)
            cut = True
        else:
            while pb.now["heard"] != p or pb.speaking.locked():
                time.sleep(0.1)
            time.sleep(args.wait)
            if pb.now["pending"] != p or not pb.now["sid"]:
                continue  # withdrawn (the other person asked for a change) or done
            print(f"\n== {args.owner}: presses once (mic ON)", flush=True)
            click(bounce=True)
        line = heard[0] if heard else "keep"
        print(f'== {args.owner}: says "{line}", then presses again (mic OFF, send)', flush=True)
        time.sleep(1.0)
        click()
        holds_ += 1
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
    print("MIC lines:", ", ".join(tee.mics))
    before = [x for _, x in tee.says[:tee.said_before]]
    want_before = [pb.CONNECTED_LINE] if pb.SAY_CONNECTED else []
    print(f"spoken before the first press ({tee.first_press} s):", before or "nothing")
    print("spoken in all:", "; ".join(f'{t}s "{x}"' for t, x in tee.says))
    early = [c for _, c in tee.leds[:tee.leds_before]]
    early = [c for i, c in enumerate(early) if i == 0 or early[i - 1] != c]
    print("LEDs before the first press:", " -> ".join(early))
    slow = [x for x in log if x.startswith("espeak") and "-s" in x.split()]
    if pb.SAY_CONNECTED:
        print("connected line through the stub espeak:", slow[0] if slow else "NOT SLOWED")
    # this Dot's owner asked for a change: the agents decided again right after its mic went off
    O = "orange breathing"  # a time waits for this owner's answer
    if unoq:  # long press: yellow flash, "Okay, let me look" (blue), agents again; short press: green flash
        answers = ["yellow flash (find another time)", "yellow breathing", O] * longs + ["green flash (keep)"]
    else:
        answers = [*(["yellow (mic on)"] if holds_ else []), *(["yellow breathing"] if args.say else [])]
    if unoq:  # yellow until Mayo has answered (a tap to hear it is blue), then orange, then the press
        want = ["dim white", "bright white", "yellow breathing", "blue (talking)", O, *answers, "green", "dim white"]
        print("UNO Q LED while the other person's answer waited:", ", ".join(sorted(waited)) or "never waited")
    else:
        want = ["dim white", "bright white", "yellow breathing", O, "blue (talking)", *answers, "green", "dim white"]
    early_ok = early in ([*want[:3]], [*want[:3], O]) if unoq else early == [*want[:3], O]
    waited_ok = not unoq or waited <= {"yellow breathing", "blue (talking)"}
    it = iter(seq)
    # one press = one toggle, bounce and all, on the Pi; the UNO Q: one press line per answer, no mic
    toggles = (tee.mics == ["LONG PRESS: find another time"] * longs + ["SHORT PRESS: keep"] * shorts
               and holds_ == 0 and tee.ignored == 0 and tee.changes == longs and shorts >= 1) if unoq else \
        tee.mics == ["MIC ON", "MIC OFF"] * holds_ and holds_ >= 1
    if unoq:
        print(f"UNO Q presses: {longs} long (find another time, {tee.changes} read as change by the server), "
              f"{shorts} short (keep), {tee.ignored} ignored; lines:", ", ".join(tee.mics))
    arecords = sum(x.startswith("arecord") for x in log)
    heard_it = not args.expect_say or any(args.expect_say in x for _, x in tee.says)
    if args.expect_say:
        print(f'heard "{args.expect_say}":', heard_it)
    ok = all(w in it for w in want) and "red" not in seq and not pb.now["rec"] and toggles \
        and not (unoq and arecords) and heard_it \
        and before == want_before and early_ok and waited_ok \
        and bool(slow) == pb.SAY_CONNECTED and all(x.endswith("--stdout " + pb.CONNECTED_LINE) for x in slow) \
        and pb.CONNECTED_LINE == "Dots connected. We will get back to you once we are done."
    print("ALL GOOD" if ok else "SOMETHING FAILED", flush=True)
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
