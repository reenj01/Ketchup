"""Ketchup from the command line (simulates both pebbles).

    python brain/demo.py "let's catch up Thursday" --dry-run              # asks you each question
    python brain/demo.py "let's catch up Thursday" --mayo morning --frankie "night works for me" \
        --confirm change,keep --dry-run
    python brain/demo.py "let's catch up Thursday" --mayo timeout          # Mayo's agent uses her habit
    python brain/demo.py "let's catch up Thursday" --pick 2                # manual override, books slot 2
    python brain/demo.py "let's catch up Thursday" --mayo noon --frankie noon --confirm keep \
        --dry-run --extra-busy tests/fixtures/noon_blocked_thu.json        # force a fallback

Answers can be keywords or anything a person might say out loud.
--confirm sets both people's keep/change per round (comma list);
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
    confirms = {k: (a[f"{k}_confirm"] or a["confirm"] or "").split(",") for k in PEOPLE}
    confirms = {k: [x for x in v if x] for k, v in confirms.items()}
    while s.state not in DONE:
        for key, p in PEOPLE.items():
            q = s.questions.get(p["device_id"])
            while q:
                if q["kind"] == "time_of_day":
                    ans = a[key] if a[key] is not None else input(
                        f"[{p['name']}'s pebble] {q['say']} (Enter = no answer) ")
                    a[key] = ""  # answer once
                else:
                    ans = confirms[key].pop(0) if confirms[key] else input(
                        f"[{p['name']}'s pebble] {q['say']} ")
                if not ans or ans == "timeout":
                    break
                s.answer(p["device_id"], ans)
                q = s.questions.get(p["device_id"])
        s.advance(force=True)
    print(f"Final: {s.state}. Trace: {s.public()['trace_file']}")


if __name__ == "__main__":
    main()
