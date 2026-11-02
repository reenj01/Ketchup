"""Run pebble/pi_pebble.py on a laptop with mock GPIO pins and stub audio tools.

    pip install gpiozero    # any venv; this script only talks HTTP to the server
    python server/tap_server.py --dry-run &
    python scripts/sim_pebble.py --wavs DIR   # DIR has morning.wav, change.wav, keep.wav

Mayo is the simulated Pi pebble: the Hall pin goes low (tap), and for each
question the FSR pin is held while a stub `arecord` "records" the next WAV.
Real Muse Voice Transcribe (META_API_KEY) turns it into text. Frankie is a
plain HTTP pebble that taps at the same time and answers "night", then "keep".
espeak/aplay are stubs that log what would be spoken (PEBBLE_TTS=espeak, so
MMS-TTS isn't loaded). Checks that the LED goes
white -> yellow -> red -> green -> white and every press is heard and answered.
"""
import argparse
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STUBS = Path("/tmp/ketchup_pebble_stubs")
STUB = {
    "arecord": '#!/bin/sh\nfor a; do out="$a"; done\ncp "$(cat %s/next)" "$out"\n'
               'echo "arecord $*" >> %s/calls.log\ntrap "exit 0" TERM INT\nwhile :; do sleep 0.05; done\n',
    "espeak-ng": '#!/bin/sh\necho "espeak $*" >> %s/calls.log\n',
    "aplay": '#!/bin/sh\ncat > /dev/null\necho "aplay $*" >> %s/calls.log\n',
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8787")
    ap.add_argument("--wavs", required=True, help="folder with morning.wav, change.wav, keep.wav")
    ap.add_argument("--green-s", default="5", help="how long green holds (pebble default 60)")
    args = ap.parse_args()
    STUBS.mkdir(exist_ok=True)
    (STUBS / "calls.log").write_text("")
    for name, body in STUB.items():
        f = STUBS / name
        f.write_text(body.replace("%s", str(STUBS)))
        f.chmod(0o755)
    os.environ.update(PATH=f"{STUBS}:{os.environ['PATH']}", GPIOZERO_PIN_FACTORY="mock",
                      GPIOZERO_MOCK_PIN_CLASS="mockpwmpin", KETCHUP_SERVER=args.url,
                      PEBBLE_ID="mayo", PEBBLE_GREEN_S=args.green_s)
    os.environ.setdefault("PEBBLE_TTS", "espeak")  # stub voice; PEBBLE_TTS=mms to load Meta MMS-TTS too
    sys.path.insert(0, str(ROOT / "pebble"))
    import pi_pebble as pb
    from gpiozero import Device

    colors = []
    show = pb.show
    pb.show = lambda c: (colors.append(c) if not colors or colors[-1] != c else None, show(c))
    threading.Thread(target=pb.main, daemon=True).start()
    time.sleep(1.5)
    pin = Device.pin_factory.pin

    def call(path, body=None):
        return pb.call(path, body)

    print("\n== tap: Mayo's Hall pin low + Frankie /tap", flush=True)
    frankie = {}
    t = threading.Thread(target=lambda: frankie.update(call("/tap", {"device_id": "pebble-frankie"})))
    pin(17).drive_low(); t.start(); time.sleep(0.2); pin(17).drive_high(); t.join()
    while not pb.now["sid"]:
        time.sleep(0.2)
    sid = pb.now["sid"]
    print("Frankie tap:", frankie, flush=True)

    mayo_lines = {"time_of_day": ["morning"], "confirm": ["change", "keep"]}
    frankie_lines = {"time_of_day": "evening please", "confirm": "keep"}
    done_q, presses = set(), 0
    while True:
        s = call(f"/sessions/{sid}")
        if s["state"] in pb.DONE + ("failed",):
            break
        f = s["questions"].get("pebble-frankie")
        if f and ("f", s["change_round"], f["say"]) not in done_q:
            done_q.add(("f", s["change_round"], f["say"]))
            r = call(f"/sessions/{sid}/answer", {"device_id": "pebble-frankie", "answer": frankie_lines[f["kind"]]})
            print(f'Frankie says "{frankie_lines[f["kind"]]}" -> {r.get("mapped_to") or r.get("error")}', flush=True)
        m = s["questions"].get("pebble-mayo")
        key = ("m", s["change_round"], m and m["say"])
        if m and key not in done_q and pb.now["heard"] == key[1:] and mayo_lines[m["kind"]]:
            done_q.add(key)
            wav = mayo_lines[m["kind"]].pop(0)
            (STUBS / "next").write_text(str(Path(args.wavs) / f"{wav}.wav"))
            print(f"\n== Mayo holds the FSR ({wav}.wav)", flush=True)
            pin(24).drive_high(); time.sleep(1.0); pin(24).drive_low(); presses += 1
        time.sleep(0.5)
    print("\nfinal state:", s["state"], "| slot:", s.get("agreed_slot"), flush=True)
    time.sleep(float(args.green_s) + 2.5)
    names = {pb.WHITE: "white", pb.YELLOW: "yellow", pb.RED: "red", pb.GREEN: "green"}
    seq = [names[c] for c in colors]
    print("LED sequence:", " -> ".join(seq))
    log = (STUBS / "calls.log").read_text().splitlines()
    print("stub calls:", sum(l.startswith("arecord") for l in log), "arecord,",
          sum(l.startswith("espeak") for l in log), "espeak,", sum(l.startswith("aplay") for l in log), "aplay")
    left = [p for p in Path("/tmp").glob("tmp*.wav")]
    want = ["white", "yellow", "red", "green", "white"]
    it = iter(seq)
    ok = all(w in it for w in want) and s["state"] in pb.DONE and presses == 3
    print("temp WAVs left in /tmp:", len(left))
    print("ALL GOOD" if ok else "SOMETHING FAILED")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
