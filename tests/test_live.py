"""The "Connect to hardware demo" feed, no network beyond localhost: python tests/test_live.py

server/live.py (on the Mac) pushes every printed line and model call to the web simulator's
POST /live/<session>; web/live.html polls GET /live/<session>. Checks: the token gates both
ways, events are capped, secrets are redacted before they leave, lines from two threads stay
whole, and the tap server's LED/HEARD lines come out as the page parses them.
"""
import io
import json
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ["KETCHUP_CALENDAR"] = "mock"
os.environ["KETCHUP_TTS"] = "off"
SESSION, TOKEN = secrets.token_hex(6), secrets.token_hex(24)
os.environ["KETCHUP_LIVE_TOKEN"] = TOKEN
os.environ["FAKE_TEST_API_KEY"] = "zz-not-a-real-key-1234"
for d in ("web", "brain", "server"):
    sys.path.insert(0, str(ROOT / d))
import live  # noqa: E402
import live_store  # noqa: E402
import sim  # noqa: E402
import tap_server as ts  # noqa: E402
import timing  # noqa: E402

# ---- the store: claim, token, caps ----
assert live_store.post("ab", TOKEN, [])[0] == 400  # session too short
assert live_store.post("abcdef12", "short", [])[0] == 400  # token too short
assert live_store.get("abcdef12", TOKEN) == (200, {"events": [], "next": 0, "waiting": True})
assert live_store.post("abcdef12", TOKEN, [{"kind": "log", "text": "x" * 900}, {"kind": "nope"}, "junk"]) == \
    (200, {"ok": True, "stored": 1})
assert live_store.post("abcdef12", secrets.token_hex(24), [{"kind": "log", "text": "hi"}])[0] == 403
assert live_store.get("abcdef12", secrets.token_hex(24))[0] == 403
code, out = live_store.get("abcdef12", TOKEN)
assert code == 200 and len(out["events"]) == 1 and len(out["events"][0]["text"]) == 400, out
live_store.MAX_EVENTS = 5
live_store.post("abcdef99", TOKEN, [{"kind": "log", "text": str(i)} for i in range(8)])
code, out = live_store.get("abcdef99", TOKEN, since=0)
assert [e["text"] for e in out["events"]] == ["3", "4", "5", "6", "7"] and out["dropped"] == 3, out
assert [e["seq"] for e in live_store.get("abcdef99", TOKEN, since=6)[1]["events"]] == [7, 8]
live_store.MAX_LIVE, live_store.MAX_EVENTS = 2, 3000
assert live_store.post("abcdef77", TOKEN, [])[0] == 503  # full and nobody idle
assert live_store.post("abcdef77", TOKEN, [], now=time.time() + live_store.IDLE_S + 1)[0] == 200  # idle one replaced
live_store._live.clear()
live_store.MAX_LIVE = 8
print("OK store: token gates post and get, 400-char fields, cap keeps the newest, idle sessions replaced")

# ---- redaction ----
for raw in (f"token={TOKEN}", "Authorization: Bearer abc.def-123456789", "got sk-proj-AAAAAAAAAAAAAAAAAAAA",
            "zz-not-a-real-key-1234", "x " + "a1" * 20):
    assert "[redacted]" in live.redact(raw) and TOKEN not in live.redact(raw), raw
for keep in ('HEARD Mayo\'s Dot (voice): "keep it"', "model meta-models/muse-spark-open-1026 1.2 s", "the token expired"):
    assert live.redact(keep) == keep, live.redact(keep)
print("OK redact: env keys, the token, Bearer, sk-, long random strings; normal lines untouched")

# ---- the real web/sim.py handler + the real pusher ----
srv = ThreadingHTTPServer(("127.0.0.1", 0), sim.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_port}"
live.URL, live.TOKEN = f"{base}/live/{SESSION}", TOKEN
term = io.StringIO()
real_out = sys.stdout
live._state["on"] = True
sys.stdout = live._Tee(term)
timing.HOOKS.append(live._on_call)
threading.Thread(target=live._sender, daemon=True).start()


def both(i):
    for j in range(20):
        print(f"thread {i} line {j} " + "words " * 5)


