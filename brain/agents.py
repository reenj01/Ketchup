"""One Muse Spark Open agent per person, and the negotiation between them.

Each agent knows its own person's calendar (busy times, no titles shared),
what its person answered to "Morning, noon, or night?", habits inferred from
its own calendar, and the other person's free windows (shared by the other
agent: times only, no event details).
"""
import json

from parse_plan import ask_json

MAX_ROUNDS = 2

SYSTEM = """You are {name}'s personal scheduling agent inside Ketchup.
{name} and {other} just agreed to "{activity}". The numbered slots below are
times when BOTH are free, each already at least 30 minutes away from any
other event on either calendar, best fit to both answers first. Morning,
noon and night are soft preferences. "fallback" = nothing worked in the
window someone asked for that day. "compromise" = their window had time, but
this slot is the other person's pick or a middle ground. Get a slot that is good for {name} while
being fair, so the two of you agree quickly.

- {name} was asked "Morning, noon, or night?" and said: {my_answer}
- {other} said: {their_answer}
- {name}'s habits, inferred from {name}'s own calendar (weaker than what they said):
{profile}
- {other}'s free time in this date range, shared by {other}'s agent (times only):
{their_free}

Reply with strict JSON only, no prose, no code fences:
{{"ranking": [slot numbers, best for {name} first, every slot exactly once],
  "decision": "propose" | "accept" | "counter",
  "pick": slot number you want,
  "reason": "one short sentence, friendly, under 20 words"}}
Rules:
- What each person said matters most. If both said the same part of the day,
  stay in it. If they differ, find a fair middle (noon sits between morning
  and night) or a choice that honors both as well as possible.
- An answer marked "no answer: default from calendar habits" was NOT said by
  the person; never claim they asked for it, and weigh it below a spoken answer.
- If you pick a fallback slot, say so plainly in your reason, e.g.
  "Nothing worked at noon, so how about <time>?" Use the real slot time.
  Never call a compromise a fallback.
- Use inferred habits only to break ties, weighted by their confidence.
- "propose" only when you go first. Otherwise "accept" (pick = the other
  agent's pick) or "counter" (pick = a different slot).
- Accept if the other pick is fair for {name}, even if not the very best."""


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
    def check(r):
        if sorted(r.get("ranking", [])) != list(range(1, n + 1)):
            raise ValueError(f"ranking must list 1..{n} once: {r.get('ranking')}")
        if r.get("pick") not in range(1, n + 1):
            raise ValueError(f"bad pick {r.get('pick')}")
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


def agent_turn(me, other, activity, know, context, round_no, last=None, n=3):
    """One agent move. `know[name]` = {"answer", "profile", "free"}; `last` is the
    other agent's latest {pick, reason} or None."""
    system = SYSTEM.format(
        name=me["name"], other=other["name"], activity=activity,
        my_answer=know[me["name"]]["answer"], their_answer=know[other["name"]]["answer"],
        profile=json.dumps(know[me["name"]]["profile"], indent=2),
        their_free=fmt_windows(know[other["name"]]["free"]))
    user = f"{me['name']}'s own calendar:\n{context}\n\n"
    if last is None:
        user += "You go first. Propose the slot you want."
        summary = f"{me['name']}'s agent proposes first from {n} shared slots"
    else:
        user += (f"{other['name']}'s agent wants slot {last['pick']}: \"{last['reason']}\"\n"
                 "Accept it or counter with a different slot.")
        summary = (f"{me['name']}'s agent answers {other['name']}'s pick of slot "
                   f"{last['pick']} (round {round_no})")
    out = ask_json(system, user, check=_checker(n, last is None, last and last["pick"]))
    return {"round": round_no, "agent": me["name"], "prompt_summary": summary,
            "prompt": {"system": system, "user": user},
            "model": out["model"], "model_errors": out["errors"],
            "reasoning_content": out["reasoning"], "reply": out["reply"]}


def negotiate(proposer, other, activity, slots, busy_by_cal, minutes, know, log=print, notes=None):
    """Proposer's agent picks first, the other accepts or counters, up to
    MAX_ROUNDS rounds; then the best combined rank wins.
    `slots` = [(start, window)]. Returns {"agreed_index", "how", "turns", "ranked"}
    where `ranked` lists every slot index, agreed first, then next-best."""
    n = len(slots)
    if n == 1:
        log("  Only one shared slot, nothing to negotiate.")
        return {"agreed_index": 0, "how": "only one shared slot", "turns": [], "ranked": [0]}
    ctx = {p["name"]: calendar_context(busy_by_cal[p["calendar_id"]], slots, minutes, notes)
           for p in (proposer, other)}
    turns, rankings, last, agreed, how = [], {}, None, None, None
    order = [proposer, other]
    for round_no in range(1, MAX_ROUNDS + 1):
        for i, me in enumerate(order):
            t = agent_turn(me, order[1 - i], activity, know, ctx[me["name"]], round_no, last, n)
            turns.append(t)
            r = t["reply"]
            rankings[me["name"]] = r["ranking"]
            log(f"  [round {round_no}] {me['name']}'s agent {r['decision']}s slot {r['pick']} "
                f"({fmt(slots[r['pick'] - 1][0])}, {slots[r['pick'] - 1][1]}): {r['reason']}")
            if r["decision"] == "accept":
                agreed, how = r["pick"], f"agreed in round {round_no}"
                break
            last = {"pick": r["pick"], "reason": r["reason"]}
        if agreed:
            break
    # Combined rank: lowest summed position; ties go to the proposer's order.
    score = {s: sum(rk.index(s) for rk in rankings.values()) for s in range(1, n + 1)}
    by_score = sorted(score, key=lambda s: (score[s], rankings[proposer["name"]].index(s)))
    if not agreed:
        agreed, how = by_score[0], "best combined rank"
        log(f"  No agreement after {MAX_ROUNDS} rounds; best combined rank is slot {agreed}.")
    ranked = [agreed - 1] + [s - 1 for s in by_score if s != agreed]
    return {"agreed_index": agreed - 1, "how": how, "turns": turns, "ranked": ranked,
            "combined_rank": score}
