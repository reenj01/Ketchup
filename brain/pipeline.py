"""Ketchup end to end: phrase -> plan -> shared slots -> agents negotiate -> booked.

Used by the CLI (demo.py) and the tap server. Every run saves a trace to traces/.
"""
import json
from datetime import date, datetime, timedelta
from pathlib import Path

from agents import fmt, negotiate
from book import book
from calendar_slots import freebusy, free_slots, pick_spread
from gcal import PEOPLE, calendar
from infer_prefs import infer_prefs
from parse_plan import TZ, parse_plan

TRACES = Path(__file__).parent.parent / "traces"


def run(phrase, proposer="mayo", pick=None, dry_run=False, log=print, source="cli"):
    """Returns the trace dict. `pick` (1-3) skips the agents (manual override)."""
    now = datetime.now(TZ)
    other = next(k for k in PEOPLE if k != proposer)
    people = [PEOPLE[proposer], PEOPLE[other]]
    trace = {"started_at": now.isoformat(), "source": source, "input_phrase": phrase,
             "proposer": PEOPLE[proposer]["name"], "dry_run": dry_run, "people": people}
    try:
        log(f'Heard: "{phrase}" (said by {PEOPLE[proposer]["name"]})')
        parsed = parse_plan(phrase, now)
        plan = parsed["plan"]
        trace.update(model=parsed["model"], model_reasoning=parsed["reasoning"],
                     model_errors=parsed["errors"], parsed_plan=plan)
        log(f"Plan ({parsed['model']}): {json.dumps(plan)}")

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
            log(f"No shared time {start_day} to {end_day}; looking at next week.")
            start_day = end_day + timedelta(days=7 - end_day.weekday())  # next Monday
            end_day = start_day + timedelta(days=6)
        offers = pick_spread(slots)
        trace["candidate_slots"] = [fmt(s) for s in offers]
        if not offers:
            raise RuntimeError("No shared free time this week or next.")

        log(f"Shared free times for {' + '.join(p['name'] for p in people)}:")
        for i, s in enumerate(offers, 1):
            log(f"  {i}. {fmt(s)} ({plan['duration_min']} min)")

        if pick:
            choice = offers[pick - 1]
            trace["agreement"] = {"how": f"manual override --pick {pick}"}
            log(f"Manual pick: slot {pick}")
        else:
            log("Each agent reads its own person's calendar habits:")
            trace["inferred_profiles"] = [infer_prefs(cal, p, now) for p in people]
            profiles = {}
            for inf in trace["inferred_profiles"]:
                pr = profiles[inf["person"]] = inf["profile"]
                log(f"  {inf['person']}: {pr['time_of_day_preference']}, buffer "
                    f"{pr['min_buffer_min']} min, confidence {pr['confidence']}: {pr['summary']}")
            log("Agents negotiating:")
            result = negotiate(people[0], people[1], plan["activity"], offers, busy,
                               plan["duration_min"], profiles, log=log)
            choice = offers[result["agreed_index"]]
            trace["agent_turns"] = result.pop("turns")
            trace["agreement"] = result
        trace["agreed_slot"] = {"slot": fmt(choice), "start": choice.isoformat(),
                                "how": trace["agreement"]["how"]}
        log(f"Agreed: {fmt(choice)} ({trace['agreement']['how']})")

        if dry_run:
            trace["status"] = "agreed (dry run, not booked)"
            log("Dry run: not booking.")
        else:
            created = book(cal, people, choice, plan["duration_min"], plan["activity"])
            trace["created_events"] = created
            log(f"Booked {fmt(choice)} on both calendars:")
            for c in created:
                log(f"  {c['person']}: {c['event_id']} {c['link']}")
            trace["status"] = "booked"
    except Exception as e:
        trace["status"] = "failed"
        trace["error"] = f"{type(e).__name__}: {e}"
        raise
    finally:
        TRACES.mkdir(exist_ok=True)
        path = TRACES / f"{now:%Y-%m-%dT%H-%M-%S}-{source}.json"
        path.write_text(json.dumps(trace, indent=2, ensure_ascii=False, default=str) + "\n")
        trace["trace_file"] = str(path.relative_to(TRACES.parent))
        log(f"Trace: {trace['trace_file']}")
    return trace
