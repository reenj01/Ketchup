"""Re-run one traced slot negotiation with today's agents (real model calls).

Takes the slots, each person's own busy blocks and answers from a saved
trace, so old behaviour (e.g. a repeated proposal) can be compared with the
current code. Nothing is booked.

  .venv/bin/python scripts/replay_negotiation.py traces/<file>.json [--said Mayo=night]
"""
import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain"))
from agents import negotiate  # noqa: E402
from calendar_slots import window_of  # noqa: E402
from gcal import PEOPLE  # noqa: E402
from parse_plan import TZ  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("trace")
ap.add_argument("--said", action="append", default=[], help="Name=window: treat as an explicit answer")
ap.add_argument("--test-habit", action="append", default=[],
                help="Name=morning|afternoon|evening: TEST ONLY, replaces the inferred habit to check "
                     "that a spoken answer still wins (never used by the product)")
a = ap.parse_args()
t = json.loads(Path(a.trace).read_text())
year = t["started_at"][:4]
first = [x for x in t["agent_turns"] if x.get("topic") != "length"]
people = {p["name"]: p for p in PEOPLE.values()}
busy, slots, minutes = {}, None, None
for name in people:
    user = next(x["prompt"]["user"] for x in first if x["agent"] == name)
    blocks = []
    for day, spans in re.findall(r"^(\w{3} \w{3} \d+) busy: (.*)$", user, re.M):
        for s, e in re.findall(r"(\d+:\d+ [AP]M)-(\d+:\d+ [AP]M)", spans):
            blocks.append(tuple(datetime.strptime(f"{year} {day} {x}", "%Y %a %b %d %I:%M %p").replace(tzinfo=TZ)
                                for x in (s, e)))
    busy[people[name]["calendar_id"]] = blocks
    rows = re.findall(r"^\s+\d+\. (\w{3} \w{3} \d+, \d+:\d+ [AP]M) ET \[(\w+)[^\]]*\] \((\d+) min\)", user, re.M)
    slots = [(datetime.strptime(f"{year} {s}", "%Y %a %b %d, %I:%M %p").replace(tzinfo=TZ), w) for s, w, _ in rows]
    minutes = int(rows[0][2])
said = dict(x.split("=") for x in a.said)
answers = {s["person"]: s for s in t["steps"] if s["step"] == "answer"
           and s.get("question", "") in ("Morning, noon, or night?", "What time of day do you prefer to meet?")}
defaults = {s["person"]: s for s in t["steps"] if s["step"] == "default_answer"}
profiles = {i["person"]: i["profile"] for i in t["inferred_profiles"]}
for x in a.test_habit:
    n, h = x.split("=")
    profiles[n] = {**profiles[n], "time_of_day_preference": h, "confidence": "high",
                   "summary": f"TEST ONLY: habit set to {h} to check that what {n} said wins"}
    print(f"TEST ONLY: {n}'s habit replaced with {h} (high confidence)")
know = {}
for name in people:
    if name in said:
        know[name] = {"answer": f"{said[name]} (said by {name})", "said": said[name]}
    elif name in answers:
        know[name] = {"answer": f"{answers[name]['mapped_to']} (said by {name})", "said": answers[name]["mapped_to"]}
    else:
        know[name] = {"answer": f"{defaults[name]['choice']} (no answer: default from calendar habits)", "said": None}
    know[name].update(profile=profiles[name], free=[])
proposer = people[t.get("proposer", "Mayo").title()] if t.get("proposer") else people["Mayo"]
other = next(p for p in people.values() if p is not proposer)
print("Slots:", ", ".join(f"{i}. {s:%a %-I:%M %p} [{w}]" for i, (s, w) in enumerate(slots, 1)))
print("Answers:", {n: k["answer"] for n, k in know.items()})
print("Habits:", {n: profiles[n]["time_of_day_preference"] for n in know})
print("Old run:", [(x["agent"], x["reply"]["decision"], x["reply"]["pick"]) for x in first])
res = negotiate(proposer, other, t["parsed_plan"]["activity"], slots, busy, minutes, know)
for x in res["turns"]:
    if x.get("rejected_because"):
        print(f"  code check on {x['agent']}'s agent: model wanted slot {x['rejected_reply']['pick']} -> "
              f"{x['rejected_because']}" + (" -> moved by code" if x.get("moved_by_code") else " -> retried"))
print("Agreed:", f"{slots[res['agreed_index']][0]:%a %-I:%M %p}", res["how"])
