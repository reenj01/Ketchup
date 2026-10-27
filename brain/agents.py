"""One Muse Spark Open agent per person, and the negotiation between them.

Each agent knows its own person's calendar (busy times, no titles shared),
what its person answered to "Morning, noon, or night?" (habits inferred from
its own calendar only when the person gave no answer), and the other person's free windows (shared by the other
agent: times only, no event details).
"""
import json

from lengths import STEP, clean, fix_text, middle
from parse_plan import ask_json

MAX_ROUNDS = 2

SYSTEM = """You are {name}'s personal scheduling agent inside Ketchup.
{name} and {other} just agreed to "{activity}". The numbered slots below are
times when BOTH are free, each already at least 30 minutes away from any other
event on either calendar, best first. "fallback" = nothing worked that day in
the window someone asked for.

What they answered to "Morning, noon, or night?":
- {name}: {my_answer}
- {other}: {their_answer}
{habits}{other}'s free time, shared by {other}'s agent (times only):
{their_free}

Reply with strict JSON only, no prose, no code fences:
{{"ranking": [slot numbers, best for {name} first, every slot exactly once],
  "decision": "propose" | "accept" | "counter",
  "pick": slot number you want,
  "backup": a second slot number (not the pick), ideally another day, used
            right away if someone wants to change the plan,
  "reason": "one short sentence, friendly, under 20 words"}}
Rules, most important first:
- What a person said in this chat is the top priority. It always beats
  calendar habits and any "middle ground". Never pick a window nobody said
  while a slot in a window someone said is listed.
- Going first: propose a slot in {name}'s window. If both said the same
  window, stay in it. If they said different windows, settle on a slot in one
  of the two windows they said, taking turns fairly.
- Never propose a slot that was already offered (listed in the message).
  Accept it, or counter with a slot nobody offered yet.
- An answer marked "no answer" was NOT said; never claim they asked for it.
- If you pick a fallback slot, say so plainly, e.g. "Nothing worked at noon,
  so how about <time>?" Use the real slot time.
- Backup: when you propose or counter, name a backup too. When you accept,
  keep the other agent's backup if it works for {name}, or name a better one
  nobody offered as a pick. Mention the backup briefly in your reason.
- If the message says someone asked for a change, start your reason with how
  you followed what they said, e.g. "You said not Thursday, so ...".
- "propose" only when you go first. Otherwise "accept" (pick = the other
  agent's pick) or "counter" (pick = a new slot). Settle quickly: in the last
  round, accept any fair slot in a window someone said."""

HABITS = ("{name} gave no answer, so use {name}'s habits, inferred by Ketchup from "
          "{name}'s own calendar: {habit}\n")


def fmt(t):
    return t.strftime("%a %b %-d, %-I:%M %p ET")


def fmt_windows(wins):
    return "\n".join(f"  {s:%a %b %-d} {s:%-I:%M %p}-{e:%-I:%M %p}" for s, e in wins) or "  none"


def calendar_context(busy, slots, minutes, notes=None):
    """Plain-text view of one person's own calendar around each slot."""
    from datetime import timedelta
    days = sorted({t.date() for t, _ in slots})
    lines = []
    for d in days:
        blocks = [f"{s:%-I:%M %p}-{e:%-I:%M %p}" for s, e in busy if s.date() == d]
        lines.append(f"{d:%a %b %-d} busy: {', '.join(blocks) or 'nothing'}")
    lines.append("Shared slots (gap = free time before / after on your own calendar):")
    for i, (t, window) in enumerate(slots, 1):
        end = t + timedelta(minutes=minutes)
        before = max([e for _, e in busy if e <= t and e.date() == t.date()], default=None)
        after = min([s for s, _ in busy if s >= end and s.date() == t.date()], default=None)
        gb = f"{int((t - before).total_seconds() // 60)} min" if before else "open"
        ga = f"{int((after - end).total_seconds() // 60)} min" if after else "open"
        note = (notes or {}).get(t)
        lines.append(f"  {i}. {fmt(t)} [{window}{' - ' + note if note else ''}] ({minutes} min)"
                     f" - gap before: {gb}, after: {ga}")
    return "\n".join(lines)


