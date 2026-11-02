"""Frankie's pebble, simulated in a terminal, for a real-hardware run with the Pi (Mayo).

    python scripts/frankie_terminal.py [--url http://localhost:8787]

Press Enter at the same moment the Pi pebble is tapped (taps within 2 s pair).
Then each question for Frankie is printed; type Frankie's answer and press Enter.
It goes to the same /answer endpoint the real pebble uses, as text (no mic).
"""
import argparse
import json
import time
import urllib.error
import urllib.request

DEVICE = "pebble-frankie"
DONE = ("booked", "agreed (dry run, not booked)", "failed")


def call(url, path, body=None):
    req = urllib.request.Request(url + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return json.load(e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8787")
    url = ap.parse_args().url
    while True:
        input("Frankie: press Enter as you tap the Pi pebble... ")
        r = call(url, "/tap", {"device_id": DEVICE})
        print("TAP", r.get("status"))
        if r.get("status") == "paired":
            break
    sid, answered = r["session_id"], None
    while True:
        s = call(url, f"/sessions/{sid}")
        if s.get("state") in DONE:
            print("DONE", s["state"], "|", s.get("speak", {}).get(DEVICE, ""))
            return
        q = s.get("questions", {}).get(DEVICE)
        key = q and (s.get("change_round"), q["say"])
        if q and key != answered:
            print(f'\nFRANKIE HEARS "{q["say"]}"')
            text = input("Frankie says: ").strip()
            if text:
                a = call(url, f"/sessions/{sid}/answer", {"device_id": DEVICE, "answer": text})
                print("REPLY", a.get("say") or a.get("error"))
                answered = key
        time.sleep(1)


if __name__ == "__main__":
    main()
