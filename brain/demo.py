"""Ketchup from the command line (simulates both pebbles).

    python brain/demo.py "let's catch up Thursday" --dry-run              # asks you each question
    python brain/demo.py "let's catch up Thursday" --mayo morning --frankie "night works for me" \
        --confirm change,keep --dry-run
    python brain/demo.py "let's catch up Thursday" --mayo timeout          # Mayo's agent uses her habit
    python brain/demo.py "let's catch up Thursday" --pick 2                # manual override, books slot 2
    python brain/demo.py "let's catch up Thursday" --mayo noon --frankie noon --confirm keep \
        --dry-run --extra-busy tests/fixtures/noon_blocked_thu.json        # force a fallback

    python brain/demo.py "let's catch up Thursday" --mayo morning --frankie night \
        --mayo-confirm keep --frankie-confirm "not thursday, something later" --dry-run \
        --skip-to-end-of-day    # Frankie's reason moves the hold; nobody replies -> confirmed now

Answers can be keywords or anything a person might say out loud.
An agreed time is pencilled in as a tentative hold. Without
--skip-to-end-of-day, a hold nobody answers stays tentative and the web or tap
server's timer confirms it at the deadline (10 PM ET, or 11:59 PM if held after 10 PM).
--confirm sets both people's keep/change per round (comma list, or | when a
reply has commas: "keep|not thursday, something later");
--mayo-confirm / --frankie-confirm set one person's.
"""
import argparse
import json
from datetime import datetime

from flow import DONE, Session
from gcal import PEOPLE
from parse_plan import TZ


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("phrase")
    ap.add_argument("--proposer", default="mayo", choices=list(PEOPLE))
    ap.add_argument("--dry-run", action="store_true", help="never book")
    ap.add_argument("--pick", type=int, help="manual override: book option N, skip questions and agents")
    ap.add_argument("--extra-busy", help="JSON of synthetic busy blocks per person (dry runs only, "
                    "never written to a calendar), e.g. tests/fixtures/noon_blocked_thu.json")
    ap.add_argument("--confirm", help="keep/change per round for both, e.g. change,keep")
    ap.add_argument("--skip-to-end-of-day", action="store_true",
                    help="when nobody else answers a hold, act as if the day ended: confirm it now")
    for k in PEOPLE:
        ap.add_argument(f"--{k}", help=f"{PEOPLE[k]['name']}'s morning/noon/night answer, or 'timeout'")
        ap.add_argument(f"--{k}-confirm", help=f"{PEOPLE[k]['name']}'s keep/change per round")
    a = vars(ap.parse_args())

    extra = None
    if a["extra_busy"]:
        if not a["dry_run"]:
            ap.error("--extra-busy needs --dry-run")
        raw = json.load(open(a["extra_busy"]))
        extra = {k: [tuple(datetime.fromisoformat(x).replace(tzinfo=TZ) for x in b) for b in v]
                 for k, v in raw.items() if k in PEOPLE}
    s = Session(a["phrase"], a["proposer"], a["dry_run"], extra_busy=extra)
    s.start(ask=not a["pick"])
    if a["pick"]:
        if s.state != "failed":
            s.propose(pick=a["pick"])
        return
    raw = {k: a[f"{k}_confirm"] or a["confirm"] or "" for k in PEOPLE}
    scripted = any(raw.values())  # scripted replies: once used up, that person stays silent
    confirms = {k: v.split("|" if "|" in v else ",") for k, v in raw.items()}
    confirms = {k: [x for x in v if x] for k, v in confirms.items()}
    while s.state not in DONE:
        answered = False
        for key, p in PEOPLE.items():
            q = s.questions.get(p["device_id"])
            while q:
                if q["kind"] == "time_of_day":
                    ans = a[key] if a[key] is not None else input(
                        f"[{p['name']}'s pebble] {q['say']} (Enter = no answer) ")
                    a[key] = ""  # answer once
                else:
                    ans = confirms[key].pop(0) if confirms[key] else "" if scripted else input(
                        f"[{p['name']}'s pebble] {q['say']} (Enter = no reply) ")
                if not ans or ans == "timeout":
                    break
                r = s.answer(p["device_id"], ans)
                answered = True
                if r.get("say"):
                    print(f'[{p["name"]}\'s pebble] {r["say"]}')
                if s.state == "pencilled_in":
                    s.advance()  # a keep or change acts right away
                q = s.questions.get(p["device_id"])
        if s.state == "pencilled_in" and not answered:  # nobody else is going to reply
            if not a["skip_to_end_of_day"]:
                h = s.result["hold"]
                print(f"Pencilled in {h['slot']} (tentative); confirms {h['deadline_text']} unless "
                      "someone changes it (the web or tap server timer does that).")
                break
            print("Skipping to end of day: nobody else replied.")
            s.skip_to_end_of_day()
        elif s.state != "pencilled_in":
            s.advance(force=True)
    print(f"Final: {s.state}. Trace: {s.public()['trace_file']}")


if __name__ == "__main__":
    main()
