"""The UNO Q's pad (unoq_dot.py) and what every Dot does when it hears nothing.

UNO Q (no mic: arecord is never run): while a time waits for Frankie (LED orange breathing) a
short press keeps it ("SHORT PRESS: keep", short green flash, "keep it" to /answer) and a long
press (held PEBBLE_LONG_PRESS_S, 1.0 s by default) asks for another time: at the threshold
"LONG PRESS: find another time", a short yellow flash, the neutral "pick a different time" to
/answer, then yellow breathing while the agents pick again. With no time waiting a press is ignored
and logged. Bounce inside a press is ignored. The Pi's pad is unchanged (still a mic toggle).

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
    "arecord": '#!/bin/sh\necho "$@" >> "$(dirname "$0")/arecord.calls"\n',  # must never run
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
out, said, sent, leds = [], [], [], []


def wait_for(cond, s=3.0):
    t = time.monotonic() + s
    while not cond():
        assert time.monotonic() < t, "timed out"
        time.sleep(0.01)


def setup(pending=TIME, heard=TIME, answer=None, mode="ready"):
    out.clear(), said.clear(), sent.clear()
    pb.now.update(sid="s1", heard=heard, answered=None, pending=pending, engaged=False, green_until=0, rec=None,
                  led=None, gen=0, round=0, talk_until=0, mode=mode, speaking=False, hush=None, player=[],
                  edge=-1.0, busy=False, booking=False, greeting=False)
    uq.press.update(down=None, long=False, edge=-1.0, flash=None, flash_until=0.0)
    board = uq.FakeBoard()
    pb.led, pb.fsr = uq.BridgeLED(board), uq.BridgeFSR()
    pb.wanted = uq.wanted
    leds.clear()
    pb.print = lambda *a, **k: (out.append(" ".join(map(str, a))),
                                 leds.append(out[-1][4:]) if out[-1].startswith("LED ") else None)
    pb.say = lambda text: said.append(text)
    pb.transcribe = lambda wav: "keep it"
    pb.call = lambda path, body=None: (sent.append((path, body)), answer or (
        {"ok": True, "mapped_to": "change", "say": "Okay, let me look for a new time.", "state": "proposing"}
        if body and body.get("answer") == uq.CHANGE_TEXT else
        {"ok": True, "mapped_to": "keep", "say": "Great, Friday at 6 PM it is.", "state": "pencilled_in"}))[1]
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
    assert not hasattr(uq, "has_mic") and not hasattr(uq, "MIC") and not hasattr(uq, "drop_recording")
    src = (ROOT / "pebble" / "unoq" / "unoq_dot.py").read_text()
    assert "arecord" not in src.split('"""', 2)[2] and "PEBBLE_MIC" not in src and "toggle" not in src
    assert "PEBBLE_MIC" not in (ROOT / "deploy" / "dots" / "ketchup-dot-unoq.env.example").read_text()

    # ---- a time waiting: the LED breathes orange; short press = keep ---------------------------
    board = setup()
    assert leds == ["orange breathing"] and board.writes == [("breathe", 3000, 255, 90, 0)], (leds, board.writes)
    hold(0.1, bounce=True)
    wait_for(lambda: said)
    assert "SHORT PRESS: keep" in out and not any("LONG PRESS" in x for x in out), out
    assert answers() == ["keep it"] and sent[0][1]["device_id"] == pb.DEVICE, sent
    assert said == ["Great, Friday at 6 PM it is."], said
    assert pb.now["pending"] is None and pb.now["answered"] == TIME and pb.now["mode"] == "ready"
    time.sleep(uq.FLASH_S + 0.1)
    assert leds == ["orange breathing", "green flash (keep)", "bright white"], leds
    assert (0, 255, 0) in board.writes and board.writes[-1] == (255, 255, 255), board.writes
    print("OK UNO Q short press: SHORT PRESS: keep; 'keep it' sent once; LEDs", " -> ".join(leds))
    hold(0.1)  # kept already: nothing waits now
    assert answers() == ["keep it"] and any(x.startswith("SHORT PRESS: ignored") for x in out), out
    print("OK UNO Q short press after the keep: ignored, logged")

    # ---- long press = find another time: yellow flash, neutral change, agents decide again -----
    board = setup()
    t0 = time.monotonic()
    down()
    time.sleep(0.2)
    assert answers() == [] and not any("PRESS" in x for x in out), "not yet at 0.2 s"
    wait_for(lambda: "LONG PRESS: find another time" in out)
    t_long = time.monotonic() - t0
    assert 0.28 <= t_long < 0.45, t_long
    wait_for(lambda: answers())
    assert answers() == ["pick a different time"], sent
    time.sleep(0.6)  # still held after the change: the release must do nothing
    up()
    time.sleep(0.1)
    wait_for(lambda: said)
    assert said == ["Okay, let me look for a new time."] and answers() == ["pick a different time"], (said, sent)
    assert pb.now["pending"] is None and pb.now["answered"] == TIME and pb.now["mode"] == "talking"
    assert not any(x.startswith("SHORT PRESS") for x in out), out
    assert leds == ["orange breathing", "yellow flash (find another time)", "yellow breathing"], leds
    assert (255, 255, 0) in board.writes and board.writes[-1] == ("breathe", 3000, 255, 255, 0), board.writes
    print(f"OK UNO Q long press: LONG PRESS: find another time at {t_long:.2f} s, 'pick a different time' sent, LEDs",
          " -> ".join(leds), "; release ignored")
    hold(0.4)  # agents deciding: a long press now does nothing
    assert answers() == ["pick a different time"] and any("LONG PRESS: ignored" in x for x in out), out

    # ---- no time waiting: presses are ignored and logged, nothing sent, no flash ----------------
    for kw, why in [(dict(pending=None, heard=None), "no session"),
                    (dict(mode="talking", pending=None), "still deciding"),
                    (dict(pending=None), "waiting on the other person")]:
        setup(**kw)
        if why == "no session":
            pb.now["sid"] = None
        pb.refresh()
        hold(0.1)
        hold(0.4)
        assert sent == [] and said == [], (why, sent)
        assert sum(x.startswith(("SHORT PRESS: ignored", "LONG PRESS: ignored")) for x in out) == 2, (why, out)
        assert all(why in x for x in out if "ignored" in x) and not any("flash" in x for x in leds), (why, out, leds)
    print("OK UNO Q presses with no time waiting (no session / agents deciding / already answered): ignored, logged")

    # ---- the server refuses: nothing said, still orange ---------------------------------------
    setup(answer={"ok": False, "error": "no open question"})
    hold(0.1)
    time.sleep(uq.FLASH_S + 0.1)
    assert said == [] and pb.now["pending"] == TIME and any("not sent as keep" in x for x in out), out
    assert leds[-1] == "orange breathing", leds
    print("OK UNO Q server refused: silent, the time still waits (orange breathing)")

    # ---- the poll loop: sketch counters drive on_down / on_up ---------------------------------
    board = setup()
    import threading
    threading.Thread(target=uq.poll, args=(board, pb.fsr), daemon=True).start()
    board.press(); time.sleep(0.1); board.release()
    wait_for(lambda: said)
    assert answers() == ["keep it"]
    setup()
    pb.fsr = uq.BridgeFSR()
    board2 = uq.FakeBoard()
    pb.led = uq.BridgeLED(board2)
    threading.Thread(target=uq.poll, args=(board2, pb.fsr), daemon=True).start()
    time.sleep(0.1)
    board2.press(); wait_for(lambda: answers()); board2.release()
    assert answers() == ["pick a different time"]
    print("OK UNO Q poll loop: short press -> keep; long press -> find another time")
    assert not (STUBS / "arecord.calls").exists(), "arecord ran on the UNO Q"
    print("OK UNO Q: arecord never ran")

    # ---- hearing nothing (the Pi's mic; reply() lives in the shared pi_pebble.py) -------------
    pb.wanted = uq._wanted
    for case, wav, asr, server in [
        ("clip too short", b"\0" * 8000, "keep it", None),
        ("no words", b"\0" * 32000, "", None),
        ("only punctuation", b"\0" * 32000, " . ", None),
        ("speech to text failed", b"\0" * 32000, RuntimeError("ASR 500"), None),
        ("server: unclear", b"\0" * 32000, "mmm banana",
         {"ok": False, "error": "unclear answer, question stays open", "say": "Sorry, I missed that. Want to change it?"}),
    ]:
        setup(answer=server)
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
        assert pb.now["led"] == before == pb.ORANGE_BREATHE and pb.now["pending"] == TIME, case
    print("OK hearing nothing (short clip, no words, ASR error, server unclear): silent, one MIC line, "
          "nothing sent for empty or short, LED back to orange breathing")
    src = (ROOT / "pebble" / "pi_pebble.py").read_text()
    assert "didn't catch" not in src and "couldn't hear" not in src, "no spoken retry prompts left"
finally:
    pb.print = uq.print = REAL_PRINT