def _checker(n, first, offered):
    """Shape of one reply (retried by ask_json when wrong)."""
    def check(r):
        if sorted(r.get("ranking", [])) != list(range(1, n + 1)):
            raise ValueError(f"ranking must list 1..{n} once: {r.get('ranking')}")
        if r.get("pick") not in range(1, n + 1):
            raise ValueError(f"bad pick {r.get('pick')}")
        if n > 1 and (r.get("backup") not in range(1, n + 1) or r["backup"] == r["pick"]):
            raise ValueError(f"backup must be a slot 1..{n} other than the pick: {r.get('backup')}")
        want = {"propose"} if first else {"accept", "counter"}
        if r.get("decision") not in want:
            raise ValueError(f"decision must be one of {want}")
        if r["decision"] == "accept" and r["pick"] != offered:
            raise ValueError("accept must pick the offered slot")
        if r["decision"] == "counter" and r["pick"] == offered:
            raise ValueError("counter must pick a different slot")
        if not isinstance(r.get("reason"), str) or not r["reason"].strip():
            raise ValueError("missing reason")
    return check


def allowed_picks(slots, seen, mine, theirs, first):
    """Slot numbers a new proposal / counter may use, enforced in code:
    never one already offered; going first, {name}'s own said window when
    possible; otherwise a window someone said when possible."""
    fresh = [i for i in range(1, len(slots) + 1) if i not in seen]
    for wins in ([mine] if first and mine else [], [w for w in (mine, theirs) if w]):
        ok = [i for i in fresh if slots[i - 1][1] in wins]
        if ok:
            return ok
    return fresh


def fix_backup(r, slots, seen, mine, theirs):
    """The backup must be a slot never offered as a pick, not the pick itself,
    in a window someone said when possible, and preferably on another day.
    Returns (backup, why moved or None). Code moves it; no extra model call."""
    if len(slots) < 2:
        return None, None
    ok = [i for i in allowed_picks(slots, list(seen) + [r["pick"]], mine, theirs, False) if i != r["pick"]]
    if r.get("backup") in ok:
        return r["backup"], None
    if not ok:
        return None, "no slot left for a backup"
    day = slots[r["pick"] - 1][0].date()
    ranked = [i for i in r["ranking"] if i in ok]
    best = next((i for i in ranked if slots[i - 1][0].date() != day), ranked[0])
    return best, f"backup {r.get('backup')} was already offered or breaks the window rule"


def problem(r, allowed):
    """Why a reply breaks the code rules, or None."""
    if r["decision"] == "accept" or r["pick"] in allowed:
        return None
    return (f"Slot {r['pick']} is not allowed: it was already offered or is in a window "
            f"nobody said. Allowed slots: {allowed}. Pick one of those, or accept.")


def agent_turn(me, other, activity, know, context, round_no, last=None, n=3, context_note="",
               slots=None, seen=(), offered_by=None):
    """One agent move. `know[name]` = {"answer", "said", "profile", "free"}
    ("said" = the window they explicitly chose, None if they gave no answer);
    `last` is the other agent's latest {pick, reason} or None; `seen` = slot
    numbers already offered by either agent."""
    mine, theirs = know[me["name"]].get("said"), know[other["name"]].get("said")
    habits = ""
    if not mine:  # habits only matter when the person gave no answer
        pr = know[me["name"]]["profile"] or {}
        habits = HABITS.format(name=me["name"], habit=json.dumps(
            {k: pr.get(k) for k in ("time_of_day_preference", "preferred_hours", "confidence")}))
    system = SYSTEM.format(
        name=me["name"], other=other["name"], activity=activity,
        my_answer=know[me["name"]]["answer"], their_answer=know[other["name"]]["answer"],
        habits=habits, their_free=fmt_windows(know[other["name"]]["free"]))
    user = f"{me['name']}'s own calendar:\n{context}\n\n" + (f"{context_note}\n\n" if context_note else "")
    if seen:
        user += ("Already offered (never propose these again): " + ", ".join(
            f"slot {i} by {(offered_by or {}).get(i, 'an agent')}" for i in sorted(seen)) + "\n")
    user += f"Round {round_no} of {MAX_ROUNDS}. "
    if last is None:
        user += "You go first. Propose the slot you want."
        summary = f"{me['name']}'s agent proposes first from {n} shared slots"
    else:
        user += (f"{other['name']}'s agent wants slot {last['pick']}"
                 + (f" (backup: slot {last['backup']})" if last.get("backup") else "")
                 + f": \"{last['reason']}\"\n"
                 "Accept it or counter with a slot nobody offered yet.")
        summary = (f"{me['name']}'s agent answers {other['name']}'s pick of slot "
                   f"{last['pick']} (round {round_no})")
    allowed = allowed_picks(slots, seen, mine, theirs, last is None) if slots else list(range(1, n + 1))
    check = _checker(n, last is None, last and last["pick"])
    out = ask_json(system, user, check=check)
    turn = {"round": round_no, "agent": me["name"], "prompt_summary": summary,
            "prompt": {"system": system, "user": user}, "allowed_picks": allowed,
            "model": out["model"], "model_errors": out["errors"],
            "reasoning_content": out["reasoning"], "reply": out["reply"]}
    why = problem(out["reply"], allowed)
    if why:  # one retry that says what was wrong, then code moves the pick
        turn["rejected_reply"], turn["rejected_because"] = out["reply"], why
        out = ask_json(system, user + "\n\n" + why, check=check)
        turn.update(model=out["model"], reasoning_content=out["reasoning"], reply=out["reply"])
        turn["model_errors"] += out["errors"]
        if problem(out["reply"], allowed):
            r = dict(out["reply"])
            nxt = next((i for i in r["ranking"] if i in allowed), allowed[0] if allowed else None)
            if nxt is None and last:  # nothing new left: take the other's pick
                r.update(decision="accept", pick=last["pick"])
            else:
                r.update(pick=nxt, reason="")
            turn["reply"], turn["moved_by_code"] = r, why
    if slots:
        r = dict(turn["reply"])
        r["backup"], moved = fix_backup(r, slots, seen, mine, theirs)
        if moved:
            turn["backup_moved_by_code"] = moved
        turn["reply"] = r
    return turn


