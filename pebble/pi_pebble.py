"""Ketchup pebble on a Raspberry Pi (gpiozero + ALSA). Talks to server/tap_server.py.

Touch two pebbles together (KY-003 Hall sensor) -> POST /tap; the server pairs
two taps within 2 s. Then poll GET /sessions/<id> and show the state:
  white = idle, yellow = agents negotiating, red = a question waits for the owner
  (spoken once), green = confirmed by both (held 60 s, then white).
Voice: Meta's open-weights MMS-TTS (facebook/mms-tts-eng) runs on the pebble,
loaded once in the background; espeak-ng (an OS voice, not a model) speaks until
it's loaded, or if torch/transformers aren't installed. PEBBLE_TTS=espeak skips MMS.
Hold-to-talk: while the pressure sensor (FSR) is pressed, arecord records; on
release the WAV goes to Muse Voice Transcribe, the text to POST
/sessions/<id>/answer, and the reply ("say") is spoken. The mic is live only
while pressed; the temp WAV is deleted right after it is read.

Pins (BCM): LED red 27, green 22, blue 23 (common cathode); FSR 24 (10K
pulldown, pressed = high); Hall 17 (KY-003, low = magnet).

    export META_API_KEY=...                       # Meta Model API key
    export KETCHUP_SERVER=http://<laptop-ip>:8787  # tap server
    export PEBBLE_ID=mayo                          # or frankie
    python3 pi_pebble.py
"""
import io
import json
import os

os.environ.setdefault("OMP_NUM_THREADS", "1")  # torch hangs on the Pi with 4 threads; set before torch loads
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import wave

SERVER = os.environ.get("KETCHUP_SERVER", "http://localhost:8787").rstrip("/")
OWNER = os.environ.get("PEBBLE_ID", "mayo").lower()
DEVICE = OWNER if OWNER.startswith("pebble-") else f"pebble-{OWNER}"
CARD = os.environ.get("PEBBLE_AUDIO", "plughw:CARD=Device,DEV=0")
PHRASE = os.environ.get("PEBBLE_PHRASE")  # optional: the plan said out loud, e.g. "let's catch up Thursday"
GREEN_S = float(os.environ.get("PEBBLE_GREEN_S", 60))
ASR_URL = "https://api.meta.ai/v1/asr/transcribe"
TTS = os.environ.get("PEBBLE_TTS", "mms").lower()  # mms (falls back to espeak) | espeak
MMS_ID = "facebook/mms-tts-eng"

WHITE, YELLOW, RED, GREEN, OFF = (1, 1, 1), (1, 1, 0), (1, 0, 0), (0, 1, 0), (0, 0, 0)
DONE = ("booked", "agreed (dry run, not booked)")

led = fsr = hall = None
now = {"sid": None, "heard": None, "answered": None, "green_until": 0, "rec": None, "wav": None, "busy": False}
speaking = threading.Lock()


def show(color):
    if now.get("fading"):
        return  # a fade owns the LED until it ends
    if tuple(led.value) != color:
        print("LED", {WHITE: "white", YELLOW: "yellow", RED: "red", GREEN: "green", OFF: "off"}[color], flush=True)
        led.value = color


def fade_white_yellow_white(secs=2.0, steps=40):
    """Smooth white -> yellow -> white (only blue changes), e.g. while saying "let me check with"."""
    now["fading"] = True
    print("LED fade white-yellow-white", flush=True)
    for i in range(steps + 1):
        x = i / steps
        led.value = (1, 1, abs(1 - 2 * x))  # blue 1 -> 0 -> 1
        time.sleep(secs / steps)
    now["fading"] = False


# ---- speech out: Meta MMS-TTS, espeak-ng fallback ------------------------------
ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
        "sixteen seventeen eighteen nineteen").split()
TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()
ORD = {"one": "first", "two": "second", "three": "third", "five": "fifth", "eight": "eighth",
       "nine": "ninth", "twelve": "twelfth"}
DAYS = {"Mon": "Monday", "Tue": "Tuesday", "Tues": "Tuesday", "Wed": "Wednesday", "Thu": "Thursday",
        "Thur": "Thursday", "Thurs": "Thursday", "Fri": "Friday", "Sat": "Saturday", "Sun": "Sunday"}
MONTHS = {"Jan": "January", "Feb": "February", "Mar": "March", "Apr": "April", "Jun": "June", "Jul": "July",
          "Aug": "August", "Sep": "September", "Sept": "September", "Oct": "October", "Nov": "November",
          "Dec": "December"}
