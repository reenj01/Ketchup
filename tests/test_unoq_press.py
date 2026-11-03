"""The UNO Q's pad (unoq_dot.py) and what every Dot does when it hears nothing.

UNO Q: a short press never approves. With a mic it does what a press does on the Pi (hear a new
time, else mic on / off); with no mic it says the proposed time again. A long press (held
PEBBLE_LONG_PRESS_S, 1.0 s by default) keeps the proposed time: green flash at the threshold,
"LONG PRESS: keep" in the terminal, "keep it" sent to /answer, and a recording in progress is
dropped. Bounce inside a press is ignored. The Pi's pad is unchanged (still a toggle).

Pi and UNO Q (pi_pebble.reply): no words, a clip too short, speech to text failing, or the server
calling the answer unclear -> nothing said, nothing (more) sent, one "MIC heard nothing, waiting"
line, and the LED back to what it showed before the mic went on.
No network, hardware or models: the server, the LED, the mic and the voice are stubbed."""
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STUBS = Path(tempfile.mkdtemp(prefix="unoq_press_"))
for name, body in {
    "arecord": '#!/bin/sh\nfor a; do out="$a"; done\nhead -c 32044 /dev/zero > "$out"\n'
               'trap "exit 0" TERM INT\nwhile :; do sleep 0.02; done\n',
    "aplay": "#!/bin/sh\ncat > /dev/null\n",
}.items():
    (STUBS / name).write_text(body)
    (STUBS / name).chmod(0o755)
os.environ.update(PEBBLE_TTS="espeak", PEBBLE_LONG_PRESS_S="0.3", PEBBLE_GREEN_S="0.3",
                  PATH=f"{STUBS}:{os.environ['PATH']}")
os.environ.pop("PEBBLE_ID", None)
sys.path.insert(0, str(ROOT / "pebble"))
sys.path.insert(0, str(ROOT / "pebble" / "unoq"))
import unoq_dot as uq  # noqa: E402  (first: it sets PEBBLE_ID=frankie before pi_pebble reads it)
pb = uq.pb

assert pb.OWNER == "frankie" and pb.DEVICE == "pebble-frankie", "IDs stay frankie"
assert uq.LONG_PRESS_S == 0.3
REAL_PRINT = print
TIME = ("q1", "How about Friday at 6 PM? Want to change it?")
out, said, sent = [], [], []


def wait_for(cond, s=3.0):
    t = time.monotonic() + s
    while not cond():
        assert time.monotonic() < t, "timed out"
        time.sleep(0.01)


def setup(mic, pending=TIME, heard=TIME, answer=None):
    out.clear(), said.clear(), sent.clear()
    pb.now.update(sid="s1", heard=heard, answered=None, pending=pending, engaged=False, green_until=0, rec=None,
                  led=None, gen=0, round=0, talk_until=0, mode="ready", speaking=False, hush=None, player=[],
                  edge=-1.0, busy=False, booking=False, greeting=False)
    uq.press.update(down=None, long=False, edge=-1.0, mic=mic, flash_until=0.0)
    board = uq.FakeBoard()
    pb.led, pb.fsr = uq.BridgeLED(board), uq.BridgeFSR()
    pb.wanted = uq.wanted
    pb.print = lambda *a, **k: out.append(" ".join(map(str, a)))
    pb.say = lambda text: said.append(text)
    pb.transcribe = lambda wav: "keep it"
    pb.call = lambda path, body=None: (sent.append((path, body)), answer or {
        "ok": True, "mapped_to": "keep", "say": "Great, Friday at 6 PM it is.", "state": "pencilled_in"})[1]
    uq.print = pb.print
    pb.refresh()
    return board


def down():
    pb.fsr.is_pressed = True
    uq.on_down()


def up():
    pb.fsr.is_pressed = False
    uq.on_up()


def hold(s, bounce=False):
    down()
    if bounce:  # chatter 10 ms in: must not end the press or start a new one
        time.sleep(0.01); up(); time.sleep(0.01); down()
    time.sleep(s)
    up()
    time.sleep(0.08)


def answers():
    return [b["answer"] for p, b in sent if p.endswith("/answer")]


