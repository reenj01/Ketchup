"""Slot checks (no network): python tests/test_slots.py

1. Hard rule: every offered slot is >= 30 min from every event on both calendars.
2. Windows have no gaps: 11:30 AM is morning, 12:00 PM noon, 4:30 PM noon, 5:00 PM night,
   and the last 30-min start is 8:30 PM.
3. Soft preference: both say noon, noon is fully blocked -> same-day morning/night
   fallbacks are offered (not next week), marked "nothing worked at noon".
"""
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "brain"))
from calendar_slots import BUFFER, candidates, fallback_note, free_slots, window_of  # noqa: E402
from parse_plan import TZ  # noqa: E402

D = date(2026, 10, 29)
T = lambda h, m=0: datetime(2026, 10, 29, h, m, tzinfo=TZ)  # noqa: E731
now = datetime(2026, 10, 28, 12, tzinfo=TZ)
fmt = lambda t: t.strftime("%-I:%M %p")  # noqa: E731


def check_buffer(busy, slots):
    blocks = [b for bl in busy.values() for b in bl]
    for t in slots:
        end = t + timedelta(minutes=30)
        for s, e in blocks:
            assert end + BUFFER <= s or e + BUFFER <= t, f"{fmt(t)} too close to {fmt(s)}"


# ---- 1. hard 30 min buffer ----------------------------------------------------
busy = {"mayo": [(T(20), T(21))],           # party 8-9 PM
        "frankie": [(T(12), T(13))]}        # lunch 12-1 PM
slots = free_slots(busy, D, D, 30, now)
assert T(19, 30) not in slots, "7:30 PM ends right at the 8 PM event: must be rejected"
assert T(19, 0) in slots, "7:00-7:30 PM leaves exactly 30 min before 8 PM: allowed"
assert T(11, 30) not in slots and T(13, 0) not in slots, "too close to the 12-1 PM lunch"
assert T(11, 0) in slots and T(13, 30) in slots, "exactly 30 min clear of lunch: allowed"
check_buffer(busy, slots)
print("1. Buffer. Thu Oct 29 busy: Mayo 8-9 PM, Frankie 12-1 PM; buffer", BUFFER)
print("   7:30 PM rejected:", T(19, 30) not in slots, "| 7:00 PM allowed:", T(19, 0) in slots)
print("   Offered 30-min starts:", ", ".join(fmt(t) for t in slots))

# ---- 2. windows, no gaps --------------------------------------------------------
open_day = free_slots({}, D, D, 30, now)
assert [window_of(T(*x)) for x in [(8,), (11, 30), (12,), (16, 30), (17,), (20, 30)]] == \
    ["morning", "morning", "noon", "noon", "night", "night"]
assert open_day[0] == T(8) and open_day[-1] == T(20, 30) and all(window_of(t) for t in open_day)
print("2. Windows: 8:00-11:30 AM starts morning, 12:00-4:30 PM noon, 5:00-8:30 PM night; "
      f"{len(open_day)} starts, last {fmt(open_day[-1])}")

# ---- 3. noon blocked -> same-day fallback -------------------------------------------
busy = {"mayo": [(T(12), T(14))],           # 12-2 PM
        "frankie": [(T(14, 30), T(17))]}    # 2:30-5 PM
slots = free_slots(busy, D, D, 30, now)
check_buffer(busy, slots)
assert not [t for t in slots if window_of(t) == "noon"], "noon must be fully blocked"
c = candidates(slots, 30, ["noon", "noon"])
assert c, "must fall back to another window the same day, not next week"
assert {w for _, w in c} == {"morning", "night"} and all(t.date() == D for t, _ in c)
# nearest to noon first: 5:30 PM is 31 min after noon ends, 11:00 AM is 1 h before it starts
assert c[0] == (T(17, 30), "night") and c[1] == (T(11), "morning"), c
notes = [fallback_note(t, w, "noon", "noon", slots) for t, w in c]
assert all(n == "nothing worked at noon on Thursday" for n in notes), notes
print("3. Fallback. Both said noon; Mayo busy 12-2 PM, Frankie 2:30-5 PM -> no shared noon slot")
print("   Candidates:", [(fmt(t), w, n) for (t, w), n in zip(c, notes)])

# noon open again -> noon ranks first, other windows stay as next-best options
c2 = candidates(free_slots({}, D, D, 30, now), 30, ["noon", "noon"])
assert c2[0][1] == "noon" and fallback_note(c2[0][0], "noon", "noon", "noon", []) is None
# morning vs night -> what each person said beats the middle: noon ranks below both
mn = [w for _, w in candidates(open_day, 30, ["morning", "night"])]
assert set(mn[:2]) == {"morning", "night"} and mn.index("noon") == 2, mn
# one person said night, the other timed out (habit default morning): night first
d = [w for _, w in candidates(open_day, 30, ["night", "morning"], said=["night"])]
assert d[0] == "night", d
# excluded start times are never offered again
first = candidates(open_day, 30, ["night"])[0][0]
assert first not in [t for t, _ in candidates(open_day, 30, ["night"], exclude=[first])]
print("   With noon free: first option", fmt(c2[0][0]), c2[0][1],
      "| morning vs night:", mn, "| said night, habit morning:", d[0])
print("OK: buffer hard rule holds, windows have no gaps, blocked noon falls back the same day")
