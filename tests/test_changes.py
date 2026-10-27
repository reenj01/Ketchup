"""Lengths are clean multiples of 5, the other person never hears the
requester's words or reason, and there is no cap on changes. Runs real flow
code with the calendar and the model stubbed out (dry-run holds in a temp store)."""
import queue
import sys
import tempfile
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain"))
import agents  # noqa: E402
import holds  # noqa: E402
import web_flow  # noqa: E402
from constraints import keyword, merge  # noqa: E402
from lengths import clean, fix_text, middle  # noqa: E402
from parse_plan import TZ  # noqa: E402
from privacy import Neutral  # noqa: E402
from web_flow import WebSession, map_minutes  # noqa: E402

# ---- lengths: multiples of 5, rounded down -----------------------------------
assert middle(30, 45) == 35 and middle(30, 60) == 45 and middle(45, 60) == 50
assert clean(37) == 35 and clean(37.5) == 35 and clean(52) == 50 and clean(10) == 15
assert map_minutes("37 minutes")[0] == 35 and map_minutes("1 hr 22")[0] == 80
assert map_minutes("1 hour")[0] == 60 and map_minutes("45")[0] == 45 and map_minutes("2 hours")[0] == 120
assert fix_text("37 min? Let's do 37.5 minutes") == "35 min? Let's do 35 minutes"
assert fix_text("30 min before it starts") == "30 min before it starts"


def fake_ask(replies):
    def ask(system, user, check=None, **kw):
        r = dict(replies.pop(0))
        check(r)
        return {"reply": r, "model": "stub", "errors": [], "reasoning": ""}
    return ask


M, F = {"name": "Mayo"}, {"name": "Frankie"}
agents.ask_json = fake_ask([{"decision": "propose", "minutes": 37, "reason": "Let's meet in the middle at 37 min."},
                            {"decision": "accept", "minutes": 37, "reason": "37 min works."}])
got = agents.negotiate_length(M, F, "catch up", {"Mayo": 30, "Frankie": 45}, log=lambda *a: None)
assert got["minutes"] == 35, got
assert got["turns"][0]["reply"]["reason"] == "Let's meet in the middle at 35 min.", got["turns"][0]["reply"]
assert "35 min" in agents.LENGTH_SYSTEM.format(name="Mayo", other="Frankie", activity="x", mine=30, theirs=45,
                                                lo=30, hi=45, step=5, mid=middle(30, 45))
print("OK lengths: 30+45->35, 30+60->45, 45+60->50, '37 minutes'->35, '1 hr 22'->80, agent 37->35")

# ---- a web session with stubbed calendar + agents ------------------------------
NOW = datetime.now(TZ)
DAY0 = (NOW + timedelta(days=1)).date()


def session():
    s = WebSession.__new__(WebSession)
    s.keys, s.people = ["mayo", "frankie"], [web_flow.PEOPLE["mayo"], web_flow.PEOPLE["frankie"]]
    s.sid, s.phrase, s.dry_run, s.now = "t", "catch up", True, NOW
    s.lock, s.messages, s.thinking, s.state = threading.Lock(), [], {}, "held"
    s.open_q, s.mapping, s.kept = {}, set(), set()
    s.answers = {k: {"time": "morning", "minutes": 30} for k in s.keys}
    s.rejected, s.offered, s.change_round = [], set(), 0
    s.hold = s.backup = s.slot = s.last_change = None
    s.cons, s.minutes, s.cal = {}, 30, None
    s.plan = {"activity": "catch up"}
    s.start_day, s.end_day = DAY0, DAY0 + timedelta(days=1)
    s.trace = {"steps": [], "agent_turns": []}
    s.jobs, s.save = queue.Queue(), lambda: None
    s.profiles = {p["name"]: {} for p in s.people}
    return s


web_flow.freebusy = lambda cal, ids, a, b: {i: [] for i in ids}  # everyone free
picked = []


def stub_negotiate(proposer, other, activity, cands, *a, **kw):
    picked.append(cands[0][0])
    return {"agreed_index": 0, "how": "stub", "turns": [], "ranked": [0], "offered": [0],
            "backup_index": 1 if len(cands) > 1 else None, "backup_how": "stub"}


