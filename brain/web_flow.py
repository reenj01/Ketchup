"""The web page's version of a Ketchup session (web/index.html).

Three chats: Mayo <-> Mayo's agent (left), Frankie <-> Frankie's agent (right),
and the two agents talking to each other (middle). Everything is real: the
answers are mapped by Muse Spark Open, the agents negotiate with Muse Spark
Open (agents.py), slots come from both live calendars (calendar_slots.py).

Flow (RJ's wording):
1. Each agent asks its person "What time of day do you prefer to meet?"
   and "How long do you prefer to meet?" (typed now, spoken on the pebbles later).
2. Middle chat: the agents share free windows, settle the length, then
   negotiate the day and time.
3. Book it on both calendars right away (or not, in dry run), then each agent
   says "Frankie and you are free on <day> at <time>. I've booked it in your
   calendar. Do you want a different day or time?"
4. Anyone says yes -> delete the booked events, back to the middle chat for a
   new time (rejected times excluded), rebook, ask again. Both say no ->
   "Plan booked with <other>." At most max_change_rounds changes.

Unlike the pebble flow (flow.py: book only after both keep), this page books
first and asks after, as RJ described. All work runs on one worker thread
per session, so the Google client is never shared between threads.
"""
import json
import queue
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

from agents import fmt, negotiate, negotiate_length
from book import book
from calendar_slots import (SETTINGS, WINDOWS, at, candidates, fallback_note, free_slots, free_windows,
                            freebusy, window_of)
from flow import TRACES, hm, map_answer
from gcal import PEOPLE, calendar
from infer_prefs import infer_prefs
from parse_plan import TZ, ask_json, parse_plan

Q_TIME = "What time of day do you prefer to meet?"
Q_LEN = "How long do you prefer to meet?"
WINDOW_TEXT = ", ".join(f"{k} {hm(a)}-{hm(b)}" for k, (a, b) in WINDOWS.items())


def map_minutes(text):
    """Free text -> meeting length in minutes (15-240), or None if unclear."""
    t = text.strip().lower()
    if re.fullmatch(r"\d{1,3}", t):
        return int(t), "keyword", None
    system = ('A person was asked "How long do you prefer to meet?" and answered. '
              "Turn it into a meeting length in whole minutes between 15 and 240 "
              '("an hour" = 60, "half an hour" = 30, "quick" = 30). If it is not clear, use null. '
              'Reply with strict JSON only: {"minutes": integer or null}')

    def check(r):
        m = r.get("minutes", "missing")
        if m is not None and not (isinstance(m, int) and 15 <= m <= 240):
            raise ValueError(f"bad minutes {m}")
    out = ask_json(system, f'They said: "{text}"', check=check)
    return out["reply"]["minutes"], "Muse Spark Open", {"model": out["model"], "reply": out["reply"]}


def map_yes_no(question, text):
    t = text.strip().lower().strip(".!?")
    if t in ("yes", "no"):
        return t, "keyword", None
    system = (f'A person was asked "{question}" and answered. yes = they want a different '
              "day or time; no = they are happy with it. Map the answer to \"yes\", \"no\" "
              'or "unclear". Reply with strict JSON only: {"answer": "..."}')

    def check(r):
        if r.get("answer") not in ("yes", "no", "unclear"):
            raise ValueError("bad answer")
    out = ask_json(system, f'They said: "{text}"', check=check)
    a = out["reply"]["answer"]
    return (None if a == "unclear" else a), "Muse Spark Open", {"model": out["model"], "reply": out["reply"]}


