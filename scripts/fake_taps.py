"""Pretend to be both pebbles and tap the server.

    python scripts/fake_taps.py [--url http://localhost:8787]

Case 1: taps 0.5 s apart -> paired, then waits for the agents' result.
Case 2: taps 3 s apart   -> both expire, no session.
Run the server with --dry-run so nothing is booked.
"""
import argparse
import json
import threading
import time
import urllib.request

PHRASE = "let's catch up Thursday"


def post(url, body):
    req = urllib.request.Request(url + "/tap", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def get(url, path):
    with urllib.request.urlopen(url + path, timeout=10) as r:
        return json.load(r)


def two_taps(url, gap):
    results = {}

    def go(device, phrase=None):
        body = {"device_id": device, **({"phrase": phrase} if phrase else {})}
        results[device] = post(url, body)

    a = threading.Thread(target=go, args=("pebble-mayo", PHRASE))
    b = threading.Thread(target=go, args=("pebble-frankie",))
    a.start(); time.sleep(gap); b.start()
    a.join(); b.join()
    for d, r in results.items():
        print(f"  {d}: {json.dumps(r)}")
    return results


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
        sid = sids.pop()
        while (s := get(url, f"/sessions/{sid}"))["status"] == "negotiating":
            time.sleep(1)
        print(f"  Session {sid}: {s['status']}")
        for t in s.get("agent_turns", []):
            print(f"    [round {t['round']}] {t['agent']}: {t['decision']} slot {t['pick']} - {t['reason']}")
        if s.get("agreed_slot"):
            print(f"    Agreed: {s['agreed_slot']['slot']} ({s['agreed_slot']['how']})")
        if s.get("error"):
            print(f"    Error: {s['error']}")

    print("\nCase 2: taps 3 s apart")
    r = two_taps(url, 3.0)
    ok2 = all(x["status"] == "expired" for x in r.values())
    print(f"  -> {'NOT PAIRED (correct)' if ok2 else 'FAIL'}")

    print(f"\n{'ALL GOOD' if ok1 and ok2 else 'SOMETHING FAILED'}")
    raise SystemExit(0 if ok1 and ok2 else 1)


if __name__ == "__main__":
    main()
