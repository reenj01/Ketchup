"""What happens after two pebbles tap: a small state machine.

paired -> asking_time_of_day -> proposing -> pencilled_in -> booked | failed
(with --dry-run, "booked" becomes "agreed (dry run, not booked)").

1. Nudge: "Mayo and Frankie want to catch up Thursday."
2. Both pebbles ask "Morning, noon, or night?". Each person answers by voice.
   No answer before the timeout: their agent uses the habit it inferred from
   their calendar.
3. The agents share each other's free windows (times only) and pick a slot.
   Answers are soft: slots in the asked-for window rank first; if none work
   (30 min buffer + both calendars), other windows the same day are offered and
   the pebbles say so ("Nothing worked at noon, so how about 10:30 AM?").
   Next week only if nothing works in any window on the plan's dates.
   They agree a backup slot in the same turns.
4. The slot goes on both calendars as a tentative hold (holds.py) and both
   pebbles say "How about Thu Oct 29 at 12:00 PM? I've pencilled it in. If I
   don't hear from you, it's confirmed tonight at 10 PM. Want to change it?"
   keep -> kept (both kept = confirmed now). Silence -> confirmed at the
   deadline (10 PM ET, or 11:59 PM if held at/after 10 PM; 30 min before it starts if sooner).
   change + reason ("not thursday, something later") -> hard constraints
   (constraints.py): the backup if it fits, else the agents renegotiate under
   them. Offered times are never offered again. At most 2 changes.

Answers are voice transcripts; Muse Spark Open maps them to a choice. A plain
keyword ("night", "keep") is accepted directly, for testing.
"""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

import holds
from agents import fmt, negotiate
from book import book
from constraints import describe, followed, merge, parse_change, spoken, why_not
from calendar_slots import (ORDER, SETTINGS, WINDOWS, at, candidates, fallback_note, free_slots,
                            free_windows, freebusy, wanted_windows)
from calendar_slots import window_of as w_of
from gcal import PEOPLE, calendar
from infer_prefs import infer_prefs
from parse_plan import TZ, ask_json, parse_plan

TRACES = Path(__file__).parent.parent / "traces"
HABIT_TO_WINDOW = {"morning": "morning", "afternoon": "noon", "evening": "night"}
CHOICES = {"time_of_day": ORDER, "confirm": ["keep", "change"]}
DONE = ("booked", "agreed (dry run, not booked)", "failed")
Q_CHANGE = "Want to change it?"


def hm(t):
    return t.strftime("%-I:%M %p")


WINDOW_TEXT = ", ".join(f"{k} {hm(a)}-{hm(b)}" for k, (a, b) in WINDOWS.items())


TIME_WORDS = {w: c for c, ws in {
    "morning": ["mornings", "the morning", "in the morning", "am", "early"],
    "noon": ["afternoon", "afternoons", "the afternoon", "in the afternoon", "midday", "lunch", "lunchtime"],
    "night": ["nights", "evening", "evenings", "the evening", "in the evening", "tonight", "at night"],
}.items() for w in ws}


def map_answer(kind, question, text):
    """Voice transcript -> one of CHOICES[kind], or None if unclear.
    Returns (choice, how, model_detail)."""
    t = text.strip().lower().strip(".!?")
    if t in CHOICES[kind]:
        return t, "keyword", None
    if kind == "time_of_day" and t in TIME_WORDS:  # plain one-word answers need no model call
        return TIME_WORDS[t], "keyword", None
    extra = f" Windows: {WINDOW_TEXT}." if kind == "time_of_day" else \
        " keep = they are happy with the time; change = they want a different time."
    system = (f'A person heard the question "{question}" and answered out loud. '
              f"Map their answer to exactly one of {CHOICES[kind]}.{extra} "
              'If it is not clear, use "unclear". Reply with strict JSON only: '
              '{"answer": "..."}')

    def check(r):
        if r.get("answer") not in CHOICES[kind] + ["unclear"]:
            raise ValueError(f"bad answer {r.get('answer')}")
    out = ask_json(system, f'They said: "{text}"', check=check)
    a = out["reply"]["answer"]
    return (None if a == "unclear" else a), "voice -> Muse Spark Open", {
        "model": out["model"], "reasoning_content": out["reasoning"], "reply": out["reply"]}


