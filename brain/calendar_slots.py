"""Find shared free slots for everyone from Google Calendar free/busy."""
from datetime import date, datetime, timedelta

from parse_plan import TZ

DAY_START, DAY_END = 9, 21  # waking hours, ET
STEP = timedelta(minutes=30)


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


def free_slots(busy, start_day: date, end_day: date, minutes: int, now: datetime):
    """Every slot on a 30-min grid inside waking hours where nobody is busy."""
    length = timedelta(minutes=minutes)
    all_busy = [b for blocks in busy.values() for b in blocks]
    slots, day = [], start_day
    while day <= end_day:
        t = datetime(day.year, day.month, day.day, DAY_START, tzinfo=TZ)
        stop = datetime(day.year, day.month, day.day, DAY_END, tzinfo=TZ)
        while t + length <= stop:
            if t >= now and not any(t < e and s < t + length for s, e in all_busy):
                slots.append(t)
            t += STEP
        day += timedelta(days=1)
    return slots


def pick_spread(slots, n=3):
    """Pick n slots spread evenly across all shared free time (earliest, middle, latest)."""
    if len(slots) <= n:
        return list(slots)
    return [slots[round(i * (len(slots) - 1) / (n - 1))] for i in range(n)]