web_flow.negotiate = stub_negotiate
with tempfile.TemporaryDirectory() as d:
    holds.STORE = Path(d) / "holds.json"
    s = session()
    s.change_round = 1  # skip the first-round free-time sharing chatter
    s._negotiate()
    first = s.slot[0]

    # ---- privacy: the other chat never gets the requester's words or reason ----
    def other_chat(s, since):
        return " ".join(m["text"] for m in s.messages[since:] if m["channel"] == "frankie")

    paths = []
    # raw reply as typed; constraints as the parser reads its reason part
    for text, reason, secret in (
            ("not thursday, something later because my dentist moved", "not thursday, something later",
             ["thursday", "later", "dentist", "moved", "not thu"]),
            ("can we do a different time? I have a work dinner", "can we do a different time?",
             ["dinner", "work dinner", "i have"])):
        change, cons = keyword(reason, s.slot[0], NOW.date())
        n = len(s.messages)
        s.open_q = {"mayo": "change", "frankie": "change"}
        s._change("mayo", text, cons, "keyword")
        seen = other_chat(s, n)
        mine = " ".join(m["text"] for m in s.messages[n:] if m["channel"] == "mayo")
        assert "Mayo would prefer a different day or time" in seen and "couldn't find" not in seen, seen
        leaked = [w for w in secret + ["said", "asked", "\u201c", "because"] if w in seen.lower()]
        assert not leaked and text not in seen, (leaked, seen)
        assert "You said" in mine, mine  # the requester's own chat may echo them
        paths.append("backup" if "moved it to our backup" in seen else "new" if "here's a new one" in seen else "?")
    assert sorted(paths) == ["backup", "new"], paths  # both the renegotiated and the backup path
    print("OK privacy (web): Frankie's chat:", seen)

    # ---- no cap: 10 changes in a row, each a new time, none repeating ----------
    s = session()
    s.change_round = 1
    s._negotiate()
    held = [s.slot[0]]
    for i in range(10):
        s.open_q = {"mayo": "change", "frankie": "change"}
        s._change("mayo", "different time please", keyword("different time please", s.slot[0], NOW.date())[1],
                  "keyword")
        assert s.state != "done" and s.hold and s.hold["status"] == "tentative", (i, s.state)
        held.append(s.slot[0])
    assert len(set(held)) == len(held) == 11, held
    assert not any("most changes" in m["text"] for m in s.messages)
    print(f"OK no cap: 10 changes -> 11 different times, {held[0]:%a %-I:%M %p} ... {held[-1]:%a %b %-d %-I:%M %p}")
    assert any("so looking at" in m["text"] for m in s.messages), "should widen to next week"

# ---- pebble lead: the other pebble gets the template only ---------------------
assert Neutral("Mayo").text("Friday Oct 30 at 10:30 AM") == \
    "Mayo would prefer a different day or time, so here's a new one: Friday Oct 30 at 10:30 AM."

# ---- constraints accumulate; newest wins on contradiction ---------------------
ref = datetime(2026, 10, 29, 10, 30, tzinfo=TZ)
c1 = keyword("not thursday", ref, date(2026, 10, 27))[1]
c2 = dict(c1, not_days=[], only_days=["2026-10-29"])  # newer: "only Thursday"
m = merge(c1, c2)
assert "2026-10-29" not in m["not_days"], m
c3 = keyword("not friday", ref, date(2026, 10, 27))[1]
assert {"2026-10-29", "2026-10-30", "2026-12-17"} <= set(merge(c1, c3)["not_days"])  # every Thu/Fri in 8 weeks
print("OK constraints accumulate, newest wins")

# ---- pebble: the other pebble's line is the template, never the reason --------
import flow  # noqa: E402

fl = flow.Session.__new__(flow.Session)
fl.people = [{"name": "Mayo", "device_id": "pebble-mayo"}, {"name": "Frankie", "device_id": "pebble-frankie"}]
raw = "not thursday, something later because my dentist moved"
fl.last_change = ("Mayo", raw, keyword("not thursday, something later", ref, date(2026, 10, 27))[1])
fl.slot = (datetime(2026, 10, 30, 10, 30, tzinfo=TZ), "morning")
fl.relaxed = None
lead = fl.change_lead()
assert "You said not Thursday" in lead["pebble-mayo"], lead
heard = lead["pebble-frankie"].text("Friday Oct 30 at 10:30 AM")
assert not [w for w in ("thursday", "later", "dentist", "said", raw) if w in heard.lower()], heard
print("OK privacy (pebble): Frankie's pebble:", heard)
