"""The web page's version of a Ketchup session (web/index.html).

Three chats: Mayo <-> Mayo's agent (left), Frankie <-> Frankie's agent (right),
and the two agents talking to each other (middle). Everything is real: the
answers are mapped by Muse Spark Open, the agents negotiate with Muse Spark
Open (agents.py), slots come from both live calendars (calendar_slots.py).

Flow (RJ's wording):
1. Each agent asks its person "What time of day do you prefer to meet?"
   and "How long do you prefer to meet?" (typed now, spoken on the pebbles later).
2. Middle chat: the agents share free windows, settle the length, then
   negotiate the day and time, and a backup slot in the same turns.
3. Pencil it in on both calendars as a tentative hold (holds.py) and ask
   "Do you want a different day or time?". "No" = kept. Both kept -> confirmed
   now. Nobody answers -> confirmed at the deadline (10 PM ET, 11:59 PM if held after 10 PM, or
   30 min before it starts if sooner); "Skip to end of day" runs that now.
4. "Yes / not Thursday, something later" -> the reason becomes hard
   constraints (constraints.py). If the backup fits them, the hold moves
   there right away and both are asked again; otherwise the hold is removed
   and the agents renegotiate under the constraint (offered times never come
   back). No cap on changes: it keeps finding new times, widening week by
   week up to search_horizon_weeks, until both keep one.

Same rules as the pebble flow (flow.py). All work runs on one worker thread
per session, so the Google client is never shared between threads.
"""
import json
import queue
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

from agents import fmt, negotiate, negotiate_length
from lengths import clean, fix_text
from privacy import Neutral, finding
import holds
from constraints import describe, followed, merge, parse_change, why_not
from calendar_slots import (SETTINGS, WINDOWS, at, candidates, fallback_note, free_slots, free_windows,
                            freebusy, window_of)
from flow import TRACES, hm, map_answer
from gcal import PEOPLE, calendar
from infer_prefs import infer_prefs
from parse_plan import TZ, ask_json, parse_plan
from timing import CALLS, summary

Q_TIME = "What time of day do you prefer to meet?"
Q_LEN = "How long do you prefer to meet?"
HABITS_LABEL = "reading calendar habits…"
WINDOW_TEXT = ", ".join(f"{k} {hm(a)}-{hm(b)}" for k, (a, b) in WINDOWS.items())


HOURS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3}


def map_minutes(text):
    """Free text -> meeting length in minutes, or None if unclear. Always a
    multiple of 5, rounded down ("37 minutes" -> 35, "1 hr 22" -> 80)."""
    res = _map_minutes(text)
    return (clean(res[0]) if res[0] else res[0]), res[1], res[2]


