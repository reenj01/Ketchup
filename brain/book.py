"""Put the agreed meeting on everyone's calendar.

A service account can't send invites without domain-wide delegation, so the
same event is inserted into each person's calendar directly.
"""
from datetime import datetime, timedelta

from timing import timed


def book(cal, people, start: datetime, minutes: int, activity: str):
    names = " + ".join(p["name"] for p in people)
    event = {
        "summary": f"Ketchup: {names} {activity}",
        "description": f"Booked by Ketchup. {names} said they'd {activity}, "
                       "tapped their pebbles, and their agents picked this time.",
        "start": {"dateTime": start.isoformat(), "timeZone": "America/New_York"},
        "end": {"dateTime": (start + timedelta(minutes=minutes)).isoformat(),
                "timeZone": "America/New_York"},
    }
    created = []
    for p in people:
        with timed("google", "events.insert"):
            ev = cal.events().insert(calendarId=p["calendar_id"], body=event).execute()
        created.append({"person": p["name"], "calendar_id": p["calendar_id"],
                        "event_id": ev["id"], "link": ev.get("htmlLink")})
    return created