class Session:
    def __init__(self, phrase, proposer="mayo", dry_run=False, log=print, source="cli", sid=None,
                 extra_busy=None):
        other = next(k for k in PEOPLE if k != proposer)
        self.people = [PEOPLE[proposer], PEOPLE[other]]
        self.by_device = {p["device_id"]: p for p in self.people}
        self.phrase, self.dry_run, self.sid, self._log = phrase, dry_run, sid, log
        self.now = datetime.now(TZ)
        self.lock = threading.RLock()
        self.state, self.questions, self.answers, self.confirms = "paired", {}, {}, {}
        self.deadline, self.options, self.proposal_i, self.change_round = None, [], 0, 0
        self.lines, self.result, self.notes, self.shared = [], {}, {}, []
        self.slot, self.backup, self.hold, self.offered, self.rejected = None, None, None, set(), []
        self.cons, self.last_change = {}, None
        # Synthetic busy blocks {person_key: [(start, end)]}, dry runs only (testing).
        assert not extra_busy or dry_run, "extra busy blocks are for dry runs only"
        self.extra_busy = extra_busy or {}
        self.trace = {"started_at": self.now.isoformat(), "source": source, "session_id": sid,
                      "input_phrase": phrase, "proposer": PEOPLE[proposer]["name"],
                      "dry_run": dry_run, "settings": SETTINGS,
                      "people": [{"name": p["name"], "device_id": p["device_id"]} for p in self.people],
                      "steps": [], "agent_turns": []}
        self.path = TRACES / f"{self.now:%Y-%m-%dT%H-%M-%S}-{source}.json"

    # ---- bookkeeping -------------------------------------------------------
    def say(self, text):
        self.lines.append(text)
        self._log(text)

    def step(self, name, **data):
        self.trace["steps"].append({"at": datetime.now(TZ).isoformat(timespec="seconds"),
                                    "state": self.state, "step": name, **data})
        self.save()

    def set_state(self, s):
        self.state = self.trace["state"] = s
        self.step("state", to=s)

    def save(self):
        TRACES.mkdir(exist_ok=True)
        self.path.write_text(json.dumps(self.trace, indent=2, ensure_ascii=False, default=str) + "\n")

    def fail(self, e):
        self.trace["error"] = self.result["error"] = f"{type(e).__name__}: {e}" if isinstance(e, Exception) else e
        self.questions = {}
        self.say(f"Failed: {self.result['error']}")
        self.set_state("failed")

    def ask(self, kind, text):
        """`text` is one string for both pebbles or {device_id: string}."""
        say = text if isinstance(text, dict) else {p["device_id"]: text for p in self.people}
        self.questions = {d: {"kind": kind, "say": t, "choices": CHOICES[kind]} for d, t in say.items()}
        self.step("ask", kind=kind, say=say, devices=list(self.questions))
        if len(set(say.values())) == 1:
            self.say(f'Both pebbles say: "{next(iter(say.values()))}"')
        else:
            for p in self.people:
                self.say(f'{p["name"]}\'s pebble says: "{say[p["device_id"]]}"')

    # ---- 1. start: plan, habits, nudge, ask morning/noon/night ----------------
    def start(self, ask=True):
        try:
            self.say(f'Heard: "{self.phrase}" (said by {self.people[0]["name"]})')
            parsed = parse_plan(self.phrase, self.now)
            self.plan = plan = parsed["plan"]
            self.trace.update(model=parsed["model"], model_reasoning=parsed["reasoning"],
                              model_errors=parsed["errors"], parsed_plan=plan)
            self.say(f"Plan ({parsed['model']}): {json.dumps(plan)}")
            self.cal = calendar()
            # Habits are read in the background (one Google client per thread)
            # while people answer; they're only needed for a missing answer.
            self._pool = ThreadPoolExecutor(2)
            self._infs = [self._pool.submit(infer_prefs, calendar(), p, self.now) for p in self.people]
            self.start_day = max(date.fromisoformat(plan["start_date"]), self.now.date())
            self.end_day = max(date.fromisoformat(plan["end_date"]), self.start_day)
            if self.start_day == self.end_day:
                when = f"{self.start_day:%A}"
            else:
                when = f"between {self.start_day:%a %b %-d} and {self.end_day:%a %b %-d}"
            nudge = f"{self.people[0]['name']} and {self.people[1]['name']} want to {plan['activity']} {when}!"
            self.result["nudge"] = nudge
            self.step("nudge", say=nudge)
            self.say(f"Nudge: {nudge}")
            if ask:
                self.set_state("asking_time_of_day")
                self.deadline = time.monotonic() + SETTINGS["answer_timeout_s"]
                self.ask("time_of_day", "Morning, noon, or night?")
        except Exception as e:
            self.fail(e)

    @property
    def profiles(self):
        if "inferred_profiles" not in self.trace:
            infs = [f.result() for f in self._infs]
            self._pool.shutdown(wait=False)
            self.trace["inferred_profiles"] = infs
            for i in infs:
                pr = i["profile"]
                self.say(f"  {i['person']}'s habits (inferred from own calendar, used only without an "
                         f"answer): {pr['time_of_day_preference']}, confidence {pr['confidence']}")
        return {i["person"]: i["profile"] for i in self.trace["inferred_profiles"]}

    # ---- answers from either pebble ----------------------------------------
    def answer(self, device_id, text):
        with self.lock:
            p, q = self.by_device.get(device_id), self.questions.get(device_id)
        if not p:
            return {"ok": False, "error": f"unknown device {device_id}"}
        if not q:
            return {"ok": False, "error": f"no open question for {device_id} (state {self.state})"}
        try:  # outside the lock: both pebbles' answers are understood in parallel
            if q["kind"] == "confirm":
                r = parse_change(text, self.slot[0], datetime.now(TZ).date(), question=Q_CHANGE)
                choice = {"yes": "change", "no": "keep"}.get(r["change"])
                how, detail, cons = r["how"], r["detail"], r["constraints"]
            else:
                (choice, how, detail), cons = map_answer(q["kind"], q["say"], text), None
        except Exception as e:
            return {"ok": False, "error": f"couldn't map answer: {e}"}
        with self.lock:
            if self.questions.get(device_id) is not q:
                return {"ok": False, "error": "question closed while the answer was being understood"}
            self.step("answer", person=p["name"], device_id=device_id, question=q["say"],
                      heard=text, mapped_to=choice, how=how, mapping=detail,
                      constraints=cons and describe(cons))
            self.say(f'  {p["name"]} said "{text}" -> {choice or "unclear, asking again"}')
            if not choice:
                return {"ok": False, "error": "unclear answer, question stays open", "say": q["say"]}
            (self.answers if q["kind"] == "time_of_day" else self.confirms)[p["name"]] = \
                {"choice": choice, "source": "voice", "heard": text, "constraints": cons}
            del self.questions[device_id]
            other = next(o["name"] for o in self.people if o is not p)
            if q["kind"] == "time_of_day":
                say = f"Got it, {choice}. I'll check with {other}'s agent."
            elif choice == "keep":
                say = (f"Great, keeping it. It's confirmed once {other} keeps it too, or "
                       f"{self.result['hold']['deadline_text']} if {other} doesn't reply.")
            else:
                say = f"Got it: {describe(cons)}. Give me a moment."
            return {"ok": True, "mapped_to": choice, "say": say,
                    "constraints": cons and describe(cons)}

    def advance(self, force=False):
        """Move on when every open question is answered (or timed out / forced)."""
        with self.lock:
            try:
                if self.state == "asking_time_of_day" and (
                        not self.questions or force or time.monotonic() >= self.deadline):
                    for dev in list(self.questions):
                        p = self.by_device[dev]
                        habit = self.profiles[p["name"]]["time_of_day_preference"]
                        choice = HABIT_TO_WINDOW.get(habit)
                        self.answers[p["name"]] = {"choice": choice, "source": "no answer: default "
                                                   f"from calendar habits ({habit})"}
                        self.step("default_answer", person=p["name"], habit=habit, choice=choice)
                        self.say(f"  {p['name']} didn't answer -> agent uses calendar habit: "
                                 f"{choice or 'no preference'}")
                    self.questions = {}
                    self.propose()
                elif self.state == "pencilled_in":
                    self.confirm_result()
            except Exception as e:
                self.fail(e)

    # ---- 3. agents share free windows and pick ------------------------------
    def propose(self, pick=None, note=""):
        self.set_state("proposing")
        plan, minutes = self.plan, self.plan["duration_min"]
        choices = [a["choice"] for a in self.answers.values() if a.get("choice")] if pick is None else []
        wanted = wanted_windows(choices)
        self.step("windows", answers=self.answers, preferred_windows=wanted)
        self.say(f"Preferred: {', '.join(f'{w} {hm(WINDOWS[w][0])}-{hm(WINDOWS[w][1])}' for w in wanted)}; "
                 f"other windows only if nothing fits (always 30 min clear of other events)")
        ids = [p["calendar_id"] for p in self.people]
        start, end = self.start_day, self.end_day
        for attempt in ("requested dates", "next week"):
            busy = freebusy(self.cal, ids, at(start, WINDOWS["morning"][0]).replace(hour=0),
                            at(end, WINDOWS["morning"][0]).replace(hour=0) + timedelta(days=1))
            for key, blocks in self.extra_busy.items():
                busy[PEOPLE[key]["calendar_id"]] = sorted(busy[PEOPLE[key]["calendar_id"]] + blocks)
            if self.extra_busy:
                self.say("  (dry run with synthetic busy blocks added: " + "; ".join(
                    f"{PEOPLE[k]['name']} {s:%a} {hm(s)}-{hm(e)}" for k, bl in self.extra_busy.items()
                    for s, e in bl) + ")")
            gone = {(t.date(), w) for t, w in self.rejected}  # a changed time rules out its window that day
            shared = [t for t in free_slots(busy, start, end, minutes, datetime.now(TZ))
                      if (t.date(), w_of(t)) not in gone and why_not(self.cons, t) is None]
            said = [a["choice"] for a in self.answers.values()
                    if a.get("choice") and a.get("source") == "voice"] if pick is None else []
            cands = (candidates(shared, minutes, choices, said=said, exclude=self.offered)
                     or candidates(shared, minutes, choices, said=said))
            ch = {p["name"]: self.answers.get(p["name"], {}).get("choice") for p in self.people}
            notes = {t: {p["name"]: fallback_note(t, w, ch[p["name"]], ch[o["name"]], shared)
                         for p, o in (self.people, self.people[::-1])} for t, w in cands}
            free = {p["name"]: free_windows(busy[p["calendar_id"]], start, end, self.now)
                    for p in self.people}
            self.step("share_free_times", attempt=attempt, dates=[str(start), str(end)],
                      free_windows={n: [f"{s:%a %b %-d} {hm(s)}-{hm(e)}" for s, e in w]
                                    for n, w in free.items()},
                      candidates=[{"slot": f"{fmt(t)} [{w}]", "notes": notes[t]} for t, w in cands])
            for n, w in free.items():
                self.say(f"  {n}'s agent shares free time: " + "; ".join(
                    f"{s:%a} {hm(s)}-{hm(e)}" for s, e in w) if w else f"  {n}'s agent: no free time")
            if cands:
                break
            self.say(f"Nothing fits {start} to {end}; looking at next week.")
            start = end + timedelta(days=7 - end.weekday())
            end = start + timedelta(days=6)
        if not cands:
            return self.fail("No shared time that fits, this week or next.")
        self.notes, self.shared = notes, shared
        self.say("Options both are free for, best fit first:")
        for i, (t, w) in enumerate(cands, 1):
            self.say(f"  {i}. {fmt(t)} [{w}]{self.note_text(t)}")
        if pick:
            self.options = [cands[pick - 1]]
            self.trace["agreement"] = {"how": f"manual override --pick {pick}"}
            return self.book()
        know = {p["name"]: {"answer": self.describe_answer(p["name"]),
                            "said": (self.answers.get(p["name"], {}).get("choice")
                                     if self.answers.get(p["name"], {}).get("source") == "voice" else None),
                            "profile": self.profiles[p["name"]], "free": free[p["name"]]}
                for p in self.people}
        self.say("Agents negotiating:")
        res = negotiate(self.people[0], self.people[1], plan["activity"], cands, busy,
                        minutes, know, log=self.say,
                        notes={t: self.note_text(t).strip(" ()") for t, _ in cands}, context_note=note)
        self.trace["agent_turns"] += res.pop("turns")
        self.trace.setdefault("agreements", []).append(res)
        self.offered |= {cands[i][0] for i in res["offered"]}
        self.slot = cands[res["agreed_index"]]
        self.backup = cands[res["backup_index"]] if res.get("backup_index") is not None else None
        if self.backup:
            self.offered.add(self.backup[0])
        self.step("agents_agreed", how=res["how"], slot=fmt(self.slot[0]),
                  backup=self.backup and fmt(self.backup[0]), backup_how=res.get("backup_how"))
        self.say(f"Agreed: {fmt(self.slot[0])}" + (f", backup {fmt(self.backup[0])} ({res['backup_how']})"
                                                  if self.backup else ", no backup left"))
        lead = {}
        if self.last_change:
            name, text, c = self.last_change
            f = followed(c, self.slot[0])
            lead = {p["device_id"]: (f"You said {spoken(c) or 'a different time'}, so I found a new time"
                                     + (f": {f}. " if f else ". ") if p["name"] == name else
                                     f"{name} said {spoken(c) or 'a different time'}, so we found a new time. ")
                    for p in self.people}
            self.last_change = None
        self.hold_and_ask(lead)

    def note_text(self, t):
        notes = {who: x for who, x in self.notes.get(t, {}).items() if x}
        if not notes:
            return ""
        kind = "fallback" if any(x.startswith("nothing worked") for x in notes.values()) else "compromise"
        return f" ({kind}, {'; '.join(f'{who}: {x}' for who, x in notes.items())})"

    def proposal_line(self, p, t):
        """What one pebble says, plain about any fallback."""
        q = f"how about {t:%a %b %-d} at {hm(t)}?"
        choice = self.answers.get(p["name"], {}).get("choice")
        note = self.notes.get(t, {}).get(p["name"])
        if note and note.startswith("nothing worked"):
            lead = f"Nothing worked at {choice} on {t:%A}, so {q}"
        elif note == "the other person's pick":
            other = next(o for o in self.people if o is not p)
            oa = self.answers[other["name"]]
            said = "picked" if oa["source"] == "voice" else "usually prefers"
            lead = f"{other['name']} {said} {oa['choice']}, so {q}"
        elif note == "middle ground":
            other = next(o for o in self.people if o is not p)
            oa = self.answers[other["name"]]
            said = "picked" if oa["source"] == "voice" else "usually prefers"
            lead = f"{other['name']} {said} {oa['choice']}, so to meet in the middle, {q}"
        else:
            lead = q[0].upper() + q[1:]
        return lead

    def describe_answer(self, name):
        a = self.answers.get(name, {})
        return f"{a.get('choice') or 'no preference'} ({a.get('source', 'not asked')})"

    # ---- 4. tentative hold, keep / change -----------------------------------
    def hold_and_ask(self, lead=None):
        t, w = self.slot
        now = datetime.now(TZ)
        if self.hold and self.hold["status"] == "tentative":
            self.hold = holds.move(self.cal, self.hold["id"], t, self.plan["duration_min"], now)
        else:
            self.hold = holds.create(self.cal, self.people, t, self.plan["duration_min"], self.plan["activity"],
                                     self.sid, self.trace["source"], self.dry_run, now)
            holds.LISTENERS[self.hold["id"]] = self.on_auto_confirmed
        dl = datetime.fromisoformat(self.hold["deadline"])
        self.result["hold"] = {"status": self.hold["status"], "slot": fmt(t), "deadline": dl.isoformat(),
                               "deadline_text": holds.deadline_text(dl, now),
                               "backup": self.backup and fmt(self.backup[0]),
                               "api_status": self.hold.get("api_status"), "event_ids": [
                                   e["event_id"] for e in self.hold["events"]]}
        self.result["proposal"] = {"slot": fmt(t), "window": w, "start": t.isoformat()}
        self.say(f"Pencilled in {fmt(t)} (tentative"
                 + (", dry run: Ketchup only" if self.dry_run else f", Google status {self.hold.get('api_status')}")
                 + f"); confirms {self.result['hold']['deadline_text']} unless someone changes it.")
        self.confirms = {}
        self.set_state("pencilled_in")
        when = self.result["hold"]["deadline_text"]
        self.ask("confirm", {p["device_id"]: f"{(lead or {}).get(p['device_id'], '')}{self.proposal_line(p, t)} "
                 f"I've pencilled it in. If I don't hear from you, it's confirmed {when}. {Q_CHANGE}"
                 for p in self.people})

    def confirm_result(self):
        changers = [n for n, c in self.confirms.items() if c["choice"] == "change"]
        if changers:
            return self.change(changers[0])
        if all(p["name"] in self.confirms for p in self.people):
            return self.confirm("both kept it")

    def change(self, name):
        c = self.confirms[name]
        text, cons = c["heard"].strip().rstrip(".!"), c["constraints"] or {}
        self.questions = {}  # the other pebble's question is withdrawn
        if self.change_round >= SETTINGS["max_change_rounds"]:
            self.say(f"{name} wants a change, but that's the most changes this time; keeping {fmt(self.slot[0])}.")
            return self.confirm("change limit reached")
        self.change_round += 1
        self.cons = merge(self.cons, cons)
        self.rejected.append(self.slot)
        self.step("change_requested", by=name, heard=text, constraints=describe(self.cons),
                  change_round=self.change_round)
        self.say(f'{name} wants a change (change {self.change_round} of {SETTINGS["max_change_rounds"]}): '
                 f'"{text}" -> {describe(cons)}')
        b = self.backup
        reason = "there is no backup left" if not b else why_not(self.cons, b[0])
        if reason is None:
            self.backup, self.slot = None, b
            f = followed(cons, b[0])
            self.say(f"  Backup {fmt(b[0])} fits ({f}): moving the hold there, no new negotiation.")
            said = spoken(cons) or "a different time"
            return self.hold_and_ask({p["device_id"]: (f"You said {said}, so I moved it to our backup"
                                                       + (f": {f}. " if f else ". ") if p["name"] == name else
                                                       f"{name} said {said}, so I moved it to our backup. ")
                                      for p in self.people})
        self.say(f"  Backup {'doesn' + chr(39) + 't fit: ' + reason if b else 'used up'}; renegotiating under it.")
        if self.hold and self.hold["status"] == "tentative":
            holds.LISTENERS.pop(self.hold["id"], None)
            rec = holds.release(self.cal, self.hold["id"])
            if rec["status"] == "confirmed":
                self.hold = rec
                return self.finish()
            self.trace.setdefault("deleted_events", []).extend(rec["events"])
        self.hold, self.backup, self.last_change = None, None, (name, text, cons)
        self.propose(note=f"{name} asked to change it: \"{text}\". Code already removed every slot that breaks "
                          f"it ({describe(self.cons)}) and every time offered before. Start your reason with how "
                          "you followed it, e.g. \"You said not Thursday, so ...\".")

    def confirm(self, why):
        holds.LISTENERS.pop(self.hold["id"], None)
        self.hold = holds.confirm(self.cal, self.hold["id"], why)
        self.finish()

    def on_auto_confirmed(self, rec):
        """Deadline passed (or skip to end of day): called by holds.sweep."""
        with self.lock:
            if self.state != "pencilled_in":
                return
            self.hold = rec
            self.say(f"Nobody changed it ({rec['why']}): confirmed.")
            self.finish()

    def finish(self):
        t, w = self.slot
        self.questions = {}
        self.trace["agreed_slot"] = self.result["agreed_slot"] = {"slot": fmt(t), "window": w,
                                                                  "start": t.isoformat()}
        self.result["hold"].update(status=self.hold["status"], why=self.hold.get("why"),
                                   api_status=self.hold.get("api_status"))
        other = {p["device_id"]: next(o["name"] for o in self.people if o is not p) for p in self.people}
        self.result["announce"] = {d: f"Plan booked with {n}." for d, n in other.items()}
        self.step("confirmed", hold=self.hold)
        if self.dry_run:
            self.say(f"Confirmed {fmt(t)} ({self.hold.get('why')}). Dry run: not on any calendar.")
            return self.set_state("agreed (dry run, not booked)")
        self.trace["created_events"] = self.result["created_events"] = self.hold["events"]
        self.say(f"Confirmed {fmt(t)} on both calendars ({self.hold.get('why')}), Google status "
                 f"{self.hold.get('api_status')}:")
        for c in self.hold["events"]:
            self.say(f"  {c['person']}: {c['event_id']} {c.get('link')}")
        self.set_state("booked")

    def book(self):
        """Manual override (--pick): book straight away, no hold."""
        t, w = self.options[0]
        self.trace["agreed_slot"] = self.result["agreed_slot"] = {"slot": fmt(t), "window": w,
                                                                  "start": t.isoformat()}
        if self.dry_run:
            self.say(f"Picked {fmt(t)}. Dry run: not booking.")
            return self.set_state("agreed (dry run, not booked)")
        created = book(self.cal, self.people, t, self.plan["duration_min"], self.plan["activity"])
        self.trace["created_events"] = self.result["created_events"] = created
        self.say(f"Booked {fmt(t)} on both calendars:")
        for c in created:
            self.say(f"  {c['person']}: {c['event_id']} {c['link']}")
        self.set_state("booked")

    def skip_to_end_of_day(self):
        """Demo: act as if the day ended now. Returns the confirmed records."""
        if not self.hold or self.hold["status"] != "tentative":
            return []
        return holds.sweep(calendar, hold_ids=[self.hold["id"]])

    # ---- what the pebbles poll ----------------------------------------------
    def public(self):
        with self.lock:
            return {"session_id": self.sid, "state": self.state, "phrase": self.phrase,
                    "dry_run": self.dry_run, "questions": self.questions,
                    "answers": self.answers, "confirms": self.confirms,
                    "change_round": self.change_round, **self.result,
                    # what each pebble should say now: its open question, else the last announcement
                    "speak": {p["device_id"]: (self.questions.get(p["device_id"], {}).get("say")
                                               or self.result.get("announce", {}).get(p["device_id"]))
                              for p in self.people},
                    "trace_file": str(self.path.relative_to(TRACES.parent)),
                    "log": self.lines[-40:]}
