"""'Change' takes a reason: turn the reply into hard constraints.

"not thursday, something later" -> {"not_days": [Thu dates], "after": <current slot>}.
A keyword fast path handles the common phrasings with no model call; anything
it can't fully account for goes to Muse Spark Open. Constraints are applied in
code as hard filters before ranking, so a slot that breaks what the person said
is never offered, whatever the agents prefer.

Constraint dict (JSON-able):
  not_days / only_days: ["YYYY-MM-DD"]      after / before: ISO datetime (later / earlier
  than the slot being changed)              after_time / before_time: "HH:MM" (later /
  earlier in the day)                       windows: allowed morning/noon/night
  minutes: new meeting length or None
"""
import re
from datetime import date, datetime, time, timedelta

from calendar_slots import ORDER, SETTINGS, WINDOWS, window_of
from lengths import clean
from parse_plan import ask_json

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
DAY = r"(mon|tues?|wed(?:nes)?|thu(?:rs?)?|fri|sat(?:ur)?|sun)(?:day)?s?"
WIN_WORDS = {"morning": "morning", "mornings": "morning", "afternoon": "noon", "afternoons": "noon",
             "lunch": "noon", "lunchtime": "noon", "midday": "noon", "noon": "noon",
             "evening": "night", "evenings": "night", "night": "night", "nights": "night",
             "tonight": "night", "after work": "night"}
WIN = "(" + "|".join(sorted(map(re.escape, WIN_WORDS), key=len, reverse=True)) + ")"
KEEP = r"\b(keep(?: it)?|that works|works(?: for me)?|perfect|sounds good|good|fine|great|all good|leave it|no thanks|nope|nah|no)\b"
# The only replies the fast path may read as "keep it": a bare no or a short affirmation.
# Anything else that sounds like keeping goes to the model, so a reason is never dropped.
_NO = ["", "no", "nope", "nah", "no thanks", "no thank you", "nah thanks", "nope thanks"]
_OK = ["", "keep it", "keep", "leave it", "sounds good", "perfect", "that works", "works", "works for me",
       "that works for me", "all good", "good", "fine", "great", "it's fine", "that's fine", "its fine",
       "thats fine", "it's perfect", "that's perfect", "that's good", "looks good", "i'm good", "im good",
       "we're good", "keep it please", "please keep it", "it works", "that's great"]
KEEP_EXACT = {f"{a} {b}".strip() + t for a in _NO for b in _OK for t in ("", " thanks")} - {"", " thanks"}
# "yeah that works" is keep; a bare "yes"/"ok" is not (yes = I want a different time)
KEEP_EXACT |= {f"{a} {b}{t}" for a in ("yes", "yeah", "yep", "yup", "ok", "okay", "sure")
               for b in _OK if b for t in ("", " thanks")}
KEEP_EXACT |= {f"{a} {b}" for a in _OK for b in _OK if a and b}  # "perfect, keep it"
# "that's not at night" describes the pencilled slot: it isn't at night, and they want night.
# A bare "not at night" is a request: anything but night.
SLOT_IS = r"\b(?:that's|thats|that is|it's|its|it is|this is|this one's)\s+not|\b(?:that|it|this)\s+(?:isn't|isnt|is not)"
# complaints with no direction: still a change
OBJECT = (r"\b(?:that |it |this )?(?:doesn't|doesnt|does not|won't|wont|will not|don't|dont)\s+work\b"
          r"|\b(?:i )?(?:can't|cant|cannot) (?:make (?:it|that)|do (?:it|that))\b|\bno good\b|\bnot good\b"
          r"|\bbad time\b|\bnot ideal\b|\b(?:i'm|im|i am) busy\b|\bbusy then\b|\b(?:i )?have a conflict\b"
          r"|\bconflict\b|\bthat's bad\b|\bnot great\b")
CHANGE = r"\b(change|different|another|move|switch|reschedule|other)\b"
FILLER = set("""yes yeah yep yup sure ok okay please pls maybe hmm hmmm um uh actually i i'd id would
could can we you let's lets do does go try make it move change switch something sometime some time
a bit little instead how what about better is be that the an different another other day one for me
to of rather prefer like if possible works work possibly perhaps then any thing anything please
reschedule on in at there this so just that's thats it's its""".split())

EMPTY = {"not_days": [], "only_days": [], "after": None, "before": None, "after_time": None,
         "before_time": None, "windows": [], "minutes": None}