def negotiate(proposer, other, activity, slots, busy_by_cal, minutes, know, log=print, notes=None,
              on_turn=None, context_note="", before_turn=None):
    """Proposer's agent picks first, the other accepts or counters, up to
    MAX_ROUNDS rounds; then the best combined rank wins. Each proposal names a
    backup too; the accepting agent keeps it or names its own (same turns, no
    extra call), and code checks it was never offered as a pick. No slot is ever
    offered twice (checked in code, see allowed_picks).
    `slots` = [(start, window)]. Returns {"agreed_index", "how", "turns",
    "ranked", "offered"} where `ranked` lists every slot index, agreed first,
    then next-best, and `offered` = indexes either agent proposed."""
    n = len(slots)
    if n == 1:
        log("  Only one shared slot, nothing to negotiate.")
        if on_turn:
            on_turn(None, {"decision": "only", "pick": 1, "reason": "Only one shared slot, nothing to negotiate."}, slots[0])
        return {"agreed_index": 0, "how": "only one shared slot", "turns": [], "ranked": [0], "offered": [0],
                "backup_index": None, "backup_how": "no other shared slot"}
    ctx = {p["name"]: calendar_context(busy_by_cal[p["calendar_id"]], slots, minutes, notes)
           for p in (proposer, other)}
    turns, rankings, last, agreed, how, backup, backup_how = [], {}, None, None, None, None, None
    seen, by = [], {}
    order = [proposer, other]
    for round_no in range(1, MAX_ROUNDS + 1):
        for i, me in enumerate(order):
            if before_turn:
                before_turn(me)
            t = agent_turn(me, order[1 - i], activity, know, ctx[me["name"]], round_no, last, n,
                           context_note, slots=slots, seen=seen, offered_by=by)
            turns.append(t)
            r = t["reply"]
            rankings[me["name"]] = r["ranking"]
            log(f"  [round {round_no}] {me['name']}'s agent {r['decision']}s slot {r['pick']} "
                f"({fmt(slots[r['pick'] - 1][0])}, {slots[r['pick'] - 1][1]}): {r['reason']}"
                + (" [moved by code: repeat/window rule]" if t.get("moved_by_code") else ""))
            if on_turn:
                on_turn(me, r, slots[r["pick"] - 1])
            if r["decision"] == "accept":
                agreed, how, backup = r["pick"], f"agreed in round {round_no}", r.get("backup")
                backup_how = ("both agents named it" if backup and backup == last.get("backup")
                              else f"{me['name']}'s agent named it when accepting")
                break
            seen.append(r["pick"])
            by[r["pick"]] = f"{me['name']}'s agent"
            last = {"pick": r["pick"], "backup": r.get("backup"), "reason": r["reason"] or f"slot {r['pick']}"}
        if agreed:
            break
    # Combined rank: lowest summed position; ties go to the proposer's order.
    score = {s: sum(rk.index(s) for rk in rankings.values()) for s in range(1, n + 1)}
    by_score = sorted(score, key=lambda s: (score[s], rankings[proposer["name"]].index(s)))
    if not agreed:
        agreed, how = by_score[0], "best combined rank"
        backup = next((s for s in by_score if s != agreed and s not in seen), None)
        backup_how = "next-best combined rank"
        log(f"  No agreement after {MAX_ROUNDS} rounds; best combined rank is slot {agreed}.")
    ranked = [agreed - 1] + [s - 1 for s in by_score if s != agreed]
    return {"agreed_index": agreed - 1, "how": how, "turns": turns, "ranked": ranked,
            "combined_rank": score, "offered": sorted({s - 1 for s in seen} | {agreed - 1}),
            "backup_index": backup - 1 if backup else None, "backup_how": backup_how}


