"""End-to-end check of a running simulator over HTTP (real models, mock calendars).

  python web/sim_e2e.py http://localhost:8000 morning.wav

Pairs, answers Mayo by voice (the WAV goes through Muse Voice Transcribe) and
Frankie by typing, waits for the agents, keeps it on both pebbles, and prints
every state and model call with timings. Also fetches one MMS-TTS line.
"""
import json
import sys
import time
import urllib.error
import urllib.request

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://localhost:8000"
WAV = sys.argv[2] if len(sys.argv) > 2 else None


def call(path, data=None, kind="application/json"):
    body = data if isinstance(data, bytes) or data is None else json.dumps(data).encode()
    req = urllib.request.Request(BASE + path, data=body, headers={"Content-Type": kind},
                                 method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw, ct = r.read(), r.headers.get("Content-Type", "")
            return r.status, (json.loads(raw) if "json" in ct else raw)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def wait(sid, until, limit=240):
    seen, t0 = None, time.monotonic()
    while time.monotonic() - t0 < limit:
        _, v = call(f"/sim/api/session/{sid}")
        if v["state"] != seen:
            seen = v["state"]
            print(f"  state -> {seen}")
        if until(v):
            return v
        time.sleep(0.7)
    raise SystemExit(f"timed out in state {seen}")


t_start = time.monotonic()
code, r = call("/sim/api/tap", {"phrase": "let's catch up this week"})
assert code == 200, r
sid = r["session_id"]
print("paired, session", sid)
v = wait(sid, lambda v: all(p["question"] for p in v["pebbles"].values()) or v["state"] == "failed")
print("  questions:", {k: p["question"] for k, p in v["pebbles"].items()})

code, audio = call("/sim/api/tts", {"session_id": sid, "text": v["pebbles"]["mayo"]["question"]})
print(f"  TTS fetch: HTTP {code}, {len(audio) if isinstance(audio, bytes) else audio} bytes of WAV")

if WAV:
    code, r = call(f"/sim/api/session/{sid}/voice?pebble=mayo", open(WAV, "rb").read(), "audio/wav")
    print(f"  Mayo (voice): HTTP {code} heard={r.get('heard')!r} -> {r.get('mapped_to')}; says {r.get('say')!r}")
else:
    code, r = call(f"/sim/api/session/{sid}/say", {"pebble": "mayo", "text": "morning"})
    print(f"  Mayo (typed): HTTP {code} -> {r.get('mapped_to')}; says {r.get('say')!r}")
code, r = call(f"/sim/api/session/{sid}/say", {"pebble": "frankie", "text": "evening works best"})
print(f"  Frankie (typed): HTTP {code} -> {r.get('mapped_to')}; says {r.get('say')!r}")
t0 = time.monotonic()
code, audio = call("/sim/api/tts", {"session_id": sid, "text": r.get("say", "")})
print(f"  TTS fetch (new line): HTTP {code}, {len(audio) if isinstance(audio, bytes) else audio} bytes, "
      f"{time.monotonic() - t0:.2f} s round trip")

v = wait(sid, lambda v: v["state"] in ("pencilled_in", "failed", "booked"))
print("  pencilled in:", (v.get("hold") or {}).get("slot"))
print("  questions:", {k: p["question"] for k, p in v["pebbles"].items()})
for k in ("mayo", "frankie"):
    code, r = call(f"/sim/api/session/{sid}/say", {"pebble": k, "text": "keep it"})
    print(f"  {k} keeps: HTTP {code} -> {r.get('mapped_to')}")
v = wait(sid, lambda v: v["state"] in ("booked", "failed"))
print("final state:", v["state"], "|", (v.get("agreed_slot") or {}).get("slot"), "| LEDs green" if v["state"] == "booked" else "")
_, v = call(f"/sim/api/session/{sid}")
print("\nconversation:")
for line in v["lines"]:
    print("  " + line)
print("\nmodel calls:")
for c in v["calls"]:
    print(f"  {c['at']} ET  {c['model']:<28} {c['seconds']:>6.2f} s  {'ok ' if c['ok'] else 'FAIL'} {c['what']}")
print(f"\ntotal {time.monotonic() - t_start:.1f} s, {len(v['calls'])} model calls")
