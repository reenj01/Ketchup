"""Mock calendar checks (no network): python tests/test_mock_calendar.py

1. KETCHUP_CALENDAR=mock gives the mock, whatever Google keys are set.
2. freebusy and events.list return the seeded weekly events, relative to today,
   and the slot finder sees them (no offered slot within 30 min of one).
3. Holds (insert/patch/delete) stay in memory and never show up in freebusy,
   so every simulator session starts from the same calendars.
"""
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

os.environ["KETCHUP_CALENDAR"] = "mock"
sys.path.insert(0, str(Path(__file__).parent.parent / "brain"))
import mock_calendar  # noqa: E402
from gcal import PEOPLE, calendar  # noqa: E402
from parse_plan import TZ  # noqa: E402

cal = calendar()
assert isinstance(cal, mock_calendar.MockCalendar), type(cal)
ids = [p["calendar_id"] for p in PEOPLE.values()]
now = datetime.now(TZ).replace(minute=0, second=0, microsecond=0)
week = now + timedelta(days=7)
fb = cal.freebusy().query(body={"timeMin": now.isoformat(), "timeMax": week.isoformat(),
                                "timeZone": "America/New_York", "items": [{"id": i} for i in ids]}).execute()
for i in ids:
    busy = fb["calendars"][i]["busy"]
    assert len(busy) >= 8, (i, len(busy))
    for b in busy:
        s, e = datetime.fromisoformat(b["start"]), datetime.fromisoformat(b["end"])
        assert e > now and s < week and s < e, b
past = cal.events().list(calendarId=ids[0], timeMin=(now - timedelta(days=28)).isoformat(),
                         timeMax=now.isoformat(), singleEvents=True, orderBy="startTime").execute()["items"]
assert len(past) > 30 and all(e["summary"] and e["start"]["dateTime"] for e in past), len(past)
mornings = sum(datetime.fromisoformat(e["start"]["dateTime"]).hour < 12 for e in past)
assert mornings / len(past) > 0.6, "Mayo's seeded events are mostly mornings"
print(f"ok: seeded busy per week {[len(fb['calendars'][i]['busy']) for i in ids]}, Mayo past events {len(past)}")

from calendar_slots import BUFFER, at, free_slots, freebusy  # noqa: E402
day = (now + timedelta(days=1)).date()
from datetime import time  # noqa: E402
busy = freebusy(cal, ids, at(day, time(0)), at(day + timedelta(days=1), time(0)))
slots = free_slots(busy, day, day, 30, now)
blocks = [b for bl in busy.values() for b in bl]
assert blocks, "seeded events tomorrow"
for t in slots:
    for s, e in blocks:
        assert t + timedelta(minutes=30) + BUFFER <= s or t >= e + BUFFER, (t, s, e)
print(f"ok: {len(slots)} free slots tomorrow, all 30 min clear of seeded events")

ev = cal.events().insert(calendarId=ids[0], body={
    "id": "ketchuptest0", "summary": "test", "status": "tentative",
    "start": {"dateTime": (now + timedelta(days=1)).isoformat()},
    "end": {"dateTime": (now + timedelta(days=1, minutes=30)).isoformat()}}).execute()
assert ev["status"] == "tentative"
assert cal.events().patch(calendarId=ids[0], eventId="ketchuptest0", body={"status": "confirmed"}).execute()["status"] == "confirmed"
assert [e["id"] for e in mock_calendar.created(ids[0])] == ["ketchuptest0"]
fb2 = cal.freebusy().query(body={"timeMin": now.isoformat(), "timeMax": week.isoformat(),
                                 "items": [{"id": ids[0]}]}).execute()
assert fb2["calendars"][ids[0]]["busy"] == fb["calendars"][ids[0]]["busy"], "holds stay out of freebusy"
cal.events().delete(calendarId=ids[0], eventId="ketchuptest0").execute()
assert mock_calendar.created(ids[0]) == []
print("ok: holds kept in memory only, left out of freebusy, deleted cleanly")
