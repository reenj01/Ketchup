"""Labelled replay of the model calls, so the demo runs with no keys and no accounts.

KETCHUP_RECORD=1 (with keys): every model call that succeeds (Muse Spark Open via ask_json, and
Muse Voice Transcribe in scripts/sim_dot.py) is appended to replays/demo/calls.jsonl.
Replay: KETCHUP_REPLAY=1, or automatically when neither BASETEN_API_KEY nor META_API_KEY is set
(KETCHUP_REPLAY=0 turns it off). Each call is looked up by a hash of its request (prompt text or
WAV bytes, not the model), so the recorded run plays back exactly; the clock is frozen at the
recorded time (clock.py) so the prompts match. Every replayed call prints a "replay" line.
"""
import hashlib
import json
import os
import threading
import time
from pathlib import Path

DIR = Path(__file__).resolve().parent.parent / "replays" / "demo"
CALLS, META = DIR / "calls.jsonl", DIR / "meta.json"
MODE = os.environ.get("KETCHUP_REPLAY", "")
ON = MODE == "1" or (MODE != "0" and not (os.environ.get("BASETEN_API_KEY") or os.environ.get("META_API_KEY")))
RECORD = not ON and os.environ.get("KETCHUP_RECORD") == "1"
PACE = float(os.environ.get("KETCHUP_REPLAY_PACE", 2))  # seconds per replayed call: the Dots' "thinking" LEDs show
INFO = json.loads(META.read_text()) if META.exists() else {}
_lock, _seen, _table = threading.Lock(), {}, None


class Miss(RuntimeError):
    pass


def key(*parts):
    h = hashlib.sha256()
    for p in parts:
        h.update(p if isinstance(p, bytes) else p.encode())
        h.update(b"\0")
    return h.hexdigest()[:24]


def recorded_now():
    return INFO.get("now")


def banner():
    if ON:
        print(f"REPLAY: recorded real Muse Spark Open calls from {INFO.get('recorded', '?')}, no keys needed "
              f"(clock frozen at {INFO.get('now', '?')}; set KETCHUP_REPLAY=0 and a key to call the models live)",
              flush=True)


def lookup(k, step):
    global _table
    with _lock:
        if _table is None:
            _table = {}
            for line in (CALLS.read_text().splitlines() if CALLS.exists() else []):
                row = json.loads(line)
                _table.setdefault(row["key"], []).append(row)
        rows = _table.get(k)
        if not rows:
            raise Miss(f"REPLAY MISS at step '{step}': this input is not in the recorded run (replays/demo). "
                       "Run the demo commands from the README as written, or set BASETEN_API_KEY or "
                       "META_API_KEY (see .env.example) to call the models live.")
        n = _seen[k] = _seen.get(k, -1) + 1
        row = rows[min(n, len(rows) - 1)]
    time.sleep(PACE)
    print(f"[replay] {row['model']} (recorded {INFO.get('recorded', '?')}): {step}", flush=True)
    return row


def save(k, **row):
    with _lock:
        DIR.mkdir(parents=True, exist_ok=True)
        with CALLS.open("a") as f:
            f.write(json.dumps({"key": k, **row}) + "\n")


def transcribe(wav, live, model):
    """Speech to text for a recorded WAV: the recorded transcript in replay, else the live call."""
    k = key(b"asr", wav)
    if ON:
        return lookup(k, "speech to text")["transcript"]
    if RECORD and CALLS.exists():  # the same clip heard again: keep its first transcript, so a replay matches
        for line in CALLS.read_text().splitlines():
            row = json.loads(line)
            if row["key"] == k:
                return row["transcript"]
    text = live(wav)
    if RECORD:
        save(k, kind="asr", model=model, transcript=text)
    return text


banner()
