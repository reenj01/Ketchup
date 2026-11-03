"""End-to-end check of a running simulator over HTTP (real models, mock calendars).

  python web/sim_e2e.py http://localhost:8000 [/tmp/morning.wav]

The same flow as the physical Dots: pair, and the agents pencil in a time with
no question first. Mayo asks for a change: by voice when a WAV is given (it
goes through Muse Voice Transcribe), else typed "no, pick a time at night".
Frankie's Dot must only hear that Mayo wants a different time, never her words.
The agents renegotiate, both keep the new time, and it is booked. Prints every
state and model call with timings, and fetches MMS-TTS lines.
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
code, r = call("/sim/api/tap", {"phrase": "let's hang out next week"})
assert code == 200, r
sid = r["session_id"]
print("paired, session", sid)
states = []


def pencilled(v):
    states.append(v["state"])
    return v["state"] == "failed" or (v["state"] == "pencilled_in" and all(p["question"] for p in v["pebbles"].values()))


v = wait(sid, pencilled)
assert v["state"] == "pencilled_in", v.get("error")
assert "asking_time_of_day" not in states, "asked a question before proposing"
assert all(p["kind"] == "confirm" for p in v["pebbles"].values()), "first thing asked must be the proposed time"
first = (v.get("hold") or {}).get("slot")
print("  proposed with no question first:", first)
print("  Dots say:", {k: p["question"] for k, p in v["pebbles"].items()})

code, audio = call("/sim/api/tts", {"session_id": sid, "text": v["pebbles"]["mayo"]["question"]})
print(f"  TTS fetch: HTTP {code}, {len(audio) if isinstance(audio, bytes) else audio} bytes of WAV")

if WAV:
    code, r = call(f"/sim/api/session/{sid}/voice?pebble=mayo", open(WAV, "rb").read(), "audio/wav")
    words = r.get("heard") or ""
    print(f"  Mayo (voice): HTTP {code} heard={words!r} -> {r.get('mapped_to')}; says {r.get('say')!r}")
else:
    words = "no, pick a time at night"
    code, r = call(f"/sim/api/session/{sid}/say", {"pebble": "mayo", "text": words})
    print(f"  Mayo (typed): HTTP {code} {words!r} -> {r.get('mapped_to')}; says {r.get('say')!r}")
assert code == 200 and r.get("ok"), r

v = wait(sid, lambda v: v["state"] in ("failed", "booked") or (v["state"] == "pencilled_in" and v["change_round"] >= 1
                                                                and all(p["question"] for p in v["pebbles"].values())))
assert v["state"] == "pencilled_in", v.get("error")
fq = v["pebbles"]["frankie"]["question"]
said = words.lower().strip(" .!?")
assert fq.startswith("Mayo wants a different time.") and said not in fq.lower(), fq
print("  renegotiated:", (v.get("hold") or {}).get("slot"))
print("  Dots say:", {k: p["question"] for k, p in v["pebbles"].items()})
print("  Frankie hears only that Mayo wants a different time: ok")

for k in ("mayo", "frankie"):
    code, r = call(f"/sim/api/session/{sid}/say", {"pebble": k, "text": "keep it"})
    print(f"  {k} keeps: HTTP {code} -> {r.get('mapped_to')}; says {r.get('say')!r}")
t0 = time.monotonic()
code, audio = call("/sim/api/tts", {"session_id": sid, "text": r.get("say", "")})
print(f"  TTS fetch (new line): HTTP {code}, {len(audio) if isinstance(audio, bytes) else audio} bytes, "
      f"{time.monotonic() - t0:.2f} s round trip")
v = wait(sid, lambda v: v["state"] in ("booked", "failed"))
assert v["state"] == "booked", v.get("error")
print("final state:", v["state"], "|", (v.get("agreed_slot") or {}).get("slot"))
_, v = call(f"/sim/api/session/{sid}")
print("\nconversation:")
for line in v["lines"]:
    print("  " + line)
print("\nmodel calls:")
for c in v["calls"]:
    print(f"  {c['at']} ET  {c['model']:<28} {c['seconds']:>6.2f} s  {'ok ' if c['ok'] else 'FAIL'} {c['what']}")
print(f"\ntotal {time.monotonic() - t_start:.1f} s, {len(v['calls'])} model calls")
