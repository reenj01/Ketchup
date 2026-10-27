"""End-to-end Ketchup brain: phrase -> plan -> 3 shared times -> pick -> booked.

    python brain/demo.py "let's catch up Thursday" [--pick N]

Every run saves an agent trace to traces/.
"""
import argparse
import json
from datetime import date, datetime, timedelta
from pathlib import Path

from book import book
from calendar_slots import DAY_START, freebusy, free_slots, pick_spread
from gcal import PEOPLE, calendar
from parse_plan import TZ, parse_plan

TRACES = Path(__file__).parent.parent / "traces"


def fmt(t):
    return t.strftime("%a %b %-d, %-I:%M %p ET")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("phrase")
    ap.add_argument("--pick", type=int, help="1-3, skips the prompt")
    args = ap.parse_args()

    now = datetime.now(TZ)
    trace = {"started_at": now.isoformat(), "input_phrase": args.phrase}
    people = [PEOPLE["mayo"], PEOPLE["frankie"]]
    trace["people"] = people
    try:
        print(f'Heard: "{args.phrase}"')
        parsed = parse_plan(args.phrase, now)
        plan = parsed["plan"]
        trace.update(model=parsed["model"], model_reasoning=parsed["reasoning"],
                     model_errors=parsed["errors"], parsed_plan=plan)
        print(f"Plan ({parsed['model']}): {json.dumps(plan)}")

        cal = calendar()
        ids = [p["calendar_id"] for p in people]
        start_day = max(date.fromisoformat(plan["start_date"]), now.date())
        end_day = max(date.fromisoformat(plan["end_date"]), start_day)
        trace["freebusy"] = []
        for attempt in ("requested window", "next week"):
            win_start = datetime(start_day.year, start_day.month, start_day.day, tzinfo=TZ)
            win_end = datetime(end_day.year, end_day.month, end_day.day, tzinfo=TZ) + timedelta(days=1)
            busy = freebusy(cal, ids, win_start, win_end)
            slots = free_slots(busy, start_day, end_day, plan["duration_min"], now)
            trace["freebusy"].append({
                "window": attempt, "from": str(start_day), "to": str(end_day),
                "busy": {c: [f"{fmt(s)} - {e:%-I:%M %p}" for s, e in b] for c, b in busy.items()},
                "shared_free_slots": len(slots)})
            if slots:
                break
            print(f"No shared time {start_day} to {end_day}; looking at next week.")
            start_day = end_day + timedelta(days=7 - end_day.weekday())  # next Monday
            end_day = start_day + timedelta(days=6)
        offers = pick_spread(slots)
        trace["candidate_slots"] = [fmt(s) for s in offers]
        if not offers:
            raise RuntimeError("No shared free time found.")

        print(f"Shared free times for {' + '.join(p['name'] for p in people)}:")
        for i, s in enumerate(offers, 1):
            print(f"  {i}. {fmt(s)} ({plan['duration_min']} min)")
        n = args.pick or int(input(f"Pick 1-{len(offers)}: "))
        choice = offers[n - 1]
        trace["pick"] = {"number": n, "slot": fmt(choice), "start": choice.isoformat()}

        created = book(cal, people, choice, plan["duration_min"], plan["activity"])
        trace["created_events"] = created
        print(f"Booked {fmt(choice)} on both calendars:")
        for c in created:
            print(f"  {c['person']}: {c['event_id']} {c['link']}")
        trace["status"] = "booked"
    except Exception as e:
        trace["status"] = "failed"
        trace["error"] = f"{type(e).__name__}: {e}"
        raise
    finally:
        TRACES.mkdir(exist_ok=True)
        path = TRACES / f"{now:%Y-%m-%dT%H-%M-%S}.json"
        path.write_text(json.dumps(trace, indent=2, ensure_ascii=False) + "\n")
        print(f"Trace: {path.relative_to(TRACES.parent)}")


if __name__ == "__main__":
    main()