class WebSession:
    def __init__(self, sid, phrase, dry_run=True, proposer="mayo"):
        other = next(k for k in PEOPLE if k != proposer)
        self.keys = [proposer, other]
        self.people = [PEOPLE[k] for k in self.keys]
        self.sid, self.phrase, self.dry_run = sid, phrase, dry_run
        self.now = datetime.now(TZ)
        self.lock = threading.Lock()
        self.messages, self.thinking, self.state = [], {}, "starting"
        self.open_q = {}                 # person key -> "time" | "length" | "change"
        self.answers = {k: {} for k in self.keys}
        self.booked, self.rejected, self.change_round = [], [], 0
        self.slot = None
        self.trace = {"started_at": self.now.isoformat(), "source": "web", "session_id": sid,
                      "input_phrase": phrase, "dry_run": dry_run, "settings": SETTINGS,
                      "steps": [], "agent_turns": [], "messages": self.messages}
        self.path = TRACES / f"{self.now:%Y-%m-%dT%H-%M-%S}-web-{sid}.json"
        self.jobs = queue.Queue()
        self.jobs.put(("start",))
        threading.Thread(target=self._worker, daemon=True).start()

    # ---- plumbing ----------------------------------------------------------
    def _worker(self):
        self.cal = calendar()  # this thread's own Google client
        while True:
            job = self.jobs.get()
            try:
                getattr(self, "_" + job[0])(*job[1:])
            except Exception as e:  # show it in the page instead of dying silently
                self.post("middle", "system", f"Something went wrong: {type(e).__name__}: {e}")
                self.set_state("failed")
            finally:
                self.thinking.clear()
            if self.state in ("done", "failed"):
                self.save()
                return

    def post(self, channel, who, text, **extra):
        with self.lock:
            self.messages.append({"i": len(self.messages), "channel": channel, "who": who,
                                  "text": text, "at": datetime.now(TZ).strftime("%-I:%M:%S %p"), **extra})
        self.save()

    def think(self, channel, label):
        self.thinking[channel] = label

    def done_thinking(self, channel):
        self.thinking.pop(channel, None)

    def step(self, name, **data):
        self.trace["steps"].append({"at": datetime.now(TZ).isoformat(timespec="seconds"), "step": name, **data})
        self.save()

    def set_state(self, s):
        self.state = self.trace["state"] = s
        self.step("state", to=s)

    def save(self):
        TRACES.mkdir(exist_ok=True)
        with self.lock:
            text = json.dumps(self.trace, indent=2, ensure_ascii=False, default=str)
        self.path.write_text(text + "\n")

    def name(self, k):
        return PEOPLE[k]["name"]

    def other(self, k):
        return self.keys[1] if k == self.keys[0] else self.keys[0]

    def public(self, since=0):
        with self.lock:
            return {"session_id": self.sid, "state": self.state, "dry_run": self.dry_run,
                    "people": {k: self.name(k) for k in self.keys},
                    "messages": self.messages[since:], "thinking": dict(self.thinking),
                    "waiting_for": {k: q for k, q in self.open_q.items()},
                    "slot": self.slot and fmt(self.slot[0]), "change_round": self.change_round,
                    "trace_file": str(self.path.relative_to(TRACES.parent))}

    # ---- called from HTTP threads -------------------------------------------
    def answer(self, key, text):
        if key not in self.keys:
            return {"ok": False, "error": f"unknown person {key}"}
        if not self.open_q.get(key):
            return {"ok": False, "error": f"{self.name(key)}'s agent isn't asking anything right now"}
        text = text.strip()
        if not text:
            return {"ok": False, "error": "empty answer"}
        self.post(key, "user", text)
        self.jobs.put(("handle_answer", key, text))
        return {"ok": True}

    # ---- the flow (worker thread only) --------------------------------------
    def _start(self):
        self.set_state("planning")
        self.post("middle", "system", f'Ketchup pressed: "{self.phrase}"'
                  + (" (dry run: calendars won't be touched)" if self.dry_run else " (real booking on)"))
        for ch in ("middle", *self.keys):
            self.think(ch, "reading calendars…")
        parsed = parse_plan(self.phrase, self.now)
        self.plan = parsed["plan"]
        self.trace.update(model=parsed["model"], parsed_plan=self.plan)
        with ThreadPoolExecutor(2) as ex:
            infs = list(ex.map(lambda p: infer_prefs(calendar(), p, self.now), self.people))
        self.trace["inferred_profiles"] = infs
        self.profiles = {i["person"]: i["profile"] for i in infs}
        self.start_day = max(date.fromisoformat(self.plan["start_date"]), self.now.date())
        self.end_day = max(date.fromisoformat(self.plan["end_date"]), self.start_day)
        when = (f"on {self.start_day:%A}" if self.start_day == self.end_day
                else f"between {self.start_day:%a %b %-d} and {self.end_day:%a %b %-d}")
        self.thinking.clear()
        self.post("middle", "system", f"Plan from Muse Spark Open: {self.plan['activity']} {when}.")
        for i in infs:
            pr = i["profile"]
            self.post("middle", "system", f"{i['person']}'s agent read {i['person']}'s calendar: "
                      f"usually {pr['time_of_day_preference']} ({pr['confidence']} confidence).")
        self.set_state("asking")
        for k in self.keys:
            self.post(k, "agent", f"Hi {self.name(k)}! You and {self.name(self.other(k))} want to "
                      f"{self.plan['activity']} {when}. {Q_TIME} (morning, noon, or night)")
            self.open_q[k] = "time"

    def _handle_answer(self, key, text):
        q = self.open_q.get(key)
        if not q:
            return
        self.think(key, "understanding…")
        if q == "time":
            choice, how, detail = map_answer("time_of_day", Q_TIME, text)
            self.step("answer", person=self.name(key), question=Q_TIME, heard=text, mapped_to=choice,
                      how=how, mapping=detail)
            self.done_thinking(key)
            if not choice:
                return self.post(key, "agent", f"Sorry, I didn't catch that. {Q_TIME} Morning, noon, or night?")
            self.answers[key]["time"] = choice
            a, b = WINDOWS[choice]
            self.post(key, "agent", f"Got it: {choice} ({hm(a)}-{hm(b)}). {Q_LEN}", mapped=choice)
            self.open_q[key] = "length"
        elif q == "length":
            minutes, how, detail = map_minutes(text)
            self.step("answer", person=self.name(key), question=Q_LEN, heard=text, mapped_to=minutes,
                      how=how, mapping=detail)
            self.done_thinking(key)
            if not minutes:
                return self.post(key, "agent", f"Sorry, I didn't catch that. {Q_LEN} (for example 30 min or 1 hour)")
            self.answers[key]["minutes"] = minutes
            self.open_q.pop(key)
            other = self.other(key)
            wait = "" if "minutes" in self.answers[other] else \
                f" I'll find a time with {self.name(other)}'s agent once {self.name(other)} answers."
            self.post(key, "agent", f"Got it: {minutes} min.{wait}", mapped=f"{minutes} min")
            if all("minutes" in self.answers[k] for k in self.keys):
                self._negotiate()
        elif q == "change":
            ask = "Do you want a different day or time?"
            choice, how, detail = map_yes_no(ask, text)
            self.step("answer", person=self.name(key), question=ask, heard=text, mapped_to=choice,
                      how=how, mapping=detail)
            self.done_thinking(key)
            if not choice:
                return self.post(key, "agent", f"Sorry, I didn't catch that. {ask} (yes or no)")
            self.answers[key]["change"] = choice
            self.open_q.pop(key)
            if choice == "yes":
                return self._change(key)
            if self.open_q:  # still waiting for the other person
                return self.post(key, "agent", f"Great, keeping it. Waiting on {self.name(self.other(key))}.",
                                 mapped="no")
            self._finish()

    def _negotiate(self, note=""):
        self.set_state("negotiating")
        mayo, frankie = self.people
        say = lambda k, r: self.post("middle", k, r)  # noqa: E731
        if not self.change_round:
            # 1. length
            wants = {self.name(k): self.answers[k]["minutes"] for k in self.keys}
            if len(set(wants.values())) > 1:
                self.post("middle", "system", "Lengths differ: " + ", ".join(f"{n} wants {m} min" for n, m in wants.items())
                          + ". The agents settle it first.")
            length = negotiate_length(
                mayo, frankie, self.plan["activity"], wants, log=lambda *_: None,
                before_turn=lambda me: self.think("middle", f"{me['name']}'s agent is thinking…"),
                on_turn=lambda me, r: say(self.key_of(me), f"{r['minutes']} min? {r['reason']}"
                                          if r["decision"] == "propose" else
                                          (f"OK, {r['minutes']} min. {r['reason']}" if r["decision"] == "accept"
                                           else f"How about {r['minutes']} min? {r['reason']}")))
            self.done_thinking("middle")
            self.trace["agent_turns"] += length.pop("turns")
            self.minutes = length["minutes"]
            self.trace["length_agreement"] = length
            self.post("middle", "system", f"Length: {self.minutes} min ({length['how']}).")
        # 2. free windows and slots
        self.think("middle", "checking both calendars…")
        ids = [p["calendar_id"] for p in self.people]
        start, end = self.start_day, self.end_day
        choices = [self.answers[k]["time"] for k in self.keys]
        for attempt in ("plan dates", "next week"):
            busy = freebusy(self.cal, ids, at(start, WINDOWS["morning"][0]).replace(hour=0),
                            at(end, WINDOWS["morning"][0]).replace(hour=0) + timedelta(days=1))
            # a rejected time rules out its whole window that day (no "3:30 instead of 3:00")
            gone = {(t.date(), w) for t, w in self.rejected}
            shared = [t for t in free_slots(busy, start, end, self.minutes, self.now)
                      if (t.date(), window_of(t)) not in gone]
            cands = candidates(shared, self.minutes, choices)
            if cands:
                break
            self.post("middle", "system", f"Nothing fits {start:%a %b %-d}-{end:%a %b %-d}; looking at next week.")
            start = end + timedelta(days=7 - end.weekday())
            end = start + timedelta(days=6)
        self.done_thinking("middle")
        if not cands:
            self.post("middle", "system", "No shared time left that fits, this week or next.")
            for k in self.keys:
                self.post(k, "agent", f"Sorry, I couldn't find another time that works for you and {self.name(self.other(k))}.")
            return self.set_state("failed")
        free = {p["name"]: free_windows(busy[p["calendar_id"]], start, end, self.now) for p in self.people}
        if not self.change_round:
            for k in self.keys:
                w = free[self.name(k)]
                ans = self.answers[k]
                say(k, f"{self.name(k)} wants {ans['time']}, {ans['minutes']} min. My free time (times only, "
                       "30 min clear of everything): " + ("; ".join(f"{s:%a} {hm(s)}-{hm(e)}" for s, e in w) or "none"))
        ch = {p["name"]: self.answers[k]["time"] for k, p in zip(self.keys, self.people)}
        notes = {t: {p["name"]: fallback_note(t, w, ch[p["name"]], ch[o["name"]], shared)
                     for p, o in (self.people, self.people[::-1])} for t, w in cands}
        label = {}
        for t, _ in cands:
            said = {n: x for n, x in notes[t].items() if x}
            if said:
                kind = "fallback" if any(x.startswith("nothing worked") for x in said.values()) else "compromise"
                label[t] = f"{kind}, " + "; ".join(f"{n}: {x}" for n, x in said.items())
        self.post("middle", "system", "Times both are free for, best fit first:\n" + "\n".join(
            f"{i}. {fmt(t)} [{w}]" + (f" ({label[t]})" if t in label else "")
            for i, (t, w) in enumerate(cands, 1)), kind="options")
        know = {p["name"]: {"answer": f"{self.answers[k]['time']} (typed by {p['name']})",
                            "profile": self.profiles[p["name"]], "free": free[p["name"]]}
                for k, p in zip(self.keys, self.people)}

        def on_turn(me, r, slot):
            t, w = slot
            if me is None:
                return self.post("middle", "system", f"Only one time fits: {fmt(t)}.")
            verb = {"propose": "How about", "accept": "Deal:", "counter": "What about"}[r["decision"]]
            self.post("middle", self.key_of(me), f"{verb} {fmt(t)} [{w}]? {r['reason']}"
                      if r["decision"] != "accept" else f"{verb} {fmt(t)}. {r['reason']}",
                      decision=r["decision"])
        res = negotiate(mayo, frankie, self.plan["activity"], cands, busy, self.minutes, know,
                        log=lambda *_: None, notes=label, on_turn=on_turn,
                        context_note=note,
                        before_turn=lambda me: self.think("middle", f"{me['name']}'s agent is thinking…"))
        self.done_thinking("middle")
        self.trace["agent_turns"] += res.pop("turns")
        self.trace.setdefault("agreements", []).append(res)
        if res["how"] == "best combined rank":
            self.post("middle", "system", "No deal after 2 rounds, so the best combined rank wins.")
        self.slot = cands[res["agreed_index"]]
        self.post("middle", "system", f"Agreed: {fmt(self.slot[0])} for {self.minutes} min ({res['how']}).",
                  kind="agreed")
        self._book_and_ask()

    def key_of(self, person):
        return next(k for k in self.keys if PEOPLE[k] is person)

    def _book_and_ask(self):
        t, w = self.slot
        if self.dry_run:
            self.booked = []
            self.post("middle", "system", "Dry run: not writing to either calendar.")
        else:
            self.think("middle", "booking…")
            self.booked = book(self.cal, self.people, t, self.minutes, self.plan["activity"])
            self.trace.setdefault("created_events", []).extend(self.booked)
            self.done_thinking("middle")
            self.post("middle", "system", f"Booked {fmt(t)} on both calendars.")
        self.step("booked", slot=fmt(t), minutes=self.minutes, events=self.booked, dry_run=self.dry_run)
        self.set_state("confirming")
        when = f"{t:%A, %b %-d} at {hm(t)}"
        for k in self.keys:
            note = " (Dry run: nothing was actually added.)" if self.dry_run else ""
            self.post(k, "agent", f"{self.name(self.other(k))} and you are free on {when}. "
                      f"I've booked it in your calendar.{note} Do you want a different day or time?")
            self.open_q[k] = "change"
            self.answers[k].pop("change", None)

    def _unbook(self):
        for e in self.booked:
            self.cal.events().delete(calendarId=e["calendar_id"], eventId=e["event_id"]).execute()
        if self.booked:
            self.post("middle", "system", f"Deleted the {fmt(self.slot[0])} events from both calendars.")
            self.trace.setdefault("deleted_events", []).extend(self.booked)
        self.booked = []

    def _change(self, key):
        other = self.other(key)
        if self.open_q.pop(other, None):
            self.post(other, "agent", f"{self.name(key)} asked for a different time, so I'm finding a new one.")
        if self.change_round >= SETTINGS["max_change_rounds"]:
            self.post(key, "agent", f"That's the most changes I can make this time, so the plan stays: "
                      f"{fmt(self.slot[0])}.")
            return self._finish()
        self.change_round += 1
        self.post(key, "agent", "OK, I'll find a different day or time.", mapped="yes")
        self.rejected.append(self.slot)
        self._unbook()
        self.post("middle", "system", f"{self.name(key)} wants a different day or time "
                  f"(change {self.change_round} of {SETTINGS['max_change_rounds']}). "
                  "Not offering again: " + ", ".join(f"{t:%a %b %-d} {w}" for t, w in self.rejected) + ".")
        self._negotiate(note=f"{self.name(key)} asked for a different day or time than "
                             ", ".join(f"{fmt(t)} ({t:%A} {w})" for t, w in self.rejected) + "; those windows are no longer offered.")

    def _finish(self):
        self.open_q.clear()
        for k in self.keys:
            self.post(k, "agent", f"Plan booked with {self.name(self.other(k))}."
                      + (" (Dry run: not on your calendar.)" if self.dry_run else ""), kind="final")
        self.post("middle", "system", f"Done: {fmt(self.slot[0])}, {self.minutes} min.", kind="final")
        self.set_state("done")