def _dates_for(word, today, span=7 * SETTINGS["search_horizon_weeks"]):  # the whole search horizon
    name = next(d for d in DAYS if d.startswith(word[:3]))
    return [(today + timedelta(days=i)).isoformat() for i in range(span)
            if (today + timedelta(days=i)).weekday() == DAYS.index(name)]


def keyword(text, ref: datetime, today: date):
    """Returns (change, constraints) or None if some words aren't understood."""
    t = " " + text.lower().replace("’", "'") + " "
    c = {k: (list(v) if isinstance(v, list) else v) for k, v in EMPTY.items()}
    found = False
    bare = " ".join(re.sub(r"[^a-z' ]", " ", t).split())
    if bare in KEEP_EXACT:
        return "no", c

    def eat(pattern, fn):
        nonlocal t, found
        for m in list(re.finditer(pattern, t)):
            fn(m)
            found = True
        t = re.sub(pattern, " ", t)

    # "that's not at night" -> wants night (before the "not at night" rule below)
    eat(rf"(?:{SLOT_IS})\s+(?:in\s+the\s+|at\s+)?{WIN}\b",
        lambda m: c.update(windows=[WIN_WORDS[m[1]]]))
    if re.search(SLOT_IS, t):  # "that's not Thursday", "it's not what I meant": the model decides
        return None
    eat(r"\btoo (?:early|soon in the day)\b", lambda m: c.update(after_time=ref.strftime("%H:%M")))
    eat(r"\btoo late\b", lambda m: c.update(before_time=ref.strftime("%H:%M")))
    eat(r"\btoo soon\b", lambda m: c.update(after=ref.isoformat()))
    objection = False

    def obj(m):
        nonlocal objection
        objection = True
    eat(OBJECT, obj)
    eat(rf"\b(?:not|no|except|anything but|but not|can't do|cant do|cannot do|can't|cant|cannot)\s+(?:on\s+)?"
        rf"((?:{DAY})(?:\s*(?:,|or|and|nor)\s*(?:{DAY}))*)\b",
        lambda m: c["not_days"].extend(d for w in re.findall(DAY, m[1]) for d in _dates_for(w, today)))
    eat(rf"\b(?:not|no)\s+(?:in\s+the\s+|at\s+)?{WIN}\b",
        lambda m: c.update(windows=[w for w in (c["windows"] or ORDER) if w != WIN_WORDS[m[1]]]))
    eat(r"\blater (?:in|that) the day\b|\blater that day\b|\blater in the day\b",
        lambda m: c.update(after_time=ref.strftime("%H:%M")))
    eat(r"\bearlier (?:in|that) the day\b|\bearlier that day\b|\bearlier in the day\b",
        lambda m: c.update(before_time=ref.strftime("%H:%M")))
    eat(r"\blater(?: on| in the week)?\b", lambda m: c.update(after=ref.isoformat()))
    eat(r"\bearlier(?: in the week)?\b", lambda m: c.update(before=ref.isoformat()))
    eat(r"\btomorrow\b", lambda m: c["only_days"].append((today + timedelta(days=1)).isoformat()))
    eat(rf"\b(?:on\s+)?({DAY})\b", lambda m: c["only_days"].extend(_dates_for(m[1], today)))
    eat(rf"\b(?:in\s+the\s+|at\s+)?{WIN}\b",
        lambda m: c["windows"].append(WIN_WORDS[m[1]]) if WIN_WORDS[m[1]] not in c["windows"] else None)
    eat(r"\b(\d{2,3})\s*(?:m|min|mins|minutes)\b", lambda m: c.update(minutes=clean(m[1])))
    eat(r"\b(?:half an hour|half hour)\b", lambda m: c.update(minutes=30))
    eat(r"\b(?:an|one|1)\s+hour\b", lambda m: c.update(minutes=60))
    eat(r"\b(?:two|2)\s+hours\b", lambda m: c.update(minutes=120))
    plain = re.sub(r"[^a-z' ]", " ", t).split()
    keep = re.search(KEEP, t) is not None
    change = re.search(CHANGE, t) is not None
    left = [w for w in re.sub(KEEP, " ", re.sub(CHANGE, " ", t)).replace(",", " ").split()
            if re.sub(r"[^a-z']", "", w) and re.sub(r"[^a-z']", "", w) not in FILLER]
    if left:
        return None
    if found or objection:  # any reason or complaint, even after a "no" ("no, can we do later"), is a change
        return "yes", c
    if plain in (["yes"], ["yeah"], ["yep"], ["yup"], ["sure"]):
        return "yes", c
    if change and not keep:
        return "yes", c
    return None  # keep only for the exact short replies above; the rest goes to the model


