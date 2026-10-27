"""What happens after two pebbles tap: a small state machine.

paired -> asking_time_of_day -> proposing -> confirming -> booked | failed
(with --dry-run, "booked" becomes "agreed (dry run, not booked)").

1. Nudge: "Mayo and Frankie want to catch up Thursday."
2. Both pebbles ask "Morning, noon, or night?". Each person answers by voice.
   No answer before the timeout: their agent uses the habit it inferred from
   their calendar.
3. The agents share each other's free windows (times only) and pick a slot
   that respects both answers. Nothing fits: look at next week.
4. Both pebbles ask "How about Thu Oct 29 at 12:00 PM? Want to change it?"
   Both say keep -> book. Anyone says change -> next-best option, at most 2 times.

Answers are voice transcripts; Muse Spark Open maps them to a choice. A plain
keyword ("night", "keep") is accepted directly, for testing.
"""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

from agents import fmt, fmt_windows, negotiate
from book import book
from calendar_slots import SETTINGS, WINDOWS, at, candidates, free_slots, free_windows, freebusy
from gcal import PEOPLE, calendar
from infer_prefs import infer_prefs
from parse_plan import TZ, ask_json, parse_plan

TRACES = Path(__file__).parent.parent / "traces"
ORDER = list(WINDOWS)  # morning, noon, night
HABIT_TO_WINDOW = {"morning": "morning", "afternoon": "noon", "evening": "night"}
CHOICES = {"time_of_day": ORDER, "confirm": ["keep", "change"]}
DONE = ("booked", "agreed (dry run, not booked)", "failed")


def hm(t):
    return t.strftime("%-I:%M %p")


WINDOW_TEXT = ", ".join(f"{k} {hm(a)}-{hm(b)}" for k, (a, b) in WINDOWS.items())


def map_answer(kind, question, text):
    """Voice transcript -> one of CHOICES[kind], or None if unclear.
    Returns (choice, how, model_detail)."""
    t = text.strip().lower().strip(".!?")
    if t in CHOICES[kind]:
        return t, "keyword", None
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


def wanted_windows(choices):
    """Windows to search: the ones people asked for plus any in between
    (morning + night -> noon too, as a middle ground). No answers: all."""
    if not choices:
        return list(ORDER)
    idx = sorted(ORDER.index(c) for c in choices)
    return ORDER[idx[0]: idx[-1] + 1]


