"""The reply to "Do you want a different day or time?" is keep / change / unclear.
Any complaint, objection or stated preference is a change with its reason as
constraints; only a bare no or a short affirmation is keep; unclear asks again.
RJ's bug: Frankie said "that's not at night" and the time was kept."""
import queue
import sys
import tempfile
import threading
from datetime import date, datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain"))
import constraints  # noqa: E402
import holds  # noqa: E402
import web_flow  # noqa: E402
from calendar_slots import window_of  # noqa: E402
from constraints import describe, keyword  # noqa: E402
from parse_plan import TZ  # noqa: E402
from web_flow import WebSession  # noqa: E402

REF = datetime(2026, 10, 29, 10, 30, tzinfo=TZ)
TODAY = date(2026, 10, 27)
# reply -> (keyword result: "keep" / "change" / None = goes to the model, constraints described)
TABLE = {
    "that's not at night": ("change", "night only"),  # describes the slot: they want night
    "that's not at night.": ("change", "night only"),
    "it's not in the evening": ("change", "night only"),
    "not at night": ("change", "morning or noon only"),  # a request: anything but night
    "no night please": ("change", "morning or noon only"),
    "no": ("keep", ""), "nope": ("keep", ""), "no thanks": ("keep", ""), "nah it's fine": ("keep", ""),
    "keep it": ("keep", ""), "sounds good": ("keep", ""), "perfect": ("keep", ""), "that works": ("keep", ""),
    "yes": ("change", ""), "yes change it": ("change", ""),
    "no, can we do later": ("change", "later than Thu Oct 29 10:30 AM"),
    "too early": ("change", "later in the day than 10:30 AM"),
    "too late": ("change", "earlier in the day than 10:30 AM"),
    "not thursday": ("change", "not Thursday"),
    "can it be at night": ("change", "night only"),
    "evening please": ("change", "night only"),
    "that doesn't work": ("change", ""), "no that doesn't work for me": ("change", ""),
    "something in the afternoon": ("change", "noon only"),
    "nah, i'd rather do the morning": ("change", "morning only"),
    "hmm": (None, None), "idk": (None, None), "ok": (None, None), "that's not thursday": (None, None),
}
bad = []
for text, (want, cons) in TABLE.items():
    k = keyword(text, REF, TODAY)
    got = None if k is None else ({"no": "keep", "yes": "change"}[k[0]],
                                  describe(k[1]) if constraints.active(k[1]) else "")
    if got != (None if want is None else (want, cons)):
        bad.append((text, got, want, cons))
assert not bad, bad
# the fast path may only say keep for a bare no / short affirmation
assert all(want != "keep" or len(t.split()) <= 3 for t, (want, _) in TABLE.items())
print(f"OK replies: {len(TABLE)} rows (keep only for bare no/affirmations; reasons -> change)")


# ---- the model's yes/no can't drop a reason -----------------------------------
def fake_ask(reply):
    def ask(system, user, check=None, **kw):
        check(reply)
        return {"reply": dict(reply), "model": "stub", "errors": [], "reasoning": ""}
    return ask


constraints.ask_json = fake_ask({"change": "no", "windows": ["night"]})  # what the model returned for RJ
r = constraints.parse_change("well that's not at night is it", REF, TODAY)
assert r["change"] == "yes" and r["constraints"]["windows"] == ["night"], r
constraints.ask_json = fake_ask({"change": "unclear"})
assert constraints.parse_change("hmm", REF, TODAY)["change"] is None
print('OK model "no" + a reason -> change (was: kept)')

# ---- web: RJ's case end to end, through _map and _handle_answer ----------------
NOW = datetime.now(TZ)
DAY0 = (NOW + timedelta(days=1)).date()


def session():
    s = WebSession.__new__(WebSession)
    s.keys, s.people = ["mayo", "frankie"], [web_flow.PEOPLE["mayo"], web_flow.PEOPLE["frankie"]]
    s.sid, s.phrase, s.dry_run, s.now = "t", "catch up", True, NOW
    s.lock, s.messages, s.thinking, s.state = threading.Lock(), [], {}, "held"
    s.open_q, s.mapping, s.kept = {}, set(), set()
    s.answers = {k: {"time": "morning", "minutes": 30} for k in s.keys}
    s.rejected, s.offered, s.change_round = [], set(), 1
    s.hold = s.backup = s.slot = s.last_change = s.relaxed = None
    s.cons, s.minutes, s.cal = {}, 30, None
    s.plan = {"activity": "catch up"}
    s.start_day, s.end_day = DAY0, DAY0 + timedelta(days=1)
    s.trace = {"steps": [], "agent_turns": []}
    s.jobs, s.save = queue.Queue(), lambda: None
    s.profiles = {p["name"]: {} for p in s.people}
    return s


def reply(s, key, text):
    s.open_q[key] = "change"
    s._map(key, "change", text)  # the exact path the page uses
    job = s.jobs.get_nowait()
    getattr(s, "_" + job[0])(*job[1:])


def stub_negotiate(proposer, other, activity, cands, *a, **kw):
    return {"agreed_index": 0, "how": "stub", "turns": [], "ranked": [0], "offered": [0],
            "backup_index": 1 if len(cands) > 1 else None, "backup_how": "stub"}


web_flow.negotiate = stub_negotiate
web_flow.freebusy = lambda cal, ids, a, b: {i: [] for i in ids}  # everyone free
with tempfile.TemporaryDirectory() as d:
    holds.STORE = Path(d) / "holds.json"
    s = session()
    s._negotiate()
    s.open_q = {"mayo": "change", "frankie": "change"}
    first = s.slot[0]
    assert window_of(first) == "morning", first
    reply(s, "frankie", "that's not at night")
    assert "frankie" not in s.kept and s.state != "done", (s.kept, s.state)
    assert window_of(s.slot[0]) == "night" and s.slot[0] != first, s.slot
    assert not [m for m in s.messages if m["channel"] == "frankie" and "keeping it" in m["text"]]
    print(f"OK web: \"that's not at night\" -> searched again: {first:%a %-I:%M %p} -> {s.slot[0]:%a %-I:%M %p}")

    # unclear: ask again, never keep
    s.open_q = {"mayo": "change", "frankie": "change"}
    constraints.ask_json = fake_ask({"change": "unclear"})
    reply(s, "mayo", "hmm")
    last = [m["text"] for m in s.messages if m["channel"] == "mayo"][-1]
    assert "Should I keep it or find another time?" in last and "mayo" not in s.kept and s.open_q["mayo"] == "change"
    print("OK unclear -> asks again:", last.split("(")[0].strip())

    # night is full for 8 weeks: other windows, and it says so
    def night_busy(cal, ids, a, b):
        days = [(a + timedelta(days=i)).date() for i in range((b - a).days + 1)]
        return {i: [(datetime.combine(d, time(16, 0), TZ), datetime.combine(d, time(23, 0), TZ)) for d in days]
                for i in ids}
    web_flow.freebusy = night_busy
    s = session()
    s._negotiate()
    s.open_q = {"mayo": "change", "frankie": "change"}
    reply(s, "frankie", "that's not at night")
    said = " ".join(m["text"] for m in s.messages)
    mine = [m["text"] for m in s.messages if m["channel"] == "frankie"][-1]
    assert "No night time free for both in the next 8 weeks, so looking at other times of day." in said
    assert window_of(s.slot[0]) != "night" and "no night time free for both of you" in mine, mine
    print("OK no night slot in 8 weeks -> other windows, said so:", mine.split(". ")[0] + ".")