MODEL_SYSTEM = """A person has a meeting pencilled in and was asked "{question}" They answered.
Decide if they want to change it, and turn any reason they gave into constraints.
The current slot: {ref:%A %Y-%m-%d %-I:%M %p} America/New_York. Today: {today:%A %Y-%m-%d}.
Dates: {dates}.
Windows: {windows}.
Reply with strict JSON only:
{{"change": "yes" | "no" | "unclear",
  "not_days": ["YYYY-MM-DD", ...]   (days they can't do),
  "only_days": ["YYYY-MM-DD", ...]  (if they named the day(s) they want),
  "later": bool, "earlier": bool    (later / earlier than the current slot),
  "later_in_day": bool, "earlier_in_day": bool  (a later / earlier time of day, any day),
  "windows": [...]                  (only the windows they'd accept; [] = any),
  "minutes": integer or null        (a new meeting length if they asked for one)}}
Rules:
- It's a yes/no question, so a bare "no", "no thanks", "nah it's fine", "keep it", "sounds good" mean keep it: "no".
- ANY complaint, objection or stated preference means change: "yes", even if the reply starts
  with "no" or contains "not" ("no, can we do later", "too early", "that doesn't work", "evening please").
- "That's not at night" / "it's not in the morning" describe the pencilled time: it isn't at night,
  and they WANT night, so windows = ["night"]. A bare "not at night" means they don't want night:
  windows = the other two.
- If you can't tell, "unclear". Never guess "no"."""


def model(text, ref, today, question):
    dates = ", ".join(f"{today + timedelta(days=i):%a}={today + timedelta(days=i)}" for i in range(14))
    windows = ", ".join(f"{k} {a:%H:%M}-{b:%H:%M}" for k, (a, b) in WINDOWS.items())

    def check(r):
        if r.get("change") not in ("yes", "no", "unclear"):
            raise ValueError("bad change")
        for k in ("not_days", "only_days"):
            [date.fromisoformat(d) for d in r.get(k) or []]
        if any(w not in ORDER for w in r.get("windows") or []):
            raise ValueError("bad window")
        m = r.get("minutes")
        if m is not None and not (isinstance(m, int) and 15 <= m <= 240):
            raise ValueError("bad minutes")
    out = ask_json(MODEL_SYSTEM.format(question=question, ref=ref, today=today, dates=dates, windows=windows),
                   f'They said: "{text}"', check=check)
    r = out["reply"]
    c = dict(EMPTY, not_days=r.get("not_days") or [], only_days=r.get("only_days") or [],
             windows=r.get("windows") or [], minutes=r.get("minutes") and clean(r["minutes"]),
             after=ref.isoformat() if r.get("later") else None,
             before=ref.isoformat() if r.get("earlier") else None,
             after_time=ref.strftime("%H:%M") if r.get("later_in_day") else None,
             before_time=ref.strftime("%H:%M") if r.get("earlier_in_day") else None)
    change = None if r["change"] == "unclear" else r["change"]
    if any(c[k] for k in c):  # a reason was given: that is a change, whatever the yes/no said
        change = "yes"
    return change, c, {"model": out["model"], "reply": r}


def parse_change(text, ref: datetime, today: date, question="Do you want a different day or time?"):
    """Returns {"change": "yes"|"no"|None, "constraints", "how", "detail"}."""
    k = keyword(text, ref, today)
    if k:
        return {"change": k[0], "constraints": k[1], "how": "keyword", "detail": None}
    change, c, detail = model(text, ref, today, question)
    return {"change": change, "constraints": c, "how": "Muse Spark Open", "detail": detail}


def active(c):
    return bool(c) and any(c.get(k) for k in EMPTY)


def merge(old, new):
    """Constraints from earlier changes still hold unless the new reply overrides them."""
    if not old:
        return dict(new)
    out = dict(old)
    # newest wins: a day asked for now is no longer ruled out
    out["not_days"] = sorted((set(old["not_days"]) - set(new["only_days"])) | set(new["not_days"]))
    for k in ("only_days", "windows"):
        if new[k]:
            out[k] = new[k]
    if out.get("only_days"):  # a newer "not Friday" drops Friday from an older "only Friday"
        out["only_days"] = [d for d in out["only_days"] if d not in new["not_days"]]
    for k in ("after", "before", "after_time", "before_time", "minutes"):
        if new[k] is not None:
            out[k] = new[k]
    # a newer bound that contradicts an older opposite bound replaces it
    for lo, hi in (("after", "before"), ("after_time", "before_time")):
        if out.get(lo) and out.get(hi) and out[lo] >= out[hi]:
            out[hi if new[lo] is not None else lo] = None
    return out