def _map_minutes(text):
    t = text.strip().lower().strip(".!?")
    if re.fullmatch(r"\d{1,3}", t):
        return int(t), "keyword", None
    m = re.fullmatch(r"(\d{1,3}(?:\.\d+)?)\s*(m|min|mins|minutes?)", t)
    if m and 10 <= float(m[1]) <= 240:
        return float(m[1]), "keyword", None
    m = re.fullmatch(r"(\d(?:\.\d+)?|an?|one|two|three)\s*(?:h|hr|hrs|hours?)"
                     r"(?:\s*(?:and\s*)?(\d{1,2})\s*(?:m|min|mins|minutes?)?)?", t)
    if m:
        h = HOURS.get(m[1]) or float(m[1])
        return h * 60 + int(m[2] or 0), "keyword", None
    if t in ("half an hour", "half hour", "30m"):
        return 30, "keyword", None
    system = ('A person was asked "How long do you prefer to meet?" and answered. '
              "Turn it into a meeting length in whole minutes between 15 and 240, "
              "rounded down to a multiple of 5 "
              '("an hour" = 60, "half an hour" = 30, "quick" = 30). If it is not clear, use null. '
              'Reply with strict JSON only: {"minutes": integer or null}')

    def check(r):
        m = r.get("minutes", "missing")
        if m is not None and not (isinstance(m, int) and 15 <= m <= 240):
            raise ValueError(f"bad minutes {m}")
    out = ask_json(system, f'They said: "{text}"', check=check)
    return out["reply"]["minutes"], "Muse Spark Open", {"model": out["model"], "reply": out["reply"]}


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
        self.rejected, self.offered, self.change_round = [], set(), 0
        self.mapping = set()             # person keys whose answer is being understood
        self.slot = None
        self.hold, self.backup, self.kept = None, None, set()  # tentative hold record, backup slot, who kept it
        self.cons, self.last_change = {}, None   # merged change constraints; (key, text, constraints)
        self.relaxed = None  # a said window with nothing in the horizon, so other windows were used
        self.trace = {"started_at": self.now.isoformat(), "source": "web", "session_id": sid,
                      "input_phrase": phrase, "dry_run": dry_run, "settings": SETTINGS,
                      "steps": [], "agent_turns": [], "messages": self.messages}
        self.path = TRACES / f"{self.now:%Y-%m-%dT%H-%M-%S}-web-{sid}.json"
        self.calls_from = len(CALLS)
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
            finally:  # keep only indicators for work still running on other threads
                for ch in list(self.thinking):
                    if ch not in self.mapping and self.thinking[ch] != HABITS_LABEL:
                        self.thinking.pop(ch, None)
            if self.state in ("done", "failed"):
                self.save()
                return

    def post(self, channel, who, text, **extra):
        if who != "user":
            text = fix_text(text)  # lengths shown are always clean multiples of 5
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

    def calls(self):
        return [c for c in CALLS[self.calls_from:] if c.get("sid", self.sid) == self.sid]

    def save(self):
        TRACES.mkdir(exist_ok=True)
        self.trace["timing"] = {"summary": summary(self.calls()), "calls": self.calls()}
        with self.lock:
            text = json.dumps(self.trace, indent=2, ensure_ascii=False, default=str)
        self.path.write_text(text + "\n")

    def name(self, k):
        return PEOPLE[k]["name"]

    def other(self, k):
        return self.keys[1] if k == self.keys[0] else self.keys[0]

    def public(self, since=0):
        with self.lock:
            return {"session_id": self.sid, "state": self.state, "dry_run": self.dry_run, "phrase": self.phrase,
                    "people": {k: self.name(k) for k in self.keys},
                    "messages": self.messages[since:], "thinking": dict(self.thinking),
                    "waiting_for": {k: q for k, q in self.open_q.items()},
                    "slot": self.slot and fmt(self.slot[0]), "change_round": self.change_round,
                    "hold": self.hold and {
                        "status": self.hold["status"], "slot": fmt(datetime.fromisoformat(self.hold["start"])),
                        "deadline_text": self.deadline_text(), "why": self.hold.get("why"),
                        "api_status": self.hold.get("api_status"), "kept": [self.name(k) for k in self.kept]},
                    "backup": self.backup and fmt(self.backup[0]),
                    "trace_file": str(self.path.relative_to(TRACES.parent)),
                    "timing": summary(self.calls())}

    # ---- called from HTTP threads -------------------------------------------
    def answer(self, key, text):
        if key not in self.keys:
            return {"ok": False, "error": f"unknown person {key}"}
        q = self.open_q.get(key)
        if not q or key in self.mapping:
            return {"ok": False, "error": f"{self.name(key)}'s agent isn't asking anything right now"}
        text = text.strip()
        if not text:
            return {"ok": False, "error": "empty answer"}
        self.post(key, "user", text)
        self.mapping.add(key)
        self.think(key, "understanding…")
        # Understanding an answer is model-only (no Google client), so each
        # person's answer is mapped on its own thread, in parallel.
        threading.Thread(target=self._map, args=(key, q, text), daemon=True).start()
        return {"ok": True}

    def _map(self, key, q, text):
        try:
            if q == "time":
                res = map_answer("time_of_day", Q_TIME, text)
            elif q == "length":
                res = map_minutes(text)
            else:
                res = parse_change(text, self.slot[0], datetime.now(TZ).date())
        except Exception as e:  # noqa: BLE001 - surfaced on the worker
            res = e
        self.jobs.put(("handle_answer", key, text, q, res))

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
        # Each agent reads its person's calendar habits in the background
        # (own Google client per thread) while people answer the questions.
        self._pool, self._inferred = ThreadPoolExecutor(2), 0
        self._infs = [self._pool.submit(self._infer, p) for p in self.people]
        self.start_day = max(date.fromisoformat(self.plan["start_date"]), self.now.date())
        self.end_day = max(date.fromisoformat(self.plan["end_date"]), self.start_day)
        when = (f"on {self.start_day:%A}" if self.start_day == self.end_day
                else f"between {self.start_day:%a %b %-d} and {self.end_day:%a %b %-d}")
        self.thinking.clear()
        self.post("middle", "system", f"Plan from Muse Spark Open: {self.plan['activity']} {when}.")
        self.think("middle", HABITS_LABEL)
        self.set_state("asking")
        for k in self.keys:
            self.post(k, "agent", f"Hi {self.name(k)}! You and {self.name(self.other(k))} want to "
                      f"{self.plan['activity']} {when}. {Q_TIME} (morning, noon, or night)")
            self.open_q[k] = "time"

    def _infer(self, person):
        i = infer_prefs(calendar(), person, self.now)
        pr = i["profile"]
        self.post("middle", "system", f"{i['person']}'s agent read {i['person']}'s calendar: usually "
                  f"{pr['time_of_day_preference']} ({pr['confidence']} confidence). Used only if "
                  f"{i['person']} gives no answer; what {i['person']} says comes first.")
        with self.lock:
            self._inferred += 1
            if self._inferred == len(self.people) and self.thinking.get("middle") == HABITS_LABEL:
                self.thinking.pop("middle")
        return i

    def profiles_ready(self):
        if not hasattr(self, "profiles"):
            infs = [f.result() for f in self._infs]
            self._pool.shutdown(wait=False)
            self.trace["inferred_profiles"] = infs
            self.profiles = {i["person"]: i["profile"] for i in infs}
        return self.profiles

    def _handle_answer(self, key, text, q, res):
        self.mapping.discard(key)
        self.done_thinking(key)
        if isinstance(res, Exception):
            raise res
        if self.open_q.get(key) != q:  # question was withdrawn meanwhile
            return
        if q == "time":
            choice, how, detail = res
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
            minutes, how, detail = res
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
            choice, cons = res["change"], res["constraints"]
            self.step("answer", person=self.name(key), question=ask, heard=text, mapped_to=choice,
                      constraints=describe(cons), how=res["how"], mapping=res["detail"])
            if not choice:
                self.open_q[key] = "change"  # never default to keep: ask once more
                return self.post(key, "agent", "Sorry, I didn't catch that. Should I keep it or find another time? "
                                 "(say keep it, or what you'd like instead, e.g. \"not Thursday, later\")")
            self.answers[key]["change"] = choice
            self.open_q.pop(key)
            if choice == "yes" or not self.hold:  # no time on the table: any reply is a new ask
                return self._change(key, text, cons, res["how"])
            self.kept.add(key)
            if all(k in self.kept for k in self.keys):
                return self._confirm("both kept it")
            o = self.name(self.other(key))
            self.post(key, "agent", f"Great, keeping it. It's confirmed once {o} keeps it too, or "
                      f"{self.deadline_text()} if {o} doesn't reply.", mapped="keep")

    def _negotiate(self, note=""):  # noqa: C901 - one readable flow
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
        cons, self.relaxed = self.cons, None
        for week in range(2 * SETTINGS["search_horizon_weeks"]):
            if week == SETTINGS["search_horizon_weeks"]:
                # the window the person said beats everything; other windows only if it has nothing
                if not (cons and cons.get("windows")):
                    break
                self.relaxed = " or ".join(cons["windows"])
                cons = dict(cons, windows=[])
                start, end = self.start_day, self.end_day
                self.post("middle", "system", f"No {self.relaxed} time free for both in the next "
                          f"{SETTINGS['search_horizon_weeks']} weeks, so looking at other times of day.")
            busy = freebusy(self.cal, ids, at(start, WINDOWS["morning"][0]).replace(hour=0),
                            at(end, WINDOWS["morning"][0]).replace(hour=0) + timedelta(days=1))
            # a rejected time rules out its whole window that day (no "3:30 instead of 3:00")
            gone = {(t.date(), w) for t, w in self.rejected}
            # what the person said when changing is a hard filter, before any ranking
            shared = [t for t in free_slots(busy, start, end, self.minutes, datetime.now(TZ))
                      if (t.date(), window_of(t)) not in gone and why_not(cons, t) is None]
            # times the agents already offered in an earlier round are not offered again
            # never a repeat: if everything here was offered, widen the range instead
            cands = candidates(shared, self.minutes, choices, said=choices, exclude=self.offered)
            if cands:
                break
            nxt = end + timedelta(days=7 - end.weekday())
            if week + 1 == SETTINGS["search_horizon_weeks"]:
                continue
            self.post("middle", "system", f"Nothing left {start:%a %b %-d}-{end:%a %b %-d}, so looking at "
                      f"{'next week' if not week else f'the week of {nxt:%b %-d}'}.")
            start, end = nxt, nxt + timedelta(days=6)
        self.done_thinking("middle")
        if not cands:  # nothing within the horizon: ask again, still no cap
            n = SETTINGS["search_horizon_weeks"]
            self.post("middle", "system", f"No shared time found in the next {n} weeks.")
            for k in self.keys:
                self.post(k, "agent", f"I couldn't find a shared time with {self.name(self.other(k))} in the next "
                          f"{n} weeks. What day, time or length would work instead?")
                self.open_q[k] = "change"
                self.answers[k].pop("change", None)
            return self.set_state("held")
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
        profiles = self.profiles_ready()
        know = {p["name"]: {"answer": f"{self.answers[k]['time']} (typed by {p['name']})",
                            "said": self.answers[k]["time"],
                            "profile": profiles[p["name"]], "free": free[p["name"]]}
                for k, p in zip(self.keys, self.people)}

        def on_turn(me, r, slot):
            t, w = slot
            if me is None:
                return self.post("middle", "system", f"Only one time fits: {fmt(t)}.")
            verb = {"propose": "How about", "accept": "Deal:", "counter": "What about"}[r["decision"]]
            b = r.get("backup") and cands[r["backup"] - 1][0]
            back = f" Backup: {fmt(b)}." if b else ""
            self.post("middle", self.key_of(me), (f"{verb} {fmt(t)} [{w}]?{back} {r['reason']}"
                      if r["decision"] != "accept" else f"{verb} {fmt(t)}.{back} {r['reason']}").strip(),
                      decision=r["decision"])
        res = negotiate(mayo, frankie, self.plan["activity"], cands, busy, self.minutes, know,
                        log=lambda *_: None, notes=label, on_turn=on_turn,
                        context_note=note + (f" There is no {self.relaxed} time free for both in the search range, so the options are other times of day; say so." if self.relaxed else ""),
                        before_turn=lambda me: self.think("middle", f"{me['name']}'s agent is thinking…"))
        self.done_thinking("middle")
        self.trace["agent_turns"] += res.pop("turns")
        self.trace.setdefault("agreements", []).append(res)
        self.offered |= {cands[i][0] for i in res["offered"]}
        if res["how"] == "best combined rank":
            self.post("middle", "system", "No deal after 2 rounds, so the best combined rank wins.")
        self.slot = cands[res["agreed_index"]]
        self.backup = cands[res["backup_index"]] if res.get("backup_index") is not None else None
        if self.backup:
            self.offered.add(self.backup[0])
        self.post("middle", "system", f"Agreed: {fmt(self.slot[0])} for {self.minutes} min ({res['how']})."
                  + (f" Backup: {fmt(self.backup[0])} ({res['backup_how']})." if self.backup else " No backup left."),
                  kind="agreed")
        self._hold_and_ask(self._change_lead())

    def _change_lead(self):
        """After a renegotiated change: the requester's chat echoes their own
        words, the other chat gets only a template (never the words or reason)."""
        if not self.last_change:
            return {}
        k, text, c = self.last_change
        self.last_change = None
        n = SETTINGS["search_horizon_weeks"]
        got = (f"there's no {self.relaxed} time free for both of you in the next {n} weeks, so this is the "
               f"closest" if self.relaxed else followed(c, self.slot[0]))
        return {k: f"You said \u201c{text}\u201d, so I found a new time: {got}. ",
                self.other(k): Neutral(self.name(k))}

    def key_of(self, person):
        return next(k for k in self.keys if PEOPLE[k] is person)

    def deadline_text(self):
        return holds.deadline_text(datetime.fromisoformat(self.hold["deadline"]),
                                   datetime.fromisoformat(self.hold["made_at"]))

    def _hold_and_ask(self, lead=None):
        """Pencil the slot in on both calendars (tentative) and ask both people."""
        t, w = self.slot
        now = datetime.now(TZ)
        self.think("middle", "pencilling it in…")
        if self.hold and self.hold["status"] == "tentative":
            self.hold = holds.move(self.cal, self.hold["id"], t, self.minutes, now)
            what = "Moved the tentative hold to"
        else:
            self.hold = holds.create(self.cal, self.people, t, self.minutes, self.plan["activity"],
                                     self.sid, "web", self.dry_run, now)
            holds.LISTENERS[self.hold["id"]] = lambda rec: self.jobs.put(("auto_confirmed", rec))
            what = "Pencilled in"
        self.done_thinking("middle")
        if self.hold["status"] != "tentative":  # confirmed meanwhile (skip to end of day)
            return self._finish()
        if self.dry_run:
            self.post("middle", "system", f"{what} {fmt(t)} (dry run: the hold lives in Ketchup only, "
                      "nothing written to either calendar).", kind="hold")
        else:
            self.trace.setdefault("created_events", []).extend(
                e for e in self.hold["events"] if e not in self.trace.get("created_events", []))
            self.post("middle", "system", f"{what} {fmt(t)} on both calendars: Google event status "
                      f"\"{', '.join(sorted(set(self.hold.get('api_status') or [])))}\", title "
                      f"\"{self.hold['title']}{holds.MARK}\".", kind="hold")
        dt = self.deadline_text()
        self.post("middle", "system", f"Tentative, confirms {dt} unless someone changes it.", kind="hold")
        self.step("hold", slot=fmt(t), minutes=self.minutes, hold=self.hold, dry_run=self.dry_run,
                  backup=self.backup and fmt(self.backup[0]))
        self.set_state("held")
        when = f"{t:%A, %b %-d} at {hm(t)}"
        self.kept = set()
        for k in self.keys:
            note = " (Dry run: not on your real calendar.)" if self.dry_run else ""
            head = (lead or {}).get(k, "")
            head = (f"{head.text(when)} " if isinstance(head, Neutral)
                    else f"{head}{self.name(self.other(k))} and you are free on {when}. ")
            self.post(k, "agent", f"{head}"
                      f"I've pencilled it in your calendar (tentative).{note} If I don't hear from you, "
                      f"it's confirmed {dt}. Do you want a different day or time?", kind="hold")
            self.open_q[k] = "change"
            self.answers[k].pop("change", None)

    def _change(self, key, text, cons, how):
        other = self.other(key)
        if self.open_q.pop(other, None):
            self.post(other, "agent", finding(self.name(key)))
        self.change_round += 1  # no cap: keep finding times until both keep one
        self.cons = merge(self.cons, cons)
        cur = self.slot
        self.rejected.append(cur)
        self.post(key, "agent", f"OK, I'll find a different day or time: {describe(cons)}." if describe(cons) !=
                  "no constraints" else "OK, I'll find a different day or time.", mapped="change")
        self.post("middle", "system", f"{self.name(key)} wants a different day or time (change {self.change_round}): \u201c{text}\u201d \u2192 {describe(cons)} (read by {how}). "
                  "Not offering again: " + ", ".join(f"{t:%a %b %-d %-I:%M %p}" for t in sorted(self.offered)) + ".")
        b = self.backup
        minutes = self.cons.get("minutes") or self.minutes
        if not b:
            reason = "there is no backup left"
        elif minutes != self.minutes:
            reason = f"it was agreed for {self.minutes} min"
        else:
            reason = why_not(self.cons, b[0], self.minutes) or self._still_free(b[0])
        if reason is None:  # 2. backup right away, no new negotiation
            self.backup, self.slot = None, b
            f = followed(cons, b[0])
            self.post("middle", key, f"{self.name(key)} said \u201c{text}\u201d. Our backup {fmt(b[0])} fits "
                      f"({f}), so I'm moving the hold there. No new negotiation needed.")
            return self._hold_and_ask({
                key: f"You said \u201c{text}\u201d, so I moved it to our backup: {f}. ",
                other: Neutral(self.name(key), "backup")})
        # 3. backup used up or breaks the reason: renegotiate under the constraint
        self.post("middle", "system", (f"Backup {fmt(b[0])} doesn't fit: {reason}." if b else "No backup left.")
                  + " The agents renegotiate; slots that break it are filtered out in code.")
        if self.hold and self.hold["status"] == "tentative":
            holds.LISTENERS.pop(self.hold["id"], None)
            rec = holds.release(self.cal, self.hold["id"])
            if rec["status"] == "confirmed":  # skipped to end of day meanwhile
                self.hold = rec
                return self._finish()
            if not self.dry_run:
                self.post("middle", "system", f"Removed the tentative {fmt(cur[0])} hold from both calendars.")
                self.trace.setdefault("deleted_events", []).extend(rec["events"])
        self.hold, self.backup, self.minutes = None, None, minutes
        self.last_change = (key, text, cons)
        self._negotiate(note=f"{self.name(key)} asked to change {fmt(cur[0])}: \u201c{text}\u201d. Code already "
                             f"removed every slot that breaks it ({describe(self.cons)}) and every time offered "
                             "before. Start your reason with how you followed it, e.g. \"You said not Thursday, "
                             "so ...\".")

    def _still_free(self, t):
        """Re-check the backup on both calendars, ignoring our own hold."""
        if self.dry_run:
            return None
        ids = [p["calendar_id"] for p in self.people]
        busy = freebusy(self.cal, ids, t - timedelta(hours=2), t + timedelta(minutes=self.minutes, hours=2))
        hs = datetime.fromisoformat(self.hold["start"])
        he = hs + timedelta(minutes=self.hold["minutes"])
        blocks = []
        for bl in busy.values():
            for s, e in bl:  # cut our own hold out of each busy block
                blocks += [(a, b) for a, b in ((s, min(e, hs)), (max(s, he), e)) if a < b]
        return None if t in free_slots({"x": blocks}, t.date(), t.date(), self.minutes, t) else "it's no longer free"

    def _confirm(self, why):
        holds.LISTENERS.pop(self.hold["id"], None)
        self.hold = holds.confirm(self.cal, self.hold["id"], why)
        self._finish()

    def _auto_confirmed(self, rec):
        if self.state in ("done", "failed"):
            return
        self.hold = rec
        when = ("Skipped to end of day" if rec["why"] == "skipped to end of day"
                else f"Deadline passed ({self.deadline_text()})")
        self.post("middle", "system", f"{when}: nobody changed it, so the hold is confirmed.")
        self._finish()

    def skip_to_end_of_day(self):
        """Demo button: act as if the day ended now (HTTP thread)."""
        if not self.hold or self.hold["status"] != "tentative":
            return {"ok": False, "error": "nothing is pencilled in right now"}
        holds.sweep(calendar, hold_ids=[self.hold["id"]])
        return {"ok": True}

    def _finish(self):
        self.open_q.clear()
        t = self.slot[0]
        if self.hold and not self.dry_run:
            self.post("middle", "system", f"Confirmed on both calendars: Google event status "
                      f"\"{', '.join(sorted(set(self.hold.get('api_status') or [])))}\", title "
                      f"\"{self.hold['title']}\" ({self.hold.get('why')}).", kind="final")
        for k in self.keys:
            self.post(k, "agent", f"Plan booked with {self.name(self.other(k))}: {t:%A, %b %-d} at {hm(t)}."
                      + (" (Dry run: not on your calendar.)" if self.dry_run else ""), kind="final")
        self.post("middle", "system", f"Done: {fmt(t)}, {self.minutes} min"
                  + (f" ({self.hold.get('why')})." if self.hold else "."), kind="final")
        self.set_state("done")
