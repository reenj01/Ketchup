"""Tentative holds that confirm themselves at the end of the day.

When the agents agree, the event goes on both calendars as a hold (Google
event status "tentative", title ending " (tentative)"). It stays tentative
until both people keep it, someone changes it, or the deadline passes:
10 PM ET (`hold_confirm_at`) if the hold was made before 10 PM, else 11:59 PM
that same night (`hold_late_confirm_at`); 30 min before the event starts if
that is sooner. At the deadline nobody said anything, so it's confirmed.

Sessions live in memory, so holds are saved in state/holds.json (gitignored).
Each server runs `start_timer()`: it sweeps at startup and every
`hold_sweep_s` seconds. Everything is idempotent: event ids are chosen by us
and stored before the insert, a confirmed hold is never patched twice, and a
file lock serialises the web server, the tap server and the CLI.
"""
import fcntl
import json
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, time, timedelta
from pathlib import Path

from googleapiclient.errors import HttpError

from book import MARK, book, title

__all__ = ["MARK", "STORE", "LISTENERS", "deadline_for", "deadline_text", "get", "create", "move", "confirm",
           "release", "sweep", "prune", "start_timer"]
from calendar_slots import SETTINGS
from parse_plan import TZ
from timing import timed

STORE = Path(__file__).parent.parent / "state" / "holds.json"
CONFIRM_AT = time.fromisoformat(SETTINGS["hold_confirm_at"])
LATE_CONFIRM_AT = time.fromisoformat(SETTINGS["hold_late_confirm_at"])
LEAD = timedelta(minutes=SETTINGS["hold_lead_min"])
LISTENERS = {}  # hold id -> fn(record), live sessions in this process
_calendar = None  # set by start_timer: () -> Google client


def deadline_for(made: datetime, start: datetime) -> datetime:
    """10 PM ET if the hold was made before 10 PM, else 11:59 PM that same night
    (no next-day rollover; made at 11:59 PM -> confirmed then). 30 min before it
    starts if that is sooner."""
    m = made.astimezone(TZ)
    cut = datetime.combine(m.date(), CONFIRM_AT, tzinfo=TZ)
    if m >= cut:
        cut = datetime.combine(m.date(), LATE_CONFIRM_AT, tzinfo=TZ)
    return min(cut, start - LEAD)


def clock(d: datetime) -> str:
    """'10 PM', '5:30 PM'."""
    return f"{d:%-I %p}" if d.minute == 0 else f"{d:%-I:%M %p}"


def deadline_text(deadline: datetime, made: datetime) -> str:
    """'tonight at 10 PM', 'tonight at 11:59 PM', or 'tonight at 5:30 PM, 30 min before it starts'."""
    d, m = deadline.astimezone(TZ), made.astimezone(TZ)
    days = (d.date() - m.date()).days
    day = ("tonight" if d.hour >= 17 else "today") if days == 0 else "tomorrow" if days == 1 else f"on {d:%a %b %-d}"
    lead = "" if d.time() in (CONFIRM_AT, LATE_CONFIRM_AT) else ", 30 min before it starts"
    return f"{day} at {clock(d)}{lead}"


