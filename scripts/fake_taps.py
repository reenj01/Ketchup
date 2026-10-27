"""Pretend to be both pebbles: tap, then answer the spoken questions by "voice".

    python scripts/fake_taps.py [--url http://localhost:8787]

Case 1: taps 0.5 s apart -> paired; Mayo says morning, Frankie says night
        (as voice transcripts); first proposal: Frankie wants a change, then both keep.
Case 2: taps 3 s apart -> both expire, no session.
Run the server with --dry-run so nothing is booked.
"""
import argparse
import json
import threading
import time
import urllib.error
import urllib.request

PHRASE = "let's catch up Thursday"
SCRIPT = {  # what each person "says" to each question, in order
    "pebble-mayo": {"time_of_day": ["I'm a morning person, before work is best"],
                    "confirm": ["yeah that works for me", "perfect, keep it"]},
    "pebble-frankie": {"time_of_day": ["ugh, mornings are rough. evening please"],
                       "confirm": ["hmm, can we do a different time?", "sure, sounds good"]},
}


def call(url, path, body=None):
    req = urllib.request.Request(url + path, data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return json.load(e)


def two_taps(url, gap):
    results = {}

    def go(device, phrase=None):
        results[device] = call(url, "/tap", {"device_id": device, **({"phrase": phrase} if phrase else {})})

    a = threading.Thread(target=go, args=("pebble-mayo", PHRASE))
    b = threading.Thread(target=go, args=("pebble-frankie",))
    a.start(); time.sleep(gap); b.start()
    a.join(); b.join()
    for d, r in results.items():
        print(f"  {d}: {json.dumps(r)}")
    return results


def converse(url, sid):
    """Poll like a pebble: when there is a question for me, 'say' it and answer."""
    said, seen = {d: {"time_of_day": 0, "confirm": 0} for d in SCRIPT}, set()
    while True:
        s = call(url, f"/sessions/{sid}")
        if s["state"] not in seen:
            seen.add(s["state"])
            print(f"  state -> {s['state']}" + (f"   nudge: {s['nudge']}" if s.get("nudge") and s["state"] == "asking_time_of_day" else ""))
        if s["state"] in ("booked", "agreed (dry run, not booked)", "failed"):
            return s
        for dev, q in s["questions"].items():
            lines = SCRIPT[dev][q["kind"]]
            i = said[dev][q["kind"]]
            if i >= len(lines):
                continue
            said[dev][q["kind"]] += 1
            r = call(url, f"/sessions/{sid}/answer", {"device_id": dev, "answer": lines[i]})
            print(f'  {dev} speaker: "{q["say"]}"  mic: "{lines[i]}" -> {r.get("mapped_to") or r.get("error")}')
        time.sleep(0.5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8787")
    url = ap.parse_args().url

    print("Case 1: taps 0.5 s apart")
    r = two_taps(url, 0.5)
    sids = {x.get("session_id") for x in r.values()}
    ok1 = all(x["status"] == "paired" for x in r.values()) and len(sids) == 1
    print(f"  -> {'PAIRED' if ok1 else 'FAIL'} (same session on both pebbles: {len(sids) == 1})")
    if ok1:
        s = converse(url, sids.pop())
        print("  Server log:")
        for line in s["log"]:
            print(f"    {line}")
        ok1 = s["state"] != "failed"

    print("\nCase 2: taps 3 s apart")
    r = two_taps(url, 3.0)
    ok2 = all(x["status"] == "expired" for x in r.values())
    print(f"  -> {'NOT PAIRED (correct)' if ok2 else 'FAIL'}")
    print(f"\n{'ALL GOOD' if ok1 and ok2 else 'SOMETHING FAILED'}")
    raise SystemExit(0 if ok1 and ok2 else 1)


if __name__ == "__main__":
    main()
