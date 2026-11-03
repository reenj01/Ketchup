"""Ketchup pebble on a Raspberry Pi (gpiozero + ALSA). Talks to server/tap_server.py.

Touch two pebbles together (KY-003 Hall sensor) -> POST /tap; the server pairs
two taps within 5 s (the server's KETCHUP_PAIR_WINDOW_S) and the agents pick a time from both
calendars right away, in the background. On pairing, Mayo's Dot says "Dots connected! We're now
starting auto schedule." (PEBBLE_SAY_CONNECTED=1, the default for mayo only; frankie stays silent),
slower than other speech (PEBBLE_CONNECTED_RATE), with a pause after each sentence, while its LED
stays bright white (not blue). The line is made with MMS at startup so it plays at once.
Then poll GET /sessions/<id> and show it on the LED:
  dim white        = at rest (no session)
  bright white     = tapped / paired (and while the connected line plays), a time is ready,
                     or waiting on the other person
  yellow breathing = the agents are deciding (picking or re-picking a time)
  blue             = the Dot (the AI agent) is talking
  solid yellow     = the mic is on (recording)
  green            = booked by both (held 60 s, then dim white)
Announce: when a time is ready the Dot says nothing on its own; it says the time on the next
tap (magnet) or FSR press (PEBBLE_ANNOUNCE=tap, the default). After the owner has answered once,
new times are said as they arrive. PEBBLE_ANNOUNCE=auto says every time as it arrives.
Voice: Meta's open-weights MMS-TTS (facebook/mms-tts-eng) runs on the pebble,
loaded once in the background; espeak-ng (an OS voice, not a model) speaks until
it's loaded, or if torch/transformers aren't installed. PEBBLE_TTS=espeak skips MMS.
Sentences are made one after another in a thread while the one before plays, through one
aplay per reply (opened while sentence 1 is made); MMS's leading/trailing silence is trimmed
and sentences are 150 ms apart (PEBBLE_GAP_MS).
Mic is a toggle: press the pressure sensor (FSR) once -> mic ON (arecord); press again ->
mic OFF, the WAV goes to Muse Voice Transcribe, the text to POST /sessions/<id>/answer, and
the reply ("say") is spoken. Auto-stops after 20 s (PEBBLE_REC_MAX_S). A press while the Dot
is talking stops the voice and turns the mic on. The first edge of a press acts at once; edges
within 50 ms of the last one are bounce (PEBBLE_DEBOUNCE_MS). The temp WAV is deleted right
after it is read.

A change ("no, pick a time at night") sends both agents back to deciding, until both keep it.

Pins (BCM): LED red 27, green 22, blue 23 (common cathode); FSR 24 (10K
pulldown, pressed = high); Hall 17 (KY-003, low = magnet).

    export META_API_KEY=...                       # Meta Model API key
    export KETCHUP_SERVER=http://<laptop-ip>:8787  # tap server
    export PEBBLE_ID=mayo                          # or frankie
    python3 pi_pebble.py
"""
import io
import json
import math
import os

os.environ.setdefault("OMP_NUM_THREADS", "1")  # torch hangs on the Pi with 4 threads; set before torch loads
import queue
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
BREATHE_S = float(os.environ.get("PEBBLE_BREATHE_S", 3.0))  # one slow yellow breath
ANNOUNCE = os.environ.get("PEBBLE_ANNOUNCE", "tap").lower()  # tap (say a new time on tap/press) | auto
SAY_CONNECTED = os.environ.get("PEBBLE_SAY_CONNECTED", "1" if OWNER == "mayo" else "0") == "1"
CONNECTED_LINE = os.environ.get("PEBBLE_CONNECTED_LINE", "Dots connected! We're now starting auto schedule.")
# the connected line is said slower than the rest (MMS VITS speaking_rate; the rest use 0.85),
# one sentence at a time, with a pause after each sentence
CONNECTED_RATE = float(os.environ.get("PEBBLE_CONNECTED_RATE", 0.75))
CONNECTED_PAUSE_S = float(os.environ.get("PEBBLE_CONNECTED_PAUSE_MS", 450)) / 1000
CONNECTED_TAIL_S = 0.2  # silence after its last sentence, before the LED starts breathing
CONNECTED_ESPEAK_WPM = int(os.environ.get("PEBBLE_CONNECTED_ESPEAK_WPM", 130))  # espeak's default is 175
TALK_MIN_S = float(os.environ.get("PEBBLE_TALK_MIN_S", 2.5))  # after a change both Dots breathe at least this long
DIM = float(os.environ.get("PEBBLE_DIM", 0.12))  # dim white at rest
DEBOUNCE_S = float(os.environ.get("PEBBLE_DEBOUNCE_MS", 50)) / 1000  # FSR edges closer than this are bounce
REC_MAX_S = float(os.environ.get("PEBBLE_REC_MAX_S", 20))  # the mic turns itself off after this
REC_MIN_S = 0.3  # a second press this soon after mic ON is a double hit, not "stop"
GAP_S = float(os.environ.get("PEBBLE_GAP_MS", 150)) / 1000  # silence between spoken sentences
ASR_URL = "https://api.meta.ai/v1/asr/transcribe"
TTS = os.environ.get("PEBBLE_TTS", "mms").lower()  # mms (falls back to espeak) | espeak
MMS_ID = "facebook/mms-tts-eng"

