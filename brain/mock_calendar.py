"""A stand-in for the Google Calendar client, for the web simulator.

Same calls the brain makes on the real client (freebusy().query, events().list,
insert, get, patch, delete, each with .execute()), so flow.py, infer_prefs.py,
book.py and holds.py run unchanged. Busy events come from the seeded weekly
patterns in config/mock_calendar.json, relative to today. Holds the agents
make are kept in memory here, never sent to Google.

Ketchup's own events are left out of freebusy and events.list, so every
session starts from the same seeded calendars (judges trying it one after
another get the same results); `created()` lists them for the page.
"""
import copy
import json
import threading
from datetime import date, datetime, time, timedelta
from pathlib import Path

from parse_plan import TZ

FIXTURE = json.loads((Path(__file__).parent.parent / "config/mock_calendar.json").read_text())
PEOPLE = json.loads((Path(__file__).parent.parent / "config/people.json").read_text())
KEY_OF = {p["calendar_id"]: k for k, p in PEOPLE.items()}
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
PAST, AHEAD = timedelta(weeks=4), timedelta(weeks=9)

_lock = threading.Lock()
_made = {}  # calendar_id -> {event_id: event}  (Ketchup's holds, in memory only)


def seeded(calendar_id, start: datetime, end: datetime, today: date | None = None):
    """[(start, end, title)] of the seeded events overlapping [start, end)."""
    key = KEY_OF.get(calendar_id)
    today = today or datetime.now(TZ).date()
    out = []
    day = max(start.astimezone(TZ).date(), today - PAST)
    last = min(end.astimezone(TZ).date(), today + AHEAD)
    while day <= last:
        for ev in FIXTURE["weekly"].get(key, []):
            if DAYS[day.weekday()] in ev["days"]:
                s = datetime.combine(day, time.fromisoformat(ev["start"]), tzinfo=TZ)
                e = datetime.combine(day, time.fromisoformat(ev["end"]), tzinfo=TZ)
                if s < end and e > start:
                    out.append((s, e, ev["title"]))
        day += timedelta(days=1)
    return sorted(out)


def created(calendar_id):
    with _lock:
        return copy.deepcopy(list(_made.get(calendar_id, {}).values()))


class _Call:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class _FreeBusy:
    def query(self, body):
        def run():
            s, e = datetime.fromisoformat(body["timeMin"]), datetime.fromisoformat(body["timeMax"])
            return {"calendars": {i["id"]: {"busy": [{"start": a.isoformat(), "end": b.isoformat()}
                                                    for a, b, _ in seeded(i["id"], s, e)]}
                                  for i in body["items"]}}
        return _Call(run)


class _Events:
    def list(self, calendarId, timeMin, timeMax, **_):
        s, e = datetime.fromisoformat(timeMin), datetime.fromisoformat(timeMax)
        return _Call(lambda: {"items": [
            {"id": f"mock{i}", "summary": t, "start": {"dateTime": a.isoformat()},
             "end": {"dateTime": b.isoformat()}} for i, (a, b, t) in enumerate(seeded(calendarId, s, e))]})

    def insert(self, calendarId, body):
        def run():
            ev = dict(copy.deepcopy(body), htmlLink=None)
            ev.setdefault("id", f"ketchupmock{len(_made.get(calendarId, {}))}")
            with _lock:
                _made.setdefault(calendarId, {})[ev["id"]] = ev
            return copy.deepcopy(ev)
        return _Call(run)

    def get(self, calendarId, eventId):
        def run():
            with _lock:
                return copy.deepcopy(_made[calendarId][eventId])
        return _Call(run)

    def patch(self, calendarId, eventId, body):
        def run():
            with _lock:
                ev = _made.setdefault(calendarId, {}).setdefault(eventId, {"id": eventId})
                ev.update(copy.deepcopy(body))
                return copy.deepcopy(ev)
        return _Call(run)

    def delete(self, calendarId, eventId):
        def run():
            with _lock:
                _made.get(calendarId, {}).pop(eventId, None)
            return {}
        return _Call(run)


class MockCalendar:
    def freebusy(self):
        return _FreeBusy()

    def events(self):
        return _Events()