class Session:
    def __init__(self, phrase, proposer="mayo", dry_run=False, log=print, source="cli", sid=None):
        other = next(k for k in PEOPLE if k != proposer)
        self.people = [PEOPLE[proposer], PEOPLE[other]]
        self.by_device = {p["device_id"]: p for p in self.people}
        self.phrase, self.dry_run, self.sid, self._log = phrase, dry_run, sid, log
        self.now = datetime.now(TZ)
        self.lock = threading.RLock()
        self.state, self.questions, self.answers, self.confirms = "paired", {}, {}, {}
        self.deadline, self.options, self.proposal_i, self.change_round = None, [], 0, 0
        self.lines, self.result = [], {}
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
        self.questions = {p["device_id"]: {"kind": kind, "say": text, "choices": CHOICES[kind]}
                          for p in self.people}
        self.step("ask", kind=kind, say=text, devices=list(self.questions))
        self.say(f'Both pebbles say: "{text}"')

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
            with ThreadPoolExecutor(2) as ex:
                infs = list(ex.map(lambda p: infer_prefs(calendar(), p, self.now), self.people))  # one client per thread
            self.trace["inferred_profiles"] = infs
            self.profiles = {i["person"]: i["profile"] for i in infs}
            for i in infs:
                pr = i["profile"]
                self.say(f"  {i['person']}'s habits (inferred from own calendar): "
                         f"{pr['time_of_day_preference']}, confidence {pr['confidence']}")
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

    # ---- answers from either pebble ----------------------------------------
    def answer(self, device_id, text):
        with self.lock:
            p, q = self.by_device.get(device_id), self.questions.get(device_id)
            if not p:
                return {"ok": False, "error": f"unknown device {device_id}"}
            if not q:
                return {"ok": False, "error": f"no open question for {device_id} (state {self.state})"}
            try:
                choice, how, detail = map_answer(q["kind"], q["say"], text)
            except Exception as e:
                return {"ok": False, "error": f"couldn't map answer: {e}"}
            self.step("answer", person=p["name"], device_id=device_id, question=q["say"],
                      heard=text, mapped_to=choice, how=how, mapping=detail)
            self.say(f'  {p["name"]} said "{text}" -> {choice or "unclear, asking again"}')
            if not choice:
                return {"ok": False, "error": "unclear answer, question stays open", "say": q["say"]}
            (self.answers if q["kind"] == "time_of_day" else self.confirms)[p["name"]] = \
                {"choice": choice, "source": "voice", "heard": text}
            del self.questions[device_id]
            return {"ok": True, "mapped_to": choice}

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
                elif self.state == "confirming" and not self.questions:
                    self.confirm_result()
            except Exception as e:
                self.fail(e)

    # ---- 3. agents share free windows and pick ------------------------------
    def propose(self, pick=None):
        self.set_state("proposing")
        plan, minutes = self.plan, self.plan["duration_min"]
        choices = [a["choice"] for a in self.answers.values() if a.get("choice")]
        wanted = wanted_windows(choices) if pick is None else list(ORDER)
        self.step("windows", answers=self.answers, search_windows=wanted)
        self.say(f"Searching {', '.join(f'{w} {hm(WINDOWS[w][0])}-{hm(WINDOWS[w][1])}' for w in wanted)} "
                 f"(always 30 min clear of other events)")
        ids = [p["calendar_id"] for p in self.people]
        start, end = self.start_day, self.end_day
        for attempt in ("requested dates", "next week"):
            busy = freebusy(self.cal, ids, at(start, WINDOWS["morning"][0]).replace(hour=0),
                            at(end, WINDOWS["morning"][0]).replace(hour=0) + timedelta(days=1))
            shared = free_slots(busy, start, end, minutes, self.now)
            cands = candidates(shared, minutes, wanted)
            free = {p["name"]: free_windows(busy[p["calendar_id"]], start, end, self.now)
                    for p in self.people}
            self.step("share_free_times", attempt=attempt, dates=[str(start), str(end)],
                      free_windows={n: [f"{s:%a %b %-d} {hm(s)}-{hm(e)}" for s, e in w]
                                    for n, w in free.items()},
                      candidates=[f"{fmt(t)} [{w}]" for t, w in cands])
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
        self.say("Options both are free for:")
        for i, (t, w) in enumerate(cands, 1):
            self.say(f"  {i}. {fmt(t)} [{w}]")
        if pick:
            self.options = [cands[pick - 1]]
            self.trace["agreement"] = {"how": f"manual override --pick {pick}"}
            return self.book()
        know = {p["name"]: {"answer": self.describe_answer(p["name"]),
                            "profile": self.profiles[p["name"]], "free": free[p["name"]]}
                for p in self.people}
        self.say("Agents negotiating:")
        res = negotiate(self.people[0], self.people[1], plan["activity"], cands, busy,
                        minutes, know, log=self.say)
        self.trace["agent_turns"] += res.pop("turns")
        self.trace["agreement"] = res
        self.options = [cands[i] for i in res["ranked"]]
        self.step("agents_agreed", how=res["how"], options_in_order=[f"{fmt(t)} [{w}]" for t, w in self.options])
        self.proposal_i = 0
        self.ask_confirm()

    def describe_answer(self, name):
        a = self.answers.get(name, {})
        return f"{a.get('choice') or 'no preference'} ({a.get('source', 'not asked')})"

    # ---- 4. keep / change --------------------------------------------------
    def ask_confirm(self):
        t, w = self.options[self.proposal_i]
        self.result["proposal"] = {"slot": fmt(t), "window": w, "start": t.isoformat()}
        self.confirms = {}
        self.set_state("confirming")
        self.ask("confirm", f"How about {t:%a %b %-d} at {hm(t)}? Want to change it?")

    def confirm_result(self):
        changers = [n for n, c in self.confirms.items() if c["choice"] == "change"]
        if not changers:
            return self.book()
        self.change_round += 1
        self.step("change_requested", by=changers, change_round=self.change_round)
        if self.change_round > SETTINGS["max_change_rounds"] or self.proposal_i + 1 >= len(self.options):
            return self.fail(f"No time both kept after {self.change_round - 1} changes. Try again later.")
        self.proposal_i += 1
        self.say(f"{' and '.join(changers)} {'want' if len(changers) > 1 else 'wants'} a change (change {self.change_round} of "
                 f"{SETTINGS['max_change_rounds']}): next-best option.")
        self.ask_confirm()

    def book(self):
        t, w = self.options[self.proposal_i]
        self.trace["agreed_slot"] = self.result["agreed_slot"] = {"slot": fmt(t), "window": w,
                                                                  "start": t.isoformat()}
        if self.dry_run:
            self.say(f"Both kept {fmt(t)}. Dry run: not booking.")
            return self.set_state("agreed (dry run, not booked)")
        created = book(self.cal, self.people, t, self.plan["duration_min"], self.plan["activity"])
        self.trace["created_events"] = self.result["created_events"] = created
        self.say(f"Booked {fmt(t)} on both calendars:")
        for c in created:
            self.say(f"  {c['person']}: {c['event_id']} {c['link']}")
        self.set_state("booked")

    # ---- what the pebbles poll ----------------------------------------------
    def public(self):
        with self.lock:
            return {"session_id": self.sid, "state": self.state, "phrase": self.phrase,
                    "dry_run": self.dry_run, "questions": self.questions,
                    "answers": self.answers, "confirms": self.confirms,
                    "change_round": self.change_round, **self.result,
                    "trace_file": str(self.path.relative_to(TRACES.parent)),
                    "log": self.lines[-40:]}
