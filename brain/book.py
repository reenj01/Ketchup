"""Put the agreed meeting on everyone's calendar.

A service account can't send invites without domain-wide delegation, so the
same event is inserted into each person's calendar directly.

Holds: `tentative=True` inserts it with Google Calendar's event status
"tentative" and " (tentative)" at the end of the title (holds.py confirms it
later). Event ids can be chosen by us (base32hex), so a retried insert of the
same id is a no-op instead of a duplicate.
"""
from datetime import datetime, timedelta

from googleapiclient.errors import HttpError

from timing import timed

MARK = " (tentative)"


def title(people, activity):
    return f"Ketchup: {' + '.join(p['name'] for p in people)} {activity}"


def body(people, start: datetime, minutes: int, activity: str, tentative=False):
    names = " + ".join(p["name"] for p in people)
    return {
        "summary": title(people, activity) + (MARK if tentative else ""),
        "status": "tentative" if tentative else "confirmed",
        "description": f"Booked by Ketchup. {names} said they'd {activity}, "
                       "tapped their pebbles, and their agents picked this time."
                       + (" Tentative until someone keeps it or the day ends." if tentative else ""),
        "start": {"dateTime": start.isoformat(), "timeZone": "America/New_York"},
        "end": {"dateTime": (start + timedelta(minutes=minutes)).isoformat(),
                "timeZone": "America/New_York"},
    }


def book(cal, people, start: datetime, minutes: int, activity: str, tentative=False, ids=None):
    """Insert the event on each calendar. `ids` = our own event id per person
    (makes the insert idempotent)."""
    event = body(people, start, minutes, activity, tentative)
    created = []
    for i, p in enumerate(people):
        b = dict(event, **({"id": ids[i]} if ids else {}))
        with timed("google", "events.insert"):
            try:
                ev = cal.events().insert(calendarId=p["calendar_id"], body=b).execute()
            except HttpError as e:
                if not (ids and e.resp.status == 409):  # 409 = already inserted on an earlier try
                    raise
                ev = cal.events().get(calendarId=p["calendar_id"], eventId=ids[i]).execute()
        created.append({"person": p["name"], "calendar_id": p["calendar_id"],
                        "event_id": ev["id"], "link": ev.get("htmlLink"), "status": ev.get("status")})
    return created
