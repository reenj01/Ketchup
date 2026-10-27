"""'Change' reasons -> hard constraints, filtering, backups and the hold store
(keyword path and dry-run holds only: no model or Google calls)."""
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain"))
import holds  # noqa: E402
from agents import fix_backup  # noqa: E402
from calendar_slots import free_slots, window_of  # noqa: E402
from constraints import describe, followed, keyword, merge, why_not  # noqa: E402
from parse_plan import TZ  # noqa: E402

TODAY = date(2026, 10, 27)                       # Tuesday
REF = datetime(2026, 10, 29, 10, 30, tzinfo=TZ)  # the slot being changed: Thu 10:30 AM
THU, FRI = "2026-10-29", "2026-10-30"


def kw(text):
    return keyword(text, REF, TODAY)


# ---- parsing: keyword fast path -------------------------------------------
change, c = kw("not thursday, something later")
assert change == "yes" and THU in c["not_days"] and c["after"] == REF.isoformat(), c
assert describe(c) == "not Thursday; later than Thu Oct 29 10:30 AM", describe(c)
assert kw("yeah that works for me")[0] == "no"
assert kw("keep")[0] == "no" and kw("perfect, keep it")[0] == "no"
assert kw("hmm, can we do a different time?")[0] == "yes"
change, c = kw("friday instead")
assert change == "yes" and FRI in c["only_days"] and THU not in c["only_days"]
assert kw("can we do the evening?")[1]["windows"] == ["night"]
assert sorted(kw("not in the morning")[1]["windows"]) == ["night", "noon"]
assert kw("an hour please")[1]["minutes"] == 60
assert kw("later in the day")[1]["after_time"] == "10:30"
assert {THU, FRI} <= set(kw("can't do thursday or friday")[1]["not_days"])
# words the keywords can't account for go to the model, never a guess
assert kw("my cat has a vet appointment that day") is None

# ---- filtering: constraints are hard filters before ranking -------------------
free = {"x": []}
slots = free_slots(free, date(2026, 10, 29), date(2026, 10, 30), 30, datetime(2026, 10, 27, 9, tzinfo=TZ))
_, c = kw("not thursday, something later")
kept = [t for t in slots if why_not(c, t) is None]
assert kept and all(t.date() == date(2026, 10, 30) for t in kept)
assert why_not(c, REF) == "it's on Thursday"
assert why_not(c, datetime(2026, 10, 30, 9, tzinfo=TZ)) is None
_, c = kw("earlier")
assert why_not(c, datetime(2026, 10, 30, 9, tzinfo=TZ)) == "it's not earlier than Thu 10:30 AM"
_, c = kw("can we do the evening?")
assert all(window_of(t) == "night" for t in slots if why_not(c, t) is None)
# constraints add up across changes: "not thursday" then "evening" keeps both
both = merge(kw("not thursday")[1], kw("in the evening")[1])
assert THU in both["not_days"] and both["windows"] == ["night"]
assert followed(both, datetime(2026, 10, 30, 18, tzinfo=TZ)) == "it's on Friday, it's at night"

# ---- backup: never the pick, never offered before, another day when possible --
s = [(datetime(2026, 10, 29, 10, 30, tzinfo=TZ), "morning"), (datetime(2026, 10, 29, 11, tzinfo=TZ), "morning"),
     (datetime(2026, 10, 30, 10, 30, tzinfo=TZ), "morning")]
r = {"pick": 1, "backup": 1, "ranking": [1, 2, 3]}
assert fix_backup(r, s, set(), "morning", "morning")[0] == 3      # moved, onto another day
assert fix_backup(dict(r, backup=2), s, set(), "morning", "morning") == (2, None)  # valid: kept
assert fix_backup(dict(r, backup=3), s, {3}, "morning", "morning")[0] == 2        # 3 was offered

# ---- deadlines ------------------------------------------------------------------
made = datetime(2026, 10, 27, 15, 0, tzinfo=TZ)                    # hold at 3 PM -> 10 PM
assert holds.deadline_for(made, REF) == datetime(2026, 10, 27, 22, 0, tzinfo=TZ)
assert holds.deadline_text(holds.deadline_for(made, REF), made) == "tonight at 10 PM"
late = datetime(2026, 10, 27, 22, 30, tzinfo=TZ)                   # hold at 10:30 PM -> 11:59 PM same night
assert holds.deadline_for(late, REF) == datetime(2026, 10, 27, 23, 59, tzinfo=TZ)
assert holds.deadline_text(holds.deadline_for(late, REF), late) == "tonight at 11:59 PM"
at10 = datetime(2026, 10, 27, 22, 0, tzinfo=TZ)                    # exactly 10 PM counts as late
assert holds.deadline_for(at10, REF) == datetime(2026, 10, 27, 23, 59, tzinfo=TZ)
at1159 = datetime(2026, 10, 27, 23, 59, tzinfo=TZ)                 # made at 11:59 PM -> confirmed then
assert holds.deadline_for(at1159, REF) == at1159
soon = datetime(2026, 10, 27, 18, 0, tzinfo=TZ)                    # event 6 PM same day, held at 3 PM
assert holds.deadline_for(made, soon) == datetime(2026, 10, 27, 17, 30, tzinfo=TZ)
assert holds.deadline_text(holds.deadline_for(made, soon), made) == "tonight at 5:30 PM, 30 min before it starts"

# ---- hold store (dry run: no calendar), idempotent sweep ---------------------------
with tempfile.TemporaryDirectory() as d:
    holds.STORE = Path(d) / "holds.json"
    people = [{"name": "Mayo", "calendar_id": "m"}, {"name": "Frankie", "calendar_id": "f"}]
    h = holds.create(None, people, REF, 30, "catch up", "s1", "test", True, made)
    assert h["status"] == "tentative" and holds.get(h["id"])["deadline"].startswith("2026-10-27T22:00")
    assert holds.sweep(now=made + timedelta(hours=1)) == []            # not due yet
    heard = []
    holds.LISTENERS[h["id"]] = heard.append
    assert holds.sweep(now=datetime(2026, 10, 27, 21, 59, tzinfo=TZ)) == []  # 9:59 PM: not yet
    done = holds.sweep(now=datetime(2026, 10, 27, 22, 0, tzinfo=TZ))   # 10 PM
    assert [x["id"] for x in done] == [h["id"]] and heard and heard[0]["why"] == "no reply by the deadline"
    assert holds.get(h["id"])["status"] == "confirmed"
    assert holds.sweep(now=datetime(2026, 10, 28, 0, 1, tzinfo=TZ)) == []  # confirmed once only
    h2 = holds.create(None, people, REF, 30, "catch up", "s2", "test", True, made)
    holds.move(None, h2["id"], REF + timedelta(days=1), 30, made)
    assert holds.get(h2["id"])["start"].startswith(FRI)
    assert holds.sweep(hold_ids=[h2["id"]], now=made)[0]["why"] == "skipped to end of day"
    h3 = holds.create(None, people, REF, 30, "catch up", "s3", "test", True, made)
    assert holds.release(None, h3["id"])["status"] == "released"
    assert holds.sweep(hold_ids="all", now=made) == []                 # released holds stay released
print("OK: reasons parsed, constraints filter slots, backups valid, holds confirm once")