MONTH_RE = "January|February|March|April|May|June|July|August|September|October|November|December"


def words(n):
    """12 -> 'twelve', 45 -> 'forty-five', 2026 -> 'two thousand twenty-six'."""
    if n < 20:
        return ONES[n]
    if n < 100:
        return TENS[n // 10] + ("-" + ONES[n % 10] if n % 10 else "")
    if n < 1000:
        return ONES[n // 100] + " hundred" + (" " + words(n % 100) if n % 100 else "")
    if n < 1_000_000:
        return words(n // 1000) + " thousand" + (" " + words(n % 1000) if n % 1000 else "")
    return " ".join(ONES[int(d)] for d in str(n))


def ordinal(n):
    """3 -> 'third', 21 -> 'twenty-first', 30 -> 'thirtieth'."""
    w = words(n)
    head, _, last = w.rpartition("-") if "-" in w else w.rpartition(" ")
    last = ORD.get(last) or (last[:-1] + "ieth" if last.endswith("y") else last + "th")
    return (head + ("-" if "-" in w else " ") if head else "") + last


def clock_words(h, m, ampm=""):
    """6, 0, 'PM' -> 'six pee em'; 10, 30, 'AM' -> 'ten thirty ay em'; 9, 5 -> 'nine oh five'."""
    out = words(int(h)) + ("" if not m or int(m) == 0 else f" oh {ONES[int(m)]}" if int(m) < 10 else
                           " " + words(int(m)))
    return out + {"a": " ay em", "p": " pee em"}.get(ampm[:1].lower(), "")


def mms_text(text):
    """Text -> what MMS-TTS can say. Its vocab is a-z, apostrophe, hyphen, space and digits 0-6
    (no 7/8/9, no colon); it drops punctuation. So spell out times, dates and numbers, expand
    day/month abbreviations, lowercase. Keeps . ? ! so the text can be split into sentences."""
    t = text.replace("\u2019", "'").replace("&", " and ")
    t = re.sub(r"[\u201c\u201d\"()\[\]]", "", t)
    t = re.sub(r"\b(" + "|".join(sorted(DAYS, key=len, reverse=True)) + r")\b\.?", lambda m: DAYS[m[1]], t)
    t = re.sub(r"\b(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\b\.?", lambda m: MONTHS[m[1]], t)
    t = re.sub(r"\b(\d{1,2})(?::(\d{2}))?\s*([AaPp])\.?[Mm]\b\.?(?=\s*(\S?))",
               lambda m: clock_words(m[1], m[2], m[3]) + ("." if m[0].endswith(".") and not m[4] else ""), t)
    t = re.sub(r"\b(\d{1,2}):(\d{2})\b", lambda m: clock_words(m[1], m[2]), t)
    t = re.sub(r"\b(" + MONTH_RE + r") (\d{1,2})(?:st|nd|rd|th)?\b", lambda m: f"{m[1]} {ordinal(int(m[2]))}", t)
    t = re.sub(r"\b(\d+)(?:st|nd|rd|th)\b", lambda m: ordinal(int(m[1])), t)
    t = re.sub(r"(\d)\s*min\b", r"\1 minutes", t)
    t = re.sub(r"\b(\d+)\.(\d+)\b", lambda m: words(int(m[1])) + " point " + " ".join(ONES[int(d)] for d in m[2]), t)
    t = re.sub(r"\d+", lambda m: f" {words(int(m[0]))} ", t)
    t = re.sub(r"\bET\b", "", t)
    t = re.sub(r"\b[Tt]onight\b", "this evening", t)  # MMS says "tu night"
    t = re.sub(r"\s*[:;]\s*", ", ", t)
    t = re.sub(r"[^a-zA-Z' ,.?!-]", " ", t).lower()
    t = re.sub(r"\s+([,.?!])", r"\1", t)
    return re.sub(r"\s+", " ", t).strip()


def chunks(text):
    """Split on sentence ends; a sentence under 4 words joins the next, or the one before if
    it's last (MMS garbles very short ones)."""
    out = []
    for s in (x for x in re.split(r"(?<=[.?!])\s+", text) if x.strip()):
        if out and len(out[-1].split()) < 4:
            out[-1] += " " + s
        else:
            out.append(s)
    if len(out) > 1 and len(out[-1].split()) < 4:
        last = out.pop()
        out[-1] += " " + last
    return out


mms = {}  # loaded once: model, tokenizer, torch, numpy


def load_mms():
    try:
        import numpy as np
        import torch
        torch.set_num_threads(1)
        from transformers import AutoTokenizer, VitsModel
        t0 = time.monotonic()
        m = VitsModel.from_pretrained(MMS_ID).eval()
        m.speaking_rate, m.noise_scale, m.noise_scale_duration = 0.85, 0.05, 0.1  # calm, clear consonants
        mms.update(model=m, tok=AutoTokenizer.from_pretrained(MMS_ID), torch=torch, np=np)
        for _ in mms_pcm("warming up the voice now"):  # first run is slow; pay it at startup
            pass
        print(f"VOICE Meta MMS-TTS ready ({time.monotonic() - t0:.0f} s)", flush=True)
    except Exception as e:
        print(f"VOICE MMS-TTS unavailable ({type(e).__name__}: {e}); using espeak-ng", flush=True)


def mms_pcm(text, gap=0.0):
    """Text -> 16 kHz mono int16 PCM from MMS-TTS, one sentence at a time (no added
    silence between), so playback can start after the first sentence instead of the whole reply."""
    torch, np, m, tok = mms["torch"], mms["np"], mms["model"], mms["tok"]
    first = True
    for c in chunks(mms_text(text)):
        if not re.search("[a-z]", c):
            continue
        torch.manual_seed(0)  # same line, same voice
        with torch.no_grad():
            a = m(**tok(c, return_tensors="pt")).waveform[0].numpy()
        a = (a / max(abs(a).max(), 1e-6) * 0.9 * 32767).astype(np.int16)
        yield (b"" if first else np.zeros(int(16000 * gap), np.int16).tobytes()) + a.tobytes()
        first = False


def say(text):
    print(f'SAY "{text}"', flush=True)
    with speaking:
        if mms:
            t0, play = time.monotonic(), None
            try:
                for pcm in mms_pcm(text):  # play sentence 1 while sentence 2 is made
                    if not play:
                        print(f"TIME first voice {time.monotonic() - t0:.1f} s", flush=True)
                        play = subprocess.Popen(["aplay", "-q", "-D", CARD, "-t", "raw", "-f", "S16_LE",
                                                 "-r", "16000", "-c", "1"], stdin=subprocess.PIPE)
                    play.stdin.write(pcm)
                    play.stdin.flush()
                if play:
                    play.stdin.close()
                    play.wait()
                return
            except Exception as e:
                if play:
                    play.kill()
                print("MMS-TTS error", e, "- using espeak-ng", flush=True)
        tts_bin = shutil.which("espeak-ng") or shutil.which("espeak")
        if not tts_bin:
            print("no espeak-ng or espeak; run: sudo apt install -y espeak-ng", flush=True)
            return
        tts = subprocess.Popen([tts_bin, "--stdout", text], stdout=subprocess.PIPE)
        subprocess.run(["aplay", "-q", "-D", CARD], stdin=tts.stdout)
        tts.wait()


def call(path, body=None):
    req = urllib.request.Request(SERVER + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return json.load(e)


def transcribe(wav):
    b = uuid.uuid4().hex
    settings = json.dumps({"model": "muse-voice-transcribe-1.0", "mode": "PUSH_TO_TALK", "audioEncoding": "WAV"})
    body = (f'--{b}\r\nContent-Disposition: form-data; name="request"\r\nContent-Type: application/json\r\n\r\n'
            f'{settings}\r\n--{b}\r\nContent-Disposition: form-data; name="audio"; filename="answer.wav"\r\n'
            f'Content-Type: audio/wav\r\n\r\n').encode() + wav + f"\r\n--{b}--\r\n".encode()
    req = urllib.request.Request(ASR_URL, data=body, headers={
        "Authorization": f"Bearer {os.environ['META_API_KEY']}",
        "Content-Type": f"multipart/form-data; boundary={b}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r).get("transcript", "").strip()


# ---- Hall sensor: touch pebbles together ------------------------------------
def on_tap():
    if now["sid"] or now["busy"]:
        return  # already in a session
    now["busy"] = True
    show(YELLOW)
    print("TAP -> /tap", flush=True)
    r = call("/tap", {"device_id": DEVICE, **({"phrase": PHRASE} if PHRASE else {})})
    print("TAP", r, flush=True)
    if r.get("status") == "paired":
        now.update(sid=r["session_id"], heard=None, answered=None)
    else:
        show(WHITE)
    now["busy"] = False


# ---- FSR: hold to talk ---------------------------------------------------------
RELEASE_GRACE_S = 0.35  # the FSR flickers off mid-squeeze; only stop after 0.35 s fully released


def on_press():
    if now.get("stop"):  # pressed again inside the grace window: keep the same recording going
        now["stop"].cancel()
        now["stop"] = None
    if now["rec"]:
        return
    fd, now["wav"] = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    print("MIC on", flush=True)
    now["rec"] = subprocess.Popen(["arecord", "-q", "-D", CARD, "-f", "S16_LE", "-r", "16000", "-c", "1",
                                   "-t", "wav", now["wav"]])


def on_release():
    if now["rec"]:
        now["stop"] = threading.Timer(RELEASE_GRACE_S, stop_recording)
        now["stop"].start()


def stop_recording():
    now["stop"] = None
    rec, path = now["rec"], now["wav"]
    if not rec or fsr.is_pressed:
        return
    rec.terminate()  # arecord closes the WAV cleanly on SIGTERM
    rec.wait()
    now["rec"] = None
    print("MIC off", flush=True)
    with open(path, "rb") as f:
        wav = f.read()
    os.remove(path)  # no audio kept on the pebble
    threading.Thread(target=reply, args=(wav,), daemon=True).start()


def reply(wav):
    if len(wav) < 16000:  # under ~0.5 s: a bump, not speech
        return
    t0 = time.monotonic()
    try:
        text = transcribe(wav)
        print(f"TIME transcribe {time.monotonic() - t0:.1f} s", flush=True)
    except Exception as e:
        print("ASR error", e, flush=True)
        return say("Sorry, I couldn't hear that. Try again.")
    print(f'HEARD "{text}"', flush=True)
    if not text:
        return say("Sorry, I didn't catch that.")
    if not now["sid"]:
        return say("Touch pebbles with a friend first.")
    t0 = time.monotonic()
    r = call(f"/sessions/{now['sid']}/answer", {"device_id": DEVICE, "answer": text})
    print(f"TIME server reply {time.monotonic() - t0:.1f} s", flush=True)
    print("ANSWER", {k: r.get(k) for k in ("ok", "mapped_to", "error", "state")}, flush=True)
    line = r.get("say") or "Nothing to answer right now."
    if r.get("ok"):  # answered: the agents take it from here (the server may be busy for a while)
        now["answered"] = now["heard"]
        if "check with" in line.lower():
            threading.Thread(target=fade_white_yellow_white, daemon=True).start()
        else:
            show(YELLOW)
    say(line)


# ---- poll the session and show it on the LED ----------------------------------
def tick():
    if not now["sid"]:
        if not now["busy"]:
            show(GREEN if time.monotonic() < now["green_until"] else WHITE)
        return
    s = call(f"/sessions/{now['sid']}")
    st = s.get("state")
    if st in DONE:
        show(GREEN)
        now.update(sid=None, green_until=time.monotonic() + GREEN_S)
        if s.get("speak", {}).get(DEVICE):
            say(s["speak"][DEVICE])
    elif st == "failed" or "error" in s:
        print("session ended:", st or s["error"], flush=True)
        now["sid"] = None
        show(WHITE)
    elif DEVICE in s.get("questions", {}) and \
            (s["change_round"], s["questions"][DEVICE]["say"]) != now["answered"]:
        show(RED)
        q = (s["change_round"], s["questions"][DEVICE]["say"])
        if q != now["heard"] and not now["rec"]:  # say each new question once, never over the mic
            now["heard"] = q
            say(q[1])
    else:
        show(YELLOW)


def main():
    global led, fsr, hall
    from gpiozero import Button, RGBLED
    if TTS == "mms":
        threading.Thread(target=load_mms, daemon=True).start()  # espeak-ng speaks until it's loaded
    led = RGBLED(red=27, green=22, blue=23)  # common cathode: high = on
    fsr = Button(24, pull_up=None, active_state=True, bounce_time=0.02)  # 10K pulldown, press = high
    hall = Button(17, pull_up=True, bounce_time=0.05)  # KY-003 pulls low with a magnet
    hall.when_pressed = lambda: threading.Thread(target=on_tap, daemon=True).start()
    fsr.when_pressed, fsr.when_released = on_press, on_release
    print(f"Ketchup pebble {DEVICE} -> {SERVER}", flush=True)
    show(WHITE)
    while True:
        try:
            tick()
        except Exception as e:  # server down or Wi-Fi blip: keep going
            print("poll error", e, flush=True)
        time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        led and led.off()
