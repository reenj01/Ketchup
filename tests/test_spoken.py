"""What the pebble says out loud: short plain lines, and text MMS-TTS can say
(its vocab has no 7/8/9, no colon, no punctuation)."""
import re
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "brain"), str(ROOT / "pebble")]
import pi_pebble as pb  # noqa: E402  (gpiozero is only imported in main())
from flow import spoken_deadline, spoken_when  # noqa: E402
from parse_plan import TZ  # noqa: E402
from privacy import Neutral  # noqa: E402

CASES = {
    "How about Tue Nov 3 at 6:00 PM?": "how about tuesday november third at six pee em?",
    "Thursday at 10:30 AM.": "thursday at ten thirty ay em.",
    "at 9:05 am": "at nine oh five ay em",
    "at 12 PM": "at twelve pee em",
    "11:59 PM ET": "eleven fifty-nine pee em",
    "Fri Oct 30": "friday october thirtieth",
    "Sept 21st": "september twenty-first",
    "45 min": "forty-five minutes",
    "in 8 weeks": "in eight weeks",
    "2026": "two thousand twenty-six",
    "Nothing to answer right now.": "nothing to answer right now.",
    "Got it: morning only.": "got it, morning only.",
    "You said \u201cnot Thursday\u201d": "you said not thursday",
}
for raw, want in CASES.items():
    got = pb.mms_text(raw)
    assert got == want, (raw, got, want)
    assert re.fullmatch(r"[a-z' ,.?!-]*", got), got  # nothing MMS can't say (no digits at all)
assert pb.words(19) == "nineteen" and pb.words(70) == "seventy" and pb.words(101) == "one hundred one"
assert [pb.ordinal(n) for n in (1, 2, 3, 12, 20, 22, 31)] == \
    ["first", "second", "third", "twelfth", "twentieth", "twenty-second", "thirty-first"]
assert pb.chunks("okay, morning. let me check with frankie. want to change it?") == \
    ["okay, morning. let me check with frankie.", "want to change it?"]  # short ones join the next
assert pb.chunks("okay, not on thursday at all. let me look.") == ["okay, not on thursday at all. let me look."]
print("OK mms_text: times, dates, numbers spelled out")

# ---- the spoken flow lines stay short ------------------------------------------
today = date(2026, 11, 2)
assert spoken_when(datetime(2026, 11, 3, 18, 0, tzinfo=TZ), today) == "Tuesday at 6 PM"
assert spoken_when(datetime(2026, 11, 10, 9, 30, tzinfo=TZ), today) == "Tuesday November 10 at 9:30 AM"
made = datetime(2026, 11, 2, 10, 0, tzinfo=TZ)
assert spoken_deadline(datetime(2026, 11, 2, 22, 0, tzinfo=TZ), made) == "at 10 PM tonight"
assert spoken_deadline(datetime(2026, 11, 2, 11, 30, tzinfo=TZ), made) == "at 11:30 AM today"
for line in (Neutral("Mayo").spoken("Friday at 10:30 AM"), Neutral("Mayo", "backup").spoken("Friday at 10:30 AM"),
             f"You and Frankie can do Tuesday at 6 PM. I'll hold it, and lock it in {spoken_deadline(made, made)}. "
             "Want to change it?"):
    sentences = [s for s in re.split(r"(?<=[.?!])\s+", line) if s]
    assert len(sentences) <= 3 and all(len(s.split()) <= 12 for s in sentences), line
    assert not re.search(r"pencil|tentative|confirmed|:\d\d [AP]M ET", line), line
print("OK spoken lines: short, plain")

# ---- the time each Dot proposes: "You and <the other person> can do ..." (no "How about") ----------
import flow  # noqa: E402
from types import SimpleNamespace  # noqa: E402
t = datetime(2026, 11, 6, 18, 30, tzinfo=TZ)
fake = SimpleNamespace(people=[{"name": "Mayo", "device_id": "pebble-mayo"}, {"name": "Frankie", "device_id": "pebble-frankie"}],
                       answers={"Mayo": {"choice": "night"}}, notes={})
when = spoken_when(t, datetime.now(TZ).date())
line = flow.Session.proposal_line(fake, fake.people[0], t)
assert line == f"You and Frankie can do {when}.", line
assert flow.Session.proposal_line(fake, fake.people[1], t) == f"You and Mayo can do {when}."
fake.notes = {t: {"Mayo": "nothing worked at night", "Frankie": "middle ground"}}
assert flow.Session.proposal_line(fake, fake.people[0], t) == f"Nothing worked at night on Friday. You and Frankie can do {when}."
assert flow.Session.proposal_line(fake, fake.people[1], t) == f"You and Mayo can do {when}."  # never says whose pick
assert Neutral("Frankie").spoken("Friday at 6:30 PM") == "Frankie wants a different time. You and Frankie can do Friday at 6:30 PM."
full = "You and Frankie can do Friday at 6:30 PM. I'll hold it, and lock it in at 10 PM tonight. Want to change it?"
assert pb.mms_text(full) == "you and frankey can do friday at six thirty pee em. i'll hold it, and lock it in at ten pee em this evening. want to change it?", pb.mms_text(full)
assert pb.say_as(full).startswith("You and Franky can do Friday")
print("OK proposals: 'You and Frankie can do ...' / 'You and Mayo can do ...', said 'Franky'")

# "Dots" is respelled so MMS doesn't blur "both Dots ask" into "both Dutch"
assert pb.mms_text("Both Dots ask: Morning?") == "both dotts ask, morning?", pb.mms_text("Both Dots ask: Morning?")
assert pb.mms_text("Mayo's Dot") == "mayo's dot"
print("OK Dots respelled for MMS")

# "Frankie" is said as "Franky" (audio only): MMS gets "frankey" (MMS reads "franky" as "Frandy"),
# espeak gets "Franky". IDs, env values and the printed / on-screen text keep "Frankie".
assert pb.mms_text("Plan booked with Frankie.") == "plan booked with frankey."
assert pb.mms_text("Let me check with frankie. FRANKIE's Dot") == "let me check with frankey. frankie's dot"
assert pb.say_as("Plan booked with Frankie.") == "Plan booked with Franky."
assert pb.say_as("Frankie's Dot") == "Franky's Dot" and pb.say_as("Mayo") == "Mayo"
import os, tempfile, threading  # noqa: E401,E402
stub = Path(tempfile.mkdtemp(prefix="espeak_args_"))
(stub / "espeak-ng").write_text('#!/bin/sh\nfor a; do echo "$a"; done >> "%s/args"\n' % stub)
(stub / "aplay").write_text("#!/bin/sh\ncat > /dev/null\n")
for f in ("espeak-ng", "aplay"):
    (stub / f).chmod(0o755)
path, os.environ["PATH"] = os.environ["PATH"], f"{stub}:{os.environ['PATH']}"
try:
    pb.say_espeak("Plan booked with Frankie.", threading.Event())
finally:
    os.environ["PATH"] = path
assert (stub / "args").read_text().splitlines()[-1] == "Plan booked with Franky.", (stub / "args").read_text()
print("OK Frankie said as Franky: MMS 'frankey', espeak 'Franky'; IDs untouched")