WHITE, DIM_WHITE, YELLOW, BLUE, GREEN, OFF = (1, 1, 1), (DIM, DIM, DIM), (1, 1, 0), (0, 0, 1), (0, 1, 0), (0, 0, 0)
BREATHE = "yellow breathing"  # the agents are deciding
NAMES = {WHITE: "bright white", DIM_WHITE: "dim white", YELLOW: "yellow (mic on)", BLUE: "blue (talking)",
         GREEN: "green", OFF: "off", BREATHE: BREATHE}
DONE = ("booked", "agreed (dry run, not booked)")
TALKING = ("paired", "proposing")  # the agents are picking a time

led = fsr = hall = None
now = {"sid": None, "heard": None, "answered": None, "pending": None, "engaged": False, "green_until": 0,
       "rec": None, "wav": None, "busy": False, "led": None, "gen": 0, "round": 0, "talk_until": 0,
       "mode": "ready", "speaking": False, "hush": None, "player": [], "edge": -1.0, "rec_t0": 0,
       "booking": False, "greeting": False}
ready_pcm = {}  # text -> MMS PCM made at startup (the connected line), played without waiting on the model
speaking = threading.Lock()
led_lock = threading.Lock()
mic_lock = threading.Lock()


def breath_level(t):
    """Brightness t seconds into a breath: 1 -> 0.1 -> 1, a smooth cosine (the sketch uses the same)."""
    return 0.1 + 0.9 * (0.5 + 0.5 * math.cos(2 * math.pi * (t % BREATHE_S) / BREATHE_S))


def wanted():
    """The one colour the LED should show now, most urgent first."""
    if now["rec"]:
        return YELLOW
    if now["greeting"]:
        return WHITE  # "Dots connected!": stays bright white while it plays, then breathes
    if now["speaking"] or now["booking"]:
        return BLUE  # talking (or about to say it's booked)
    if now["sid"]:
        return BREATHE if now["mode"] == "talking" else WHITE
    if now["busy"]:
        return WHITE  # tapped, waiting for the other Dot
    if time.monotonic() < now["green_until"]:
        return GREEN
    return DIM_WHITE


def refresh():
    """Show wanted() on the LED; logs 'LED <colour>' on every change."""
    c = wanted()
    with led_lock:
        if now["led"] == c:
            return
        now["gen"] += 1
        gen, now["led"] = now["gen"], c
        print("LED", NAMES[c], flush=True)
        if c != BREATHE:
            led.value = c
            return
        # the UNO Q sketch breathes on its own (led.breathe); on the Pi a thread steps the PWM
        if getattr(led, "breathe", None) and led.breathe(BREATHE_S, YELLOW):
            return
    threading.Thread(target=_breath, args=(gen, YELLOW), daemon=True).start()


def _breath(gen, rgb):
    t0 = time.monotonic()
    while True:
        with led_lock:
            if now["gen"] != gen:
                return
            v = breath_level(time.monotonic() - t0)
            led.value = tuple(v * c for c in rgb)
        time.sleep(0.04)