@contextmanager
def _store():
    STORE.parent.mkdir(exist_ok=True)
    with open(STORE.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = json.loads(STORE.read_text()) if STORE.exists() else {}
        yield data
        tmp = STORE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(STORE)


def get(hold_id):
    with _store() as data:
        return data.get(hold_id)


def _event_ids(hold_id, n):
    # Google event ids: base32hex (a-v, 0-9), 5-1024 chars. uuid hex fits.
    return [f"ketchup{hold_id}{i}" for i in range(n)]


def create(cal, people, start, minutes, activity, session_id, source, dry_run, now=None):
    """Save the hold, then insert it (tentative) on every calendar."""
    now = now or datetime.now(TZ)
    hid = uuid.uuid4().hex
    ids = _event_ids(hid, len(people))
    rec = {"id": hid, "source": source, "session_id": session_id, "dry_run": dry_run,
           "title": title(people, activity), "people": [p["name"] for p in people],
           "start": start.isoformat(), "minutes": minutes, "made_at": now.isoformat(),
           "deadline": deadline_for(now, start).isoformat(), "status": "tentative",
           "events": [{"person": p["name"], "calendar_id": p["calendar_id"], "event_id": i}
                      for p, i in zip(people, ids)] if not dry_run else [],
           "history": [f"{now:%H:%M:%S} pencilled in {start:%a %b %-d %-I:%M %p}"]}
    with _store() as data:  # saved first: a crash mid-insert still leaves the ids to clean up
        data[hid] = rec
    if not dry_run:
        with _store() as data:
            made = book(cal, people, start, minutes, activity, tentative=True, ids=ids)
            rec["events"] = made
            rec["api_status"] = [e["status"] for e in made]
            data[hid] = rec
    return rec


def _patch_all(cal, rec, body):
    out = []
    for e in rec["events"]:
        with timed("google", "events.patch"):
            ev = cal.events().patch(calendarId=e["calendar_id"], eventId=e["event_id"], body=body).execute()
        out.append(ev.get("status"))
    return out


def move(cal, hold_id, start, minutes, now=None):
    """Move a tentative hold (e.g. to the backup). New deadline from now."""
    now = now or datetime.now(TZ)
    with _store() as data:
        rec = data[hold_id]
        if rec["status"] != "tentative":
            return rec
        if not rec["dry_run"]:
            rec["api_status"] = _patch_all(cal, rec, {
                "start": {"dateTime": start.isoformat(), "timeZone": "America/New_York"},
                "end": {"dateTime": (start + timedelta(minutes=minutes)).isoformat(),
                        "timeZone": "America/New_York"}})
        rec.update(start=start.isoformat(), minutes=minutes, made_at=now.isoformat(),
                   deadline=deadline_for(now, start).isoformat())
        rec["history"].append(f"{now:%H:%M:%S} moved to {start:%a %b %-d %-I:%M %p}")
    return rec


def confirm(cal, hold_id, why, now=None):
    """Tentative -> confirmed, title marker dropped. A no-op if already done."""
    now = now or datetime.now(TZ)
    with _store() as data:
        rec = data.get(hold_id)
        if not rec or rec["status"] != "tentative":
            return rec
        if not rec["dry_run"]:
            rec["api_status"] = _patch_all(cal, rec, {"status": "confirmed", "summary": rec["title"]})
        rec.update(status="confirmed", confirmed_at=now.isoformat(), why=why)
        rec["history"].append(f"{now:%H:%M:%S} confirmed ({why})")
    return rec


def release(cal, hold_id, now=None):
    """Delete the hold's events (someone changed it and the agents renegotiate)."""
    now = now or datetime.now(TZ)
    with _store() as data:
        rec = data.get(hold_id)
        if not rec or rec["status"] != "tentative":
            return rec
        for e in rec["events"]:
            with timed("google", "events.delete"):
                try:
                    cal.events().delete(calendarId=e["calendar_id"], eventId=e["event_id"]).execute()
                except HttpError as err:
                    if err.resp.status not in (404, 410):
                        raise
        rec["status"] = "released"
        rec["history"].append(f"{now:%H:%M:%S} released")
    return rec


def sweep(cal_factory=None, now=None, hold_ids=None):
    """Confirm every tentative hold whose deadline passed. `hold_ids` = act as
    if the day ended now for those holds ('Skip to end of day'); 'all' = every
    tentative hold. Returns the confirmed records."""
    now = now or datetime.now(TZ)
    with _store() as data:
        due = [h for h, r in data.items() if r["status"] == "tentative" and (
            hold_ids == "all" or (hold_ids and h in hold_ids)
            or (hold_ids is None and datetime.fromisoformat(r["deadline"]) <= now))]
        real = any(not data[h]["dry_run"] for h in due)
    if not due:
        return []
    cal = (cal_factory or _calendar)() if real else None
    why = "no reply by the deadline" if hold_ids is None else "skipped to end of day"
    out = []
    for h in due:
        rec = confirm(cal, h, why, now)
        if rec and rec["status"] == "confirmed":
            out.append(rec)
            fn = LISTENERS.pop(h, None)
            if fn:
                fn(rec)
    return out


def prune(now=None, keep_days=3):
    now = now or datetime.now(TZ)
    with _store() as data:
        for h in [h for h, r in data.items() if r["status"] != "tentative"
                  and datetime.fromisoformat(r["made_at"]) < now - timedelta(days=keep_days)]:
            del data[h]


def start_timer(cal_factory, log=print):
    """Sweep now (startup) and then every hold_sweep_s seconds, in a daemon thread."""
    global _calendar
    _calendar = cal_factory

    def loop():
        while True:
            try:
                for rec in sweep(cal_factory):
                    log(f"[holds] confirmed {rec['title']} {rec['start']} ({rec['why']})")
                prune()
            except Exception as e:  # keep the timer alive; the next sweep retries
                log(f"[holds] sweep failed: {e}")
            stop.wait(SETTINGS["hold_sweep_s"])

    stop = threading.Event()
    threading.Thread(target=loop, daemon=True).start()
    return stop
