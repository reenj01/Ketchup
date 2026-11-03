"""The simulator's calendar endpoint (no network): python tests/test_sim_calendar.py

GET /sim/api/calendar shows each person the same seeded events the agents read
(mock freebusy), plus this session's Ketchup hold, and the hold sits on a free
gap at least buffer_min away from every event on both calendars.
"""
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

os.environ["KETCHUP_CALENDAR"] = "mock"
os.environ["KETCHUP_TTS"] = "off"
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "brain"))
import sim  # noqa: E402
from calendar_slots import free_slots  # noqa: E402
from gcal import PEOPLE, calendar  # noqa: E402
from parse_plan import TZ  # noqa: E402

W = sim.week(None)
assert len(W["days"]) == 7 and set(W["people"]) == set(PEOPLE), W.keys()
assert W["buffer_min"] == 30 and "window" not in W
now = datetime.now(TZ)
start = datetime.combine(now.date(), datetime.min.time(), tzinfo=TZ)
fb = calendar().freebusy().query(body={"timeMin": start.isoformat(), "timeMax": (start + timedelta(days=7)).isoformat(),
                                       "items": [{"id": p["calendar_id"]} for p in PEOPLE.values()]}).execute()
for k, p in PEOPLE.items():
    shown = [(b["start"], b["end"]) for b in W["people"][k]["busy"]]
    agents = [(b["start"], b["end"]) for b in fb["calendars"][p["calendar_id"]]["busy"]]
    assert shown == agents and shown, (k, len(shown), len(agents))
    assert all(b["title"] for b in W["people"][k]["busy"]) and W["people"][k]["ketchup"] == []

# a session with a hold on the first shared free slot: shown on both, clear of every event
busy = {p["calendar_id"]: [(datetime.fromisoformat(b["start"]), datetime.fromisoformat(b["end"]))
                           for b in fb["calendars"][p["calendar_id"]]["busy"]] for p in PEOPLE.values()}
slot = free_slots(busy, now.date(), (now + timedelta(days=6)).date(), 30, now)[0]
t = slot[0] if isinstance(slot, tuple) else slot


class FakeSession:
    start_day, end_day = now.date(), (now + timedelta(days=6)).date()
    hold = {"start": t.isoformat(), "minutes": 30, "title": "Ketchup", "status": "confirmed"}


class FakeSim:
    s = FakeSession()


W = sim.week(FakeSim())
assert W["window"] == [now.date().isoformat(), (now + timedelta(days=6)).date().isoformat()]
gap = timedelta(minutes=W["buffer_min"])
for k in PEOPLE:
    (h,) = W["people"][k]["ketchup"]
    a = datetime.fromisoformat(h["start"]); b = a + timedelta(minutes=h["minutes"])
    assert h["status"] == "confirmed"
    for e in W["people"][k]["busy"]:
        s, f = datetime.fromisoformat(e["start"]), datetime.fromisoformat(e["end"])
        assert not (s - gap < b and a < f + gap), (k, e, h)
print(f"OK sim calendar: {sum(len(W['people'][k]['busy']) for k in PEOPLE)} events shown = what the agents read; "
      f"hold {t:%a %-I:%M %p} clear on both")