def set_mode(mode):
    """talking (the agents are deciding) | ready (a time is ready, or waiting on the other person)."""
    now["mode"] = mode
    refresh()


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
               lambda m: clock_words(m[1], m[2], m[3]) + ("." if m[0].endswith(".") and not m[4].islower() else ""), t)
    t = re.sub(r"\b(\d{1,2}):(\d{2})\b", lambda m: clock_words(m[1], m[2]), t)
    t = re.sub(r"\b(" + MONTH_RE + r") (\d{1,2})(?:st|nd|rd|th)?\b", lambda m: f"{m[1]} {ordinal(int(m[2]))}", t)
    t = re.sub(r"\b(\d+)(?:st|nd|rd|th)\b", lambda m: ordinal(int(m[1])), t)
    t = re.sub(r"(\d)\s*min\b", r"\1 minutes", t)
    t = re.sub(r"\b(\d+)\.(\d+)\b", lambda m: words(int(m[1])) + " point " + " ".join(ONES[int(d)] for d in m[2]), t)
    t = re.sub(r"\d+", lambda m: f" {words(int(m[0]))} ", t)
    t = re.sub(r"\bET\b", "", t)
    t = re.sub(r"\b[Tt]onight\b", "this evening", t)  # MMS says "tu night"
    t = re.sub(r"\b[Dd]ots\b", "dotts", t)  # MMS blurs "both dots ask" into "both Dutch"
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
mms_lock = threading.Lock()  # one synthesis at a time (the connected line changes the rate for its run)


def load_mms():
    try:
        import numpy as np
        import torch
        torch.set_num_threads(1)
        from transformers import AutoTokenizer, VitsModel
        t0 = time.monotonic()
        m = VitsModel.from_pretrained(MMS_ID).eval()
        m.speaking_rate, m.noise_scale, m.noise_scale_duration = MMS_RATE, 0.05, 0.1  # calm, clear consonants
        mms.update(model=m, tok=AutoTokenizer.from_pretrained(MMS_ID), torch=torch, np=np)
        for _ in mms_pcm("warming up the voice now"):  # first run is slow; pay it at startup
            pass
        if SAY_CONNECTED:  # made once now, so it plays the moment the Dots pair
            ready_pcm[CONNECTED_LINE] = b"".join(connected_pcm())
        print(f"VOICE Meta MMS-TTS ready ({time.monotonic() - t0:.0f} s)", flush=True)
    except Exception as e:
        print(f"VOICE MMS-TTS unavailable ({type(e).__name__}: {e}); using espeak-ng", flush=True)