try:
    # ---- short press, no mic: says the time again, never approves -----------------------------
    board = setup(mic=False)
    hold(0.1, bounce=True)
    wait_for(lambda: said)
    assert said == [TIME[1]] and answers() == [] and pb.now["pending"] == TIME, (said, sent)
    assert "SHORT PRESS: the time again" in out and not any("LONG PRESS" in x for x in out), out
    hold(0.1)
    wait_for(lambda: len(said) == 2)
    assert answers() == [] and pb.now["pending"] == TIME and pb.now["answered"] is None
    print("OK UNO Q short press (no mic): the time again, twice; nothing approved")

    # ---- short press, mic: the Pi's toggle; still no keep from the press itself ---------------
    setup(mic=True)
    hold(0.1, bounce=True)
    assert pb.now["rec"] and "SHORT PRESS: mic" in out and answers() == [], out
    time.sleep(0.35)
    pb.transcribe = lambda wav: ""  # the owner said nothing
    hold(0.1)
    wait_for(lambda: any(x.startswith("MIC heard nothing") for x in out))
    time.sleep(0.05)
    assert not pb.now["rec"] and answers() == [] and said == [], (sent, said)
    assert pb.now["led"] == pb.WHITE and pb.now["pending"] == TIME, pb.NAMES.get(pb.now["led"])
    print("OK UNO Q short press (mic): mic on / off like the Pi; empty transcript -> silent, nothing sent, "
          "LED back to bright white")

    # ---- long press: green flash at the threshold, LONG PRESS: keep, "keep it" sent -----------
    board = setup(mic=True)
    leds = []
    pb.print = lambda *a, **k: (out.append(" ".join(map(str, a))),
                                 leds.append(out[-1][4:]) if out[-1].startswith("LED ") else None)
    uq.print = pb.print
    pb.refresh()
    t0 = time.monotonic()
    down()
    time.sleep(0.2)
    assert answers() == [] and not any("LONG PRESS" in x for x in out), "not yet at 0.2 s"
    wait_for(lambda: "LONG PRESS: keep" in out)
    t_long = time.monotonic() - t0
    assert 0.28 <= t_long < 0.45, t_long
    wait_for(lambda: answers())
    assert answers() == ["keep it"] and sent[0][1]["device_id"] == pb.DEVICE, sent
    assert "green" in leds, leds
    time.sleep(0.6)  # still held after the keep: the release must do nothing
    up()
    time.sleep(0.1)
    wait_for(lambda: said)
    assert said == ["Great, Friday at 6 PM it is."] and answers() == ["keep it"], (said, sent)
    assert pb.now["pending"] is None and pb.now["answered"] == TIME and not pb.now["rec"]
    assert not any(x.startswith("SHORT PRESS") for x in out), out
    assert leds[-1] != "green", leds  # the flash ended
    assert (0, 255, 0) in board.writes, board.writes
    print(f"OK UNO Q long press: LONG PRESS: keep at {t_long:.2f} s, LEDs", " -> ".join(leds),
          "; 'keep it' sent once; release ignored")

    # ---- long press while the mic is on: the audio is dropped, only the keep is sent ----------
    setup(mic=True)
    hold(0.1)
    assert pb.now["rec"]
    wav = pb.now["wav"]
    time.sleep(0.35)
    down()
    wait_for(lambda: answers())
    up()
    assert answers() == ["keep it"] and not pb.now["rec"] and not os.path.exists(wav), (sent, wav)
    assert "MIC OFF - dropped (long press)" in out and not any(x.startswith("HEARD") for x in out), out
    print("OK UNO Q long press with the mic on: recording dropped, not transcribed, keep sent")

    # ---- long press with no time / the server says no: silent ---------------------------------
    setup(mic=False, answer={"ok": False, "error": "no open question"})
    down(); wait_for(lambda: answers()); up()
    time.sleep(0.05)
    assert said == [] and pb.now["pending"] == TIME and any("not kept" in x for x in out), out
    setup(mic=False, pending=None, heard=None)
    pb.now["sid"] = None
    down(); wait_for(lambda: any("nothing to keep" in x for x in out)); up()
    assert sent == [] and said == []
    print("OK UNO Q long press with nothing to keep: silent")

    # ---- PEBBLE_MIC=0 forces no mic; auto needs arecord -l to list the card -------------------
    uq.MIC = "0"
    assert uq.has_mic() is False
    uq.MIC = "1"
    assert uq.has_mic() is True
    (STUBS / "arecord").write_text("#!/bin/sh\necho 'card 1: Device [USB Audio Device], device 0: USB Audio [USB Audio]'\n")
    uq.MIC = "auto"
    assert uq.has_mic() is True
    (STUBS / "arecord").write_text("#!/bin/sh\necho 'arecord: no soundcards found...' >&2\n")
    assert uq.has_mic() is False
    print("OK PEBBLE_MIC: 1 / 0 / auto (arecord -l lists the card)")

    # ---- the poll loop: sketch counters drive on_down / on_up ---------------------------------
    board = setup(mic=False)
    import threading
    threading.Thread(target=uq.poll, args=(board, pb.fsr), daemon=True).start()
    board.press(); time.sleep(0.1); board.release()
    wait_for(lambda: said)
    assert answers() == [] and said == [TIME[1]]
    time.sleep(0.1)  # a press within 50 ms of the last release is bounce, like on the Pi
    board.press(); wait_for(lambda: answers()); board.release()
    assert answers() == ["keep it"]
    print("OK UNO Q poll loop: short press -> the time again; long press -> keep")

    # ---- hearing nothing: silent on every Dot (reply() is shared by the Pi and the UNO Q) ------
    pb.wanted = uq._wanted
    for case, wav, asr, server in [
        ("clip too short", b"\0" * 8000, "keep it", None),
        ("no words", b"\0" * 32000, "", None),
        ("only punctuation", b"\0" * 32000, " . ", None),
        ("speech to text failed", b"\0" * 32000, RuntimeError("ASR 500"), None),
        ("server: unclear", b"\0" * 32000, "mmm banana",
         {"ok": False, "error": "unclear answer, question stays open", "say": "Sorry, I missed that. Want to change it?"}),
    ]:
        setup(mic=True, answer=server)
        pb.wanted = uq._wanted
        pb.led = type("L", (), {"value": pb.OFF})()

        def asr_(w, a=asr):
            if isinstance(a, Exception):
                raise a
            return a
        pb.transcribe = asr_
        before = pb.wanted()
        pb.reply(wav)
        lines = [x for x in out if x.startswith("MIC heard nothing")]
        assert said == [] and len(lines) == 1 and lines[0].startswith("MIC heard nothing, waiting"), (case, said, out)
        assert answers() == ([] if server is None else ["mmm banana"]), (case, sent)
        assert pb.now["led"] == before == pb.WHITE and pb.now["pending"] == TIME, case
    print("OK hearing nothing (short clip, no words, ASR error, server unclear): silent, one MIC line, "
          "nothing sent for empty or short, LED back to bright white")
    src = (ROOT / "pebble" / "pi_pebble.py").read_text()
    assert "didn't catch" not in src and "couldn't hear" not in src, "no spoken retry prompts left"
finally:
    pb.print = uq.print = REAL_PRINT
