"""Find shared free slots from Google Calendar free/busy.

Hard rule: a slot is only offered if it is at least `buffer_min` (30) minutes
away from every event on either calendar, before and after, and starts and
ends inside the day (8 AM-9 PM ET).

Soft preference: morning / noon / night only rank slots. A slot belongs to
the window its START falls in (morning 8:00-11:59, noon 12:00-4:59,
night 5:00-9:00), so the windows have no gaps.
"""
import json
from datetime import date, datetime, time, timedelta
from pathlib import Path

from parse_plan import TZ

SETTINGS = json.loads((Path(__file__).parent.parent / "config/settings.json").read_text())
BUFFER = timedelta(minutes=SETTINGS["buffer_min"])
WINDOWS = {k: (time.fromisoformat(a), time.fromisoformat(b))
           for k, (a, b) in SETTINGS["windows"].items()}  # morning / noon / night
ORDER = list(WINDOWS)
DAY_START, DAY_END = (time.fromisoformat(x) for x in SETTINGS["day"])
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


def window_of(t: datetime):
    """'morning' / 'noon' / 'night': the window the slot starts in."""
    for name, (a, b) in WINDOWS.items():
        if a <= t.time() <= b:
            return name
    return None


def wanted_windows(choices):
    """The windows people asked for plus any in between (morning + night ->
    noon is the middle ground). No answers: all windows."""
    if not choices:
        return list(ORDER)
    idx = sorted(ORDER.index(c) for c in choices)
    return ORDER[idx[0]: idx[-1] + 1]


def fit(window, choices):
    """How far a window is from what people asked, as (worst person, total).
    0 = exactly what was asked. Morning vs night: noon is (1, 2), morning (2, 2)."""
    d = [abs(ORDER.index(window) - ORDER.index(c)) for c in choices] or [0]
    return (max(d), sum(d))


def _span(day, names):
    return at(day, WINDOWS[names[0]][0]), at(day, WINDOWS[names[-1]][1])


def candidates(slots, minutes, choices=()):
    """Soft preferences. Every shared slot is eligible; one per (day, window):
    in a wanted window, the slot nearest its middle; in any other window
    (a fallback), the slot nearest the wanted windows. Ranked by fit to each
    person's answer, then day, then nearness. Returns [(start, window)], best first."""
    wanted = wanted_windows(choices)
    best = {}
    for t in slots:
        name = window_of(t)
        a, b = WINDOWS[name]
        if name in wanted:
            mid = at(t.date(), a) + (at(t.date(), b) - at(t.date(), a)) / 2
            off = abs(t + timedelta(minutes=minutes) / 2 - mid)
        else:
            lo, hi = _span(t.date(), wanted)
            off = (lo - t) if t < lo else (t - hi)
        key = (t.date(), name)
        if key not in best or off < best[key][0]:
            best[key] = (off, t)
    # Rank: fit to the answers, then day, then (for fallbacks) nearest to the
    # wanted windows, then time.
    ranked = sorted(best.items(), key=lambda kv: (
        fit(kv[0][1], choices), kv[0][0],
        kv[1][0] if kv[0][1] not in wanted else timedelta(0), kv[1][1]))
    return [(t, name) for (_, name), (_, t) in ranked][:SETTINGS["max_candidates"]]


def fallback_note(t, window, choice, other_choice, slots):
    """Why a slot outside `choice` is offered, or None if it is in it (or the
    person had no preference). 'nothing worked at noon on Thursday' when no
    shared slot that day was in the asked window; 'middle ground' when the
    other person asked for something else; else 'another option'."""
    if not choice or window == choice:
        return None
    if not any(window_of(s) == choice and s.date() == t.date() for s in slots):
        return f"nothing worked at {choice} on {t:%A}"
    if other_choice and window == other_choice:
        return "the other person's pick"
    if other_choice and other_choice != choice:
        return "middle ground"
    return f"another option, not {choice}"