def trim(a, np, floor=0.03, keep_s=0.02, pause_s=0.2):
    """Cut MMS's leading and trailing near-silence (under 3% of the peak), keeping 20 ms, and
    shorten any pause inside the sentence (e.g. at a comma) to at most pause_s."""
    quiet = np.abs(a) <= floor * max(float(np.abs(a).max()), 1e-6)
    loud = np.flatnonzero(~quiet)
    if not len(loud):
        return a[:0]
    keep = int(16000 * keep_s)
    a, quiet = a[max(0, loud[0] - keep):loud[-1] + keep], quiet[max(0, loud[0] - keep):loud[-1] + keep]
    # quiet runs, in 10 ms frames
    n = len(a) // 160
    fq = quiet[:n * 160].reshape(n, 160).all(1)
    out, i, cap = [], 0, int(pause_s * 100)
    while i < n:
        j = i
        while j < n and fq[j]:
            j += 1
        if j - i > cap:  # a long pause: keep its first and last halves of pause_s
            out.append((i * 160, (i + cap // 2) * 160))
            out.append(((j - cap // 2) * 160, j * 160))
        elif j > i:
            out.append((i * 160, j * 160))
        k = j
        while k < n and not fq[k]:
            k += 1
        if k > j:
            out.append((j * 160, k * 160))
        i = k
    out.append((n * 160, len(a)))
    return np.concatenate([a[x:y] for x, y in out])


MMS_RATE = 0.85  # VitsModel.speaking_rate for all speech (1.0 = the model's own pace)


def mms_one(c, rate=MMS_RATE):
    """One sentence -> trimmed int16 samples. `rate` is VITS's speaking_rate (its length_scale is
    1 / rate: slower, same pitch). Set on the model for this call only, under mms_lock."""
    torch, np, m, tok = mms["torch"], mms["np"], mms["model"], mms["tok"]
    with mms_lock, torch.no_grad():
        torch.manual_seed(0)  # same line, same voice
        m.speaking_rate = rate
        try:
            a = trim(m(**tok(c, return_tensors="pt")).waveform[0].numpy(), np)
        finally:
            m.speaking_rate = MMS_RATE
    return (a / max(abs(a).max(), 1e-6) * 0.9 * 32767).astype(np.int16) if len(a) else a


def mms_pcm(text, gap=None):
    """Text -> 16 kHz mono int16 PCM from MMS-TTS, one sentence at a time, silence trimmed,
    `gap` seconds (GAP_S) of silence before every sentence after the first."""
    np = mms["np"]
    gap = GAP_S if gap is None else gap
    first = True
    for c in chunks(mms_text(text)):
        if not re.search("[a-z]", c):
            continue
        a = mms_one(c)
        if not len(a):
            continue
        yield (b"" if first else np.zeros(int(16000 * gap), np.int16).tobytes()) + a.tobytes()
        first = False


def connected_pcm():
    """The connected line: each sentence on its own (even short ones), at CONNECTED_RATE, with
    CONNECTED_PAUSE_S of silence after each sentence and a CONNECTED_TAIL_S tail after the last."""
    np = mms["np"]
    said = [c for c in re.split(r"(?<=[.?!])\s+", mms_text(CONNECTED_LINE)) if re.search("[a-z]", c)]
    for i, c in enumerate(said):
        a = mms_one(c, CONNECTED_RATE)
        if len(a):
            rest = CONNECTED_PAUSE_S if i < len(said) - 1 else CONNECTED_TAIL_S
            yield a.tobytes() + np.zeros(int(16000 * rest), np.int16).tobytes()


def hush():
    """Stop the voice now (a press while the Dot talks). True if it was talking."""
    stop, was = now["hush"], now["speaking"]
    if stop:
        stop.set()
    for p in list(now["player"]):
        try:
            p.kill()
        except Exception:
            pass
    if was:
        print("SAY stopped by a press", flush=True)
    return was


def _wait(procs, stop):
    """Wait for playback to finish, or kill it as soon as `stop` is set."""
    while any(p.poll() is None for p in procs):
        if stop.wait(0.03):
            for p in procs:
                p.kill()
            return


def say_mms(text, stop, t_text):
    """Pipelined: a thread makes sentence N+1 while sentence N plays. One aplay for the whole
    reply, started right away so the sound card opens while sentence 1 is made. True if done."""
    play = subprocess.Popen(["aplay", "-q", "-D", CARD, "-t", "raw", "-f", "S16_LE", "-r", "16000", "-c", "1"],
                            stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
    now["player"] = [play]
    parts = queue.Queue()

    def make():
        try:
            for pcm in ([ready_pcm[text]] if text in ready_pcm else
                        connected_pcm() if text == CONNECTED_LINE else mms_pcm(text)):
                if stop.is_set():
                    break
                parts.put(pcm)
        except Exception as e:
            parts.put(e)
        parts.put(None)

    threading.Thread(target=make, daemon=True).start()
    first = True
    try:
        while (pcm := parts.get()) is not None:
            if isinstance(pcm, Exception):
                raise pcm
            if stop.is_set():
                break
            if first:
                print(f"TIME first voice {time.monotonic() - t_text:.2f} s after the text", flush=True)
                first = False
            play.stdin.write(pcm)  # blocks while it plays; the next sentence is being made meanwhile
            play.stdin.flush()
        play.stdin.close()
        _wait([play], stop)
        return True
    except (BrokenPipeError, OSError, ValueError):
        if stop.is_set():
            return True
        play.kill()
        print("aplay stopped early - trying espeak-ng", flush=True)
        return False
    except Exception as e:
        play.kill()
        print("MMS-TTS error", e, "- using espeak-ng", flush=True)
        return False


def say_espeak(text, stop):  # the connected line slower (-s words per minute)
    tts_bin = shutil.which("espeak-ng") or shutil.which("espeak")
    if not tts_bin:
        print("no espeak-ng or espeak; run: sudo apt install -y espeak-ng", flush=True)
        return
    slow = ["-s", str(CONNECTED_ESPEAK_WPM)] if text == CONNECTED_LINE else []
    tts = subprocess.Popen([tts_bin, *slow, "--stdout", text], stdout=subprocess.PIPE)
    play = subprocess.Popen(["aplay", "-q", "-D", CARD], stdin=tts.stdout, stderr=subprocess.DEVNULL)
    tts.stdout.close()
    now["player"] = [tts, play]
    _wait([tts, play], stop)


def say(text):
    print(f'SAY "{text}"', flush=True)
    t_text = time.monotonic()
    with speaking:
        try:
            if now["rec"]:  # the owner pressed to talk over it: don't talk into the mic
                print("SAY skipped: the mic is on", flush=True)
                return
            stop = now["hush"] = threading.Event()
            now["speaking"] = True
            refresh()  # blue (bright white for the connected line)
            if not (mms and say_mms(text, stop, t_text)) and not stop.is_set():
                say_espeak(text, stop)
        finally:
            now.update(speaking=False, hush=None, player=[])
            refresh()


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
    if now["busy"]:
        return
    if now["sid"]:  # in a session: a tap says the time on the table (again)
        return announce(again=True)
    now["busy"] = True
    refresh()  # bright white: tapped
    print("TAP -> /tap", flush=True)
    try:
        r = call("/tap", {"device_id": DEVICE, **({"phrase": PHRASE} if PHRASE else {})})
    except Exception as e:
        r = {"error": str(e)}
    print("TAP", r, flush=True)
    if r.get("status") == "paired":  # the agents start picking a time right away
        now.update(sid=r["session_id"], heard=None, answered=None, pending=None, engaged=False, green_until=0,
                   round=0, talk_until=0, mode="talking")
        if SAY_CONNECTED:  # its own thread: the agents and the polling go on while it plays
            now["greeting"] = True
            threading.Thread(target=say_connected, daemon=True).start()
    now["busy"] = False
    refresh()  # paired: yellow breathing (Mayo: bright white while the connected line plays); else dim white


def say_connected():
    try:
        say(CONNECTED_LINE)
    finally:
        now["greeting"] = False
        refresh()


def announce(again=False):
    """Say the time waiting for this owner: once when new, or again on a tap. True if said."""
    q = now["pending"]
    if not q or (q == now["heard"] and not again) or now["rec"]:
        return False
    now["heard"] = q
    say(q[1])
    return True


# ---- FSR: press to toggle the mic -----------------------------------------------
def on_press():
    """FSR went down. The first edge acts at once; any edge within DEBOUNCE_S of the last
    one (down or up) is bounce, so one press is one toggle."""
    t = time.monotonic()
    quiet = t - now["edge"] >= DEBOUNCE_S
    now["edge"] = t
    if quiet:
        toggle()


def on_release():
    now["edge"] = time.monotonic()  # releasing does nothing; it only arms the debounce


def toggle():
    if now["rec"]:
        if time.monotonic() - now["rec_t0"] >= REC_MIN_S:
            mic_off()
        return
    if now["pending"] and now["pending"] != now["heard"] and not now["speaking"]:
        threading.Thread(target=announce, daemon=True).start()  # a new time: this press hears it
        return
    mic_on()  # a press while the Dot talks opens the mic (yellow at once) and cuts the voice off
    hush()


def mic_on():
    with mic_lock:
        if now["rec"]:
            return
        fd, now["wav"] = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        now["rec_t0"] = time.monotonic()
        now["rec"] = rec = subprocess.Popen(["arecord", "-q", "-D", CARD, "-f", "S16_LE", "-r", "16000", "-c", "1",
                                             "-t", "wav", now["wav"]])
        print(f"MIC ON  - talk now; press again to send (auto-stop {REC_MAX_S:.0f} s)", flush=True)
    refresh()  # solid yellow
    t = threading.Timer(REC_MAX_S, lambda: now["rec"] is rec and mic_off(auto=True))
    t.daemon = True
    t.start()


def mic_off(auto=False):
    with mic_lock:
        rec, path = now["rec"], now["wav"]
        if not rec:
            return
        rec.terminate()  # arecord closes the WAV cleanly on SIGTERM
        rec.wait()
        secs = time.monotonic() - now["rec_t0"]
        now["rec"] = None
    print(f"MIC OFF - {secs:.1f} s{' (auto-stop)' if auto else ''}, sending", flush=True)
    refresh()
    try:
        with open(path, "rb") as f:
            wav = f.read()
    finally:
        os.remove(path)  # no audio kept on the pebble
    threading.Thread(target=reply, args=(wav,), daemon=True).start()


def reply(wav):
    if len(wav) < 16000:  # under ~0.5 s: a bump, not speech
        print("MIC clip too short, nothing sent", flush=True)
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
        return say("Touch Dots with a friend first.")
    t0 = time.monotonic()
    r = call(f"/sessions/{now['sid']}/answer", {"device_id": DEVICE, "answer": text})
    print(f"TIME server reply {time.monotonic() - t0:.1f} s", flush=True)
    print("ANSWER", {k: r.get(k) for k in ("ok", "mapped_to", "error", "state")}, flush=True)
    line = r.get("say") or "Nothing to answer right now."
    if r.get("ok"):  # answered: from now on new times are said as they arrive
        now.update(answered=now["pending"], pending=None, engaged=True)
        if r.get("mapped_to") == "change":
            now["mode"] = "talking"  # both agents go back to deciding (breathing once the reply is said)
    say(line)


# ---- poll the session and show it on the LED ----------------------------------
def tick():
    if not now["sid"]:
        refresh()  # dim white, or green until it runs out
        return
    s = call(f"/sessions/{now['sid']}")
    st = s.get("state")
    q = s.get("questions", {}).get(DEVICE)
    q = q and (s["change_round"], q["say"])
    if st not in DONE and s.get("change_round", 0) > now["round"]:
        # someone asked for a change: the agents are deciding again, on both Dots, even when the
        # backup time makes it instant (the other Dot may never see "proposing" between polls)
        now.update(round=s["change_round"], talk_until=time.monotonic() + TALK_MIN_S, pending=None)
    talking = time.monotonic() < now["talk_until"]
    if st in DONE:
        line = s.get("speak", {}).get(DEVICE)
        # blue while it says so (also while it waits for a line still playing), then the full green minute
        now.update(sid=None, pending=None, mode="ready", booking=bool(line))
        if line:
            refresh()
            say(line)
        now.update(green_until=time.monotonic() + GREEN_S, booking=False)
        print("LED green for", GREEN_S, "s", flush=True)
        refresh()
    elif st == "failed" or "error" in s:
        print("session ended:", st or s["error"], flush=True)
        now.update(sid=None, pending=None, mode="ready")
        refresh()
    elif s.get("busy"):  # the agents hold the session: only the state is known
        if st in TALKING or talking:
            set_mode("talking")
    elif talking:
        set_mode("talking")
    elif q and q != now["answered"]:  # a time is ready for this owner
        if q != now["pending"]:
            print("READY (tap or press to hear it):" if not (ANNOUNCE == "auto" or now["engaged"]) else "READY",
                  q[1], flush=True)
        now["pending"] = q
        set_mode("ready")
        if ANNOUNCE == "auto" or now["engaged"]:  # never over the mic; else it waits for a tap/press
            announce()
    elif st in TALKING:
        now["pending"] = None
        set_mode("talking")
    else:  # kept it, waiting on the other person
        now["pending"] = None
        set_mode("ready")


def main():
    global led, fsr, hall
    from gpiozero import Button, RGBLED
    if TTS == "mms":
        threading.Thread(target=load_mms, daemon=True).start()  # espeak-ng speaks until it's loaded
    led = RGBLED(red=27, green=22, blue=23)  # common cathode: high = on
    # no gpiozero bounce_time: its debounce waits for the level to settle, which an FSR near the
    # threshold drags out; on_press acts on the first edge and ignores the next 50 ms instead
    fsr = Button(24, pull_up=False, bounce_time=None)  # internal pull-down (plus the 10K), so a loose resistor cannot float the pin; press = high
    hall = Button(17, pull_up=True, bounce_time=0.05)  # KY-003 pulls low with a magnet
    hall.when_pressed = lambda: threading.Thread(target=on_tap, daemon=True).start()
    fsr.when_pressed, fsr.when_released = on_press, on_release
    print(f"Ketchup Dot {DEVICE} -> {SERVER}", flush=True)
    refresh()
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
