"""Drive a full web-page session over HTTP and screenshot the page.

    python web/server.py &            # in another terminal
    python scripts/web_drive.py [--real] [--shots DIR]

Mayo: mornings, 1 hour. Frankie: evenings, 30 min. Then Mayo asks for a
change, then both keep it. Prints every chat line.
"""
import argparse
import json
import time
import urllib.request
from pathlib import Path

BASE = "http://localhost:8000"


def call(path, body=None):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None,
                                 method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def wait(sid, cond, timeout=400):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = call(f"/api/session/{sid}")
        if cond(s) or s["state"] in ("failed",):
            return s
        time.sleep(1)
    raise TimeoutError(cond)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", action="store_true")
    ap.add_argument("--shots", default="")
    ap.add_argument("--phrase", default="let's catch up this week")
    a = ap.parse_args()
    shots = Path(a.shots) if a.shots else None
    page = None
    if shots:
        from playwright.sync_api import sync_playwright
        shots.mkdir(parents=True, exist_ok=True)
        pw = sync_playwright().start()
        page = pw.chromium.launch(executable_path="/usr/bin/google-chrome").new_page(viewport={"width": 1500, "height": 900})

    def shot(name):
        if page:
            time.sleep(2)  # two polls
            page.screenshot(path=str(shots / f"web-{name}.png"))
            print(f"-- screenshot {shots / f'web-{name}.png'}")

    sid = call("/api/start", {"phrase": a.phrase, "dry_run": not a.real})["session_id"]
    print("session", sid)
    if page:
        page.goto(f"{BASE}/?session={sid}")
    w = lambda person, q: lambda s: s["waiting_for"].get(person) == q  # noqa: E731
    say = lambda person, text: call(f"/api/session/{sid}/say", {"person": person, "text": text})  # noqa: E731

    wait(sid, lambda s: s["waiting_for"].get("mayo") == "time" and s["waiting_for"].get("frankie") == "time")
    say("mayo", "mornings")
    wait(sid, w("mayo", "length"))
    say("frankie", "evenings")
    wait(sid, w("frankie", "length"))
    say("mayo", "1 hour")
    wait(sid, lambda s: "mayo" not in s["waiting_for"])
    shot("1-questions")
    if page:  # Frankie's last answer typed into the page itself
        page.fill('form[data-person="frankie"] input', "30 min")
        page.click('form[data-person="frankie"] button')
    else:
        say("frankie", "30 min")
    wait(sid, lambda s: sum(m["channel"] == "middle" and m["who"] != "system" for m in s["messages"]) >= 3)
    shot("2-negotiating")
    wait(sid, lambda s: s["waiting_for"].get("mayo") == "change")
    shot("3-booked-ask")
    say("mayo", "yes, change it")
    wait(sid, lambda s: s["change_round"] == 1 and s["waiting_for"].get("mayo") == "change"
         and s["waiting_for"].get("frankie") == "change")
    shot("4-rebooked-ask")
    say("mayo", "no")
    wait(sid, lambda s: "mayo" not in s["waiting_for"])
    say("frankie", "no, that's perfect")
    s = wait(sid, lambda s: s["state"] in ("done", "failed"))
    shot("5-final")
    print(f"state {s['state']}  slot {s['slot']}  trace {s['trace_file']}")
    for m in s["messages"]:
        who = m["who"] if m["channel"] == "middle" else (m["channel"] + ("" if m["who"] == "user" else "'s agent"))
        print(f"[{m['channel']:7}] {who}: {m['text']}")
    if page:
        pw.stop()


if __name__ == "__main__":
    main()