LENGTH_SYSTEM = """You are {name}'s personal scheduling agent inside Ketchup.
{name} and {other} are meeting to "{activity}". When asked "How long do you
prefer to meet?", {name} wants {mine} minutes and {other} wants {theirs} minutes.
Agree on one meeting length with {other}'s agent.
Reply with strict JSON only, no prose, no code fences:
{{"decision": "propose" | "accept" | "counter",
  "minutes": a multiple of {step} between {lo} and {hi},
  "reason": "one short sentence, friendly, under 20 words"}}
Rules:
- Minutes are always a clean multiple of {step}. Never split into an awkward
  number: if it isn't a multiple of {step}, round DOWN (37 -> 35). The middle
  ground of {mine} and {theirs} is {mid} min (the average rounded down), e.g.
  "Let's meet in the middle at {mid} min." Any number in your reason must match.
- A shorter meeting fits more calendars; a longer one honors who wants more time.
  Be fair, and settle quickly.
- "propose" only when you go first. Otherwise "accept" (minutes = the other
  agent's minutes) or "counter" (different minutes)."""


def negotiate_length(proposer, other, activity, wants, log=print, on_turn=None, before_turn=None):
    """Agents agree on a meeting length. `wants` = {name: minutes}. Up to
    MAX_ROUNDS rounds; no deal -> the shorter length. Returns
    {"minutes", "how", "turns"}."""
    wants = {n: clean(m) for n, m in wants.items()}
    a, b = wants[proposer["name"]], wants[other["name"]]
    if a == b:
        return {"minutes": a, "how": "both asked for the same length", "turns": []}
    lo, hi = min(a, b), max(a, b)
    turns, last = [], None
    order = [proposer, other]
    for round_no in range(1, MAX_ROUNDS + 1):
        for i, me in enumerate(order):
            them = order[1 - i]
            system = LENGTH_SYSTEM.format(name=me["name"], other=them["name"], activity=activity,
                                          mine=wants[me["name"]], theirs=wants[them["name"]], lo=lo, hi=hi,
                                          step=STEP, mid=middle(lo, hi))
            if last is None:
                user = "You go first. Propose a length."
            else:
                user = (f"{them['name']}'s agent proposes {last['minutes']} minutes: \"{last['reason']}\"\n"
                        "Accept it or counter with a different length.")

            def check(r, first=last is None, offered=last and last["minutes"]):
                if r.get("decision") not in ({"propose"} if first else {"accept", "counter"}):
                    raise ValueError("bad decision")
                if not isinstance(r.get("minutes"), (int, float)) or not lo <= r["minutes"] <= hi:
                    raise ValueError(f"minutes must be {lo}-{hi}")
                raw = r["minutes"]
                r["minutes"] = max(lo, clean(raw))  # never an awkward number: round down to 5
                if r["minutes"] != raw:
                    r["minutes_raw"] = raw
                r["reason"] = fix_text(r.get("reason"))
                if r["decision"] == "counter" and r["minutes"] == offered:  # rounded onto the offer
                    r["decision"] = "accept"
                if r["decision"] == "accept" and r["minutes"] != offered:
                    raise ValueError("accept must use the offered minutes")
                if r["decision"] == "counter" and r["minutes"] == offered:
                    raise ValueError("counter must change the minutes")
                if not isinstance(r.get("reason"), str) or not r["reason"].strip():
                    raise ValueError("missing reason")
            if before_turn:
                before_turn(me)
            out = ask_json(system, user, check=check)
            r = out["reply"]
            turns.append({"round": round_no, "agent": me["name"], "topic": "length",
                          "prompt": {"system": system, "user": user}, "model": out["model"],
                          "model_errors": out["errors"], "reasoning_content": out["reasoning"], "reply": r})
            log(f"  [length round {round_no}] {me['name']}'s agent {r['decision']}s {r['minutes']} min: {r['reason']}")
            if on_turn:
                on_turn(me, r)
            if r["decision"] == "accept":
                return {"minutes": r["minutes"], "how": f"agreed in round {round_no}", "turns": turns}
            last = r
    log(f"  No length deal after {MAX_ROUNDS} rounds; taking the shorter, {lo} min.")
    return {"minutes": lo, "how": "no deal: the shorter length", "turns": turns}
