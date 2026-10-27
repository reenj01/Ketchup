"""Infer a person's meeting preferences from their own calendar with Muse Spark Open.

Nothing is hand-written: we hand the model plain facts about the person's
timed events (when they happen) and it infers when they
like to meet (time of day). The 30-min buffer is a fixed rule elsewhere.

    python brain/infer_prefs.py mayo
"""
import json
import sys
from collections import Counter
from datetime import datetime, timedelta

from gcal import PEOPLE, calendar
from parse_plan import TZ, ask_json

PAST, FUTURE = timedelta(weeks=4), timedelta(weeks=2)

SYSTEM = """You study one person's calendar and infer how they like to schedule
meetings. You get facts only: their timed events and simple counts.
Reply with strict JSON only, no prose, no code fences:
{"time_of_day_preference": "morning" | "afternoon" | "evening" | "no clear pattern",
 "preferred_hours": "e.g. 9 AM-12 PM, or \\"none\\" if unclear",
 "confidence": "low" | "medium" | "high",
 "evidence": ["short strings citing actual events from the data"],
 "summary": "one sentence"}
How to read it:
- If most of their events fall in one part of the day, they are probably a
  person for that part of the day and would prefer meeting then.
- Base everything on the data given. If there are only a few events, say
  so: use "no clear pattern" and/or confidence "low".
  Never invent events."""


def calendar_facts(cal, calendar_id, now):
    """Compact factual summary of the person's timed events."""
    items = cal.events().list(calendarId=calendar_id, timeMin=(now - PAST).isoformat(),
                              timeMax=(now + FUTURE).isoformat(), singleEvents=True,
                              orderBy="startTime", maxResults=250).execute().get("items", [])
    events = []
    for e in items:
        if "dateTime" not in e.get("start", {}):  # skip all-day events
            continue
        s = datetime.fromisoformat(e["start"]["dateTime"]).astimezone(TZ)
        t = datetime.fromisoformat(e["end"]["dateTime"]).astimezone(TZ)
        events.append((s, t, e.get("summary", "(no title)")))
    part = lambda h: "morning (before 12 PM)" if h < 12 else "afternoon (12-5 PM)" if h < 17 else "evening (after 5 PM)"
    by_part = Counter(part(s.hour) for s, _, _ in events)
    lines = [f"Window: {(now - PAST):%a %b %-d} to {(now + FUTURE):%a %b %-d} (ET). "
             f"{len(events)} timed events."]
    lines += [f"- {s:%a %b %-d} {s:%-I:%M %p}-{t:%-I:%M %p}: {title}" for s, t, title in events]
    lines.append("Events by time of day: " + (", ".join(f"{k}: {v}" for k, v in by_part.items()) or "none"))
    return "\n".join(lines)


def _check(r):
    if r.get("time_of_day_preference") not in ("morning", "afternoon", "evening", "no clear pattern"):
        raise ValueError("bad time_of_day_preference")
    if r.get("confidence") not in ("low", "medium", "high"):
        raise ValueError("bad confidence")
    if not isinstance(r.get("evidence"), list) or not isinstance(r.get("summary"), str):
        raise ValueError("missing evidence/summary")


def infer_prefs(cal, person, now=None):
    """Returns {"person", "input_summary", "model", "model_errors", "reasoning_content", "profile"}."""
    now = now or datetime.now(TZ)
    facts = calendar_facts(cal, person["calendar_id"], now)
    out = ask_json(SYSTEM, f"Today is {now:%A %Y-%m-%d}.\n{facts}", check=_check)
    return {"person": person["name"], "input_summary": facts, "model": out["model"],
            "model_errors": out["errors"], "reasoning_content": out["reasoning"],
            "profile": out["reply"]}


if __name__ == "__main__":
    cal = calendar()
    for key in sys.argv[1:] or list(PEOPLE):
        r = infer_prefs(cal, PEOPLE[key])
        print(f"{r['person']} ({r['model']}):\n{json.dumps(r['profile'], indent=2)}\n")