ws = [threading.Thread(target=both, args=(i,)) for i in range(2)]
[w.start() for w in ws]
[w.join() for w in ws]
print(f"secret {os.environ['FAKE_TEST_API_KEY']} here")
timing.record(kind="model", label="You are a scheduling agent. Someone said a plan phrase out loud.",
              model="meta-models/muse-spark-open-1026", ok=True, seconds=1.23, input="Phrase: let's catch up",
              output='{"activity": "catch up"}')
ts.sessions.clear()


class Fake:
    sid, state = "abcd1234", "pencilled_in"
    lock = threading.Lock()

    def public(self):
        return {"state": self.state, "questions": {"pebble-mayo": {"say": "How about..."}}}


ts.sessions["abcd1234"] = Fake()
ts.watch_leds()
ts.heard(Fake(), "pebble-mayo", "no, pick a time at night", "voice",
         {"asr_s": 0.8, "asr_model": "muse-voice-transcribe-1.0"})
ts.heard(Fake(), "pebble-frankie", "pick a different time", "long press", {})
deadline = time.time() + 10
while time.time() < deadline:
    code, out = live_store.get(SESSION, TOKEN)
    if sum(e["kind"] == "model" for e in out["events"]) >= 2 and any("HEARD Frankie" in e.get("text", "") for e in out["events"]):
        break
    time.sleep(0.2)
sys.stdout = real_out

req = urllib.request.Request(f"{base}/live/{SESSION}?since=0", headers={"X-Live-Token": TOKEN})
got = json.loads(urllib.request.urlopen(req, timeout=5).read())
try:
    urllib.request.urlopen(f"{base}/live/{SESSION}", timeout=5)
    raise AssertionError("read without a token")
except urllib.error.HTTPError as e:
    assert e.code == 400
logs = [e["text"] for e in got["events"] if e["kind"] == "log"]
models = [e for e in got["events"] if e["kind"] == "model"]
assert sorted(l for l in logs if l.startswith("thread")) == sorted(f"thread {i} line {j} " + "words " * 5
                                                                    for i in range(2) for j in range(20)), logs
assert "secret [redacted] here" in logs, logs
assert "LED Mayo's Dot: orange breathing (time waiting for an answer) [abcd1234]" in logs, logs
assert "LED Frankie's Dot: bright white (answered) [abcd1234]" in logs, logs
both = ts.dot_leds({"state": "pencilled_in", "questions": {"pebble-mayo": {}, "pebble-frankie": {}}})
assert both == {"pebble-mayo": "orange breathing (time waiting for an answer)",
                "pebble-frankie": "yellow breathing (waiting for the other person to answer first)"}, both
assert ts.dot_leds({"state": "pencilled_in", "questions": {"pebble-frankie": {}}})["pebble-frankie"] \
    == "orange breathing (time waiting for an answer)"
assert set(ts.dot_leds({"state": "booked"}).values()) == {"green (booked, 10 s, then dim white)"}
assert 'HEARD Mayo\'s Dot (voice, speech to text 0.8 s): "no, pick a time at night"' in logs, logs
assert 'HEARD Frankie\'s Dot (long press): "pick a different time"' in logs, logs
plan = next(m for m in models if m["model"] == "meta-models/muse-spark-open-1026")
assert plan["what"] == "Turned the spoken plan into dates, length, activity" and plan["seconds"] == 1.23 \
    and plan["in"] == "Phrase: let's catch up" and plan["out"] == '{"activity": "catch up"}', plan
asr = next(m for m in models if m["model"] == "muse-voice-transcribe-1.0")
assert asr["what"] == "Speech to text on Mayo's Dot" and asr["out"] == "no, pick a time at night", asr
assert TOKEN not in json.dumps(got)
html = sim.HERE.joinpath("sim.html").read_text()
assert 'href="/live"' in html and "Connect to hardware demo" in html
page = urllib.request.urlopen(f"{base}/live", timeout=5).read().decode()
assert "X-Live-Token" in page and "The real Dots, live." in page
srv.shutdown()
print(f"OK feed: {len(logs)} lines + {len(models)} model calls pushed through web/sim.py; whole lines from 2 threads,"
      " LED/HEARD lines, model in/out, no token or key in the feed; /live page and sim button served")