def why_not(c, t: datetime, minutes=None):
    """None if slot t satisfies every constraint, else the first reason it doesn't."""
    if not c:
        return None
    d = t.date().isoformat()
    if d in c["not_days"]:
        return f"it's on {t:%A}"
    if c["only_days"] and d not in c["only_days"]:
        return f"it's not on {_days(c['only_days'])}"
    if c["after"] and t <= datetime.fromisoformat(c["after"]):
        return f"it's not later than {datetime.fromisoformat(c['after']):%a %-I:%M %p}"
    if c["before"] and t >= datetime.fromisoformat(c["before"]):
        return f"it's not earlier than {datetime.fromisoformat(c['before']):%a %-I:%M %p}"
    if c["after_time"] and t.time() <= time.fromisoformat(c["after_time"]):
        return f"it's not later in the day than {_hm(c['after_time'])}"
    if c["before_time"] and t.time() >= time.fromisoformat(c["before_time"]):
        return f"it's not earlier in the day than {_hm(c['before_time'])}"
    if c["windows"] and window_of(t) not in c["windows"]:
        return f"it's {AT[window_of(t)]}, not {' or '.join(AT[w] for w in c['windows'])}"
    if c["minutes"] and minutes and c["minutes"] != minutes:
        return f"it's {minutes} min, not {c['minutes']}"
    return None


def ok(c, t, minutes=None):
    return why_not(c, t, minutes) is None


def _hm(s):
    return datetime.strptime(s, "%H:%M").strftime("%-I:%M %p")


def _days(isos):
    days = sorted({date.fromisoformat(d).weekday() for d in isos})
    return " or ".join(DAYS[d].capitalize() for d in days)


def describe(c):
    """Plain words for the agents and the trace: 'not Thursday; later than Thu 10:30 AM'."""
    if not active(c):
        return "no constraints"
    parts = []
    if c["not_days"]:
        parts.append(f"not {_days(c['not_days'])}")
    if c["only_days"]:
        parts.append(f"on {_days(c['only_days'])}")
    if c["after"]:
        parts.append(f"later than {datetime.fromisoformat(c['after']):%a %b %-d %-I:%M %p}")
    if c["before"]:
        parts.append(f"earlier than {datetime.fromisoformat(c['before']):%a %b %-d %-I:%M %p}")
    if c["after_time"]:
        parts.append(f"later in the day than {_hm(c['after_time'])}")
    if c["before_time"]:
        parts.append(f"earlier in the day than {_hm(c['before_time'])}")
    if c["windows"]:
        parts.append(" or ".join(c["windows"]) + " only")
    if c["minutes"]:
        parts.append(f"{c['minutes']} min")
    return "; ".join(parts)


AT = {"morning": "in the morning", "noon": "at noon", "night": "at night"}


def followed(c, t: datetime):
    """How slot t follows the constraints, for 'You said ..., so ...'."""
    out = []
    if c["not_days"] or c["only_days"]:
        out.append(f"it's on {t:%A}")
    if c["after"] or c["after_time"]:
        out.append("it's later")
    if c["before"] or c["before_time"]:
        out.append("it's earlier")
    if c["windows"]:
        out.append(f"it's {AT[window_of(t)]}")
    if c["minutes"]:
        out.append(f"it's {c['minutes']} min")
    return ", ".join(out) or "it's a different time"


def spoken(c):
    """The reason as the person might have put it, for 'You said not Thursday, so ...'."""
    if not active(c):
        return None
    parts = []
    if c["not_days"]:
        parts.append(f"not {_days(c['not_days'])}")
    if c["only_days"]:
        parts.append(_days(c["only_days"]))
    if c["after"]:
        parts.append("something later")
    if c["before"]:
        parts.append("something earlier")
    if c["after_time"]:
        parts.append("later in the day")
    if c["before_time"]:
        parts.append("earlier in the day")
    if c["windows"]:
        parts.append("the " + " or ".join(c["windows"]))
    if c["minutes"]:
        parts.append(f"{c['minutes']} minutes")
    return ", ".join(parts)
