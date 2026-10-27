"""Slot filter checks (no network): python tests/test_slots.py"""
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "brain"))
from calendar_slots import BUFFER, candidates, free_slots, WINDOWS  # noqa: E402
from parse_plan import TZ  # noqa: E402

D = date(2026, 10, 29)
T = lambda h, m=0: datetime(2026, 10, 29, h, m, tzinfo=TZ)  # noqa: E731
busy = {"mayo": [(T(20), T(21))],           # party 8-9 PM
        "frankie": [(T(12), T(13))]}        # lunch 12-1 PM
now = datetime(2026, 10, 28, 12, tzinfo=TZ)
slots = free_slots(busy, D, D, 30, now)
fmt = lambda t: t.strftime("%-I:%M %p")  # noqa: E731

assert T(19, 30) not in slots, "7:30 PM ends right at the 8 PM event: must be rejected"
assert T(19, 0) in slots, "7:00-7:30 PM leaves exactly 30 min before 8 PM: allowed"
assert T(11, 30) not in slots and T(13, 0) not in slots, "too close to the 12-1 PM lunch"
assert T(11, 0) in slots and T(13, 30) in slots, "exactly 30 min clear of lunch: allowed"
all_busy = [b for bl in busy.values() for b in bl]
for t in slots:  # every offered slot keeps 30 min on both sides of every event
    end = t + timedelta(minutes=30)
    for s, e in all_busy:
        assert end + BUFFER <= s or e + BUFFER <= t, f"{fmt(t)} too close to {fmt(s)}"
print("Thu Oct 29 busy: Mayo 8-9 PM, Frankie 12-1 PM; buffer", BUFFER)
print("7:30 PM rejected:", T(19, 30) not in slots, "| 7:00 PM allowed:", T(19, 0) in slots)
print("Offered 30-min starts:", ", ".join(fmt(t) for t in slots))
print("Candidates (one per window):", [(fmt(t), w) for t, w in candidates(slots, 30, set(WINDOWS))])
print("OK: every slot keeps >= 30 min before and after every event on both calendars")
