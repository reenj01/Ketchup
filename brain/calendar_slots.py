"""Find shared free slots from Google Calendar free/busy.

Hard rule: a slot is only offered if it is at least `buffer_min` (30) minutes
away from every event on either calendar, before and after.
"""
import json
from datetime import date, datetime, time, timedelta
from pathlib import Path

from parse_plan import TZ

SETTINGS = json.loads((Path(__file__).parent.parent / "config/settings.json").read_text())
BUFFER = timedelta(minutes=SETTINGS["buffer_min"])
WINDOWS = {k: (time.fromisoformat(a), time.fromisoformat(b))
           for k, (a, b) in SETTINGS["windows"].items()}  # morning / noon / night
DAY_START = min(a for a, _ in WINDOWS.values())
DAY_END = max(b for _, b in WINDOWS.values())
STEP = timedelta(minutes=30)


def at(day: date, t: time) -> datetime:
    return datetime.combine(day, t, tzinfo=TZ)


def freebusy(cal, calendar_ids, start, end):
    """Returns {calendar_id: [(busy_start, busy_end), ...]} in ET."""
    resp = cal.freebusy().query(body={
        "timeMin": start.isoformat(), "timeMax": end.isoformat(),
        "timeZone": "America/New_York",
        "items": [{"id": c} for c in calendar_ids],
    }).execute()
    out = {}
    for cid, data in resp["calendars"].items():
        if data.get("errors"):
            raise RuntimeError(f"Can't read calendar {cid}: {data['errors']}")
        out[cid] = [(datetime.fromisoformat(b["start"]).astimezone(TZ),
                     datetime.fromisoformat(b["end"]).astimezone(TZ))
                    for b in data["busy"]]
    return out


def clear_of(t: datetime, length: timedelta, blocks) -> bool:
    """True if [t, t+length] is at least BUFFER away from every busy block."""
    return not any(s - BUFFER < t + length and t < e + BUFFER for s, e in blocks)


def free_slots(busy, start_day: date, end_day: date, minutes: int, now: datetime):
    """Every slot on a 30-min grid, 8 AM-9 PM ET, clear of everyone's events by BUFFER."""
    length = timedelta(minutes=minutes)
    blocks = [b for bl in busy.values() for b in bl]
    slots, day = [], start_day
    while day <= end_day:
        t, stop = at(day, DAY_START), at(day, DAY_END)
        while t + length <= stop:
            if t >= now and clear_of(t, length, blocks):
                slots.append(t)
            t += STEP
        day += timedelta(days=1)
    return slots


def free_windows(blocks, start_day, end_day, now):
    """One person's free time (already BUFFER away from their events) as merged
    (start, end) intervals. This is all one agent shares with the other: no titles."""
    out = []
    for t in free_slots({"me": blocks}, start_day, end_day, 30, now):
        if out and out[-1][1] == t and out[-1][1].date() == t.date():
            out[-1] = (out[-1][0], t + STEP)
        else:
            out.append((t, t + STEP))
    return out


def window_of(t: datetime, minutes: int):
    """'morning' / 'noon' / 'night' if the whole slot fits in that window, else None."""
    end = t + timedelta(minutes=minutes)
    for name, (a, b) in WINDOWS.items():
        if at(t.date(), a) <= t and end <= at(t.date(), b):
            return name
    return None


def candidates(slots, minutes, wanted):
    """For each day and wanted window, the shared slot nearest the window's middle.
    Returns [(start, window_name)] in time order, at most max_candidates."""
    best = {}
    for t in slots:
        name = window_of(t, minutes)
        if name not in wanted:
            continue
        a, b = WINDOWS[name]
        mid = at(t.date(), a) + (at(t.date(), b) - at(t.date(), a)) / 2
        off = abs(t + timedelta(minutes=minutes) / 2 - mid)
        key = (t.date(), name)
        if key not in best or off < best[key][0]:
            best[key] = (off, t)
    out = sorted((t, name) for (_, name), (_, t) in best.items())
    n = SETTINGS["max_candidates"]
    if len(out) > n:  # spread evenly over the range
        out = [out[round(i * (len(out) - 1) / (n - 1))] for i in range(n)]
    return out
