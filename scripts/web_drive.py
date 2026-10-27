"""Drive a full web-page session over HTTP and screenshot the page.

    python web/server.py &            # in another terminal
    python scripts/web_drive.py [--real] [--shots DIR]

Both pick mornings (Mayo 1 hour, Frankie 30 min). The agents agree a slot
and a backup and pencil it in. Mayo says "not thursday, something later",
then keeps the new time. Frankie never replies, so the Skip to end of day
button auto-confirms it. Prints every chat line.
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
    ap.add_argument("--phrase", default="let's catch up Thursday or Friday")
    ap.add_argument("--mayo", nargs=2, default=["mornings", "1 hour"], metavar=("TIME", "LENGTH"))
    ap.add_argument("--frankie", nargs=2, default=["mornings", "30 min"], metavar=("TIME", "LENGTH"))
    ap.add_argument("--change", default="not thursday, something later", help="Mayo's change reason")
    ap.add_argument("--no-change", action="store_true", help="Mayo keeps the first hold")
    ap.add_argument("--out", default="", help="write timings + messages as JSON here")
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

    T = {}
    mark = lambda k: T.__setitem__(k, time.time())  # noqa: E731
    mark("pressed")
    sid = call("/api/start", {"phrase": a.phrase, "dry_run": not a.real})["session_id"]
    print("session", sid)
    if page:
        page.goto(f"{BASE}/?session={sid}")
    say = lambda person, text: call(f"/api/session/{sid}/say", {"person": person, "text": text})  # noqa: E731

    wait(sid, lambda s: s["waiting_for"].get("mayo") == "time" and s["waiting_for"].get("frankie") == "time")
    mark("questions_shown")
    say("mayo", a.mayo[0])
    say("frankie", a.frankie[0])
    wait(sid, lambda s: s["waiting_for"].get("mayo") == "length" and s["waiting_for"].get("frankie") == "length")
    mark("time_answers_mapped")
    say("mayo", a.mayo[1])
    wait(sid, lambda s: "mayo" not in s["waiting_for"])
    shot("1-questions")
    mark("last_answer")
    if page:  # Frankie's last answer typed into the page itself
        page.fill('form[data-person="frankie"] input', a.frankie[1])
        page.click('form[data-person="frankie"] button')
    else:
        say("frankie", a.frankie[1])
    wait(sid, lambda s: sum(m["channel"] == "middle" and m["who"] != "system" for m in s["messages"]) >= 3)
    shot("2-negotiating")
    asked = lambda s: s["waiting_for"].get("mayo") == "change" and s["waiting_for"].get("frankie") == "change"
    wait(sid, asked)
    mark("pencilled_ask")
    shot("3-pencilled-in")
    if not a.no_change:
        say("mayo", a.change)
        mark("change_asked")
        wait(sid, lambda s: s["change_round"] == 1 and asked(s) and (s["hold"] or {}).get("status") == "tentative")
        mark("moved_ask")
    say("mayo", "no, keep it")
    wait(sid, lambda s: "mayo" not in s["waiting_for"])
    shot("4-changed-kept")
    mark("skip")  # Frankie stays silent; end the day now
    if page:
        page.click("#skip")
    else:
        call(f"/api/session/{sid}/skip_to_end_of_day", {})
    s = wait(sid, lambda s: s["state"] in ("done", "failed"))
    mark("done")
    shot("5-confirmed")
    seg = lambda x, y: round(T[y] - T[x], 1) if x in T and y in T else None  # noqa: E731
    timing = {"press_to_questions": seg("pressed", "questions_shown"),
              "time_answers_mapped": seg("questions_shown", "time_answers_mapped"),
              "last_answer_to_pencilled_ask": seg("last_answer", "pencilled_ask"),
              "change_to_new_ask": seg("change_asked", "moved_ask"),
              "waiting_on_agents_total": round(sum(v for v in (
                  seg("pressed", "questions_shown"), seg("questions_shown", "time_answers_mapped"),
                  seg("last_answer", "pencilled_ask"), seg("change_asked", "moved_ask")) if v), 1),
              "whole_session": seg("pressed", "done"), "calls": s["timing"]}
    print("TIMING", json.dumps(timing))
    if a.out:
        Path(a.out).write_text(json.dumps({"timing": timing, "messages": s["messages"]}, indent=1))
    print(f"state {s['state']}  slot {s['slot']}  backup {s['backup']}  hold {s['hold']}  trace {s['trace_file']}")
    for m in s["messages"]:
        who = m["who"] if m["channel"] == "middle" else (m["channel"] + ("" if m["who"] == "user" else "'s agent"))
        print(f"[{m['channel']:7}] {who}: {m['text']}")
    if page:
        pw.stop()


if __name__ == "__main__":
    main()
