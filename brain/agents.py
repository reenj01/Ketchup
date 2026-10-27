"""One Muse Spark Open agent per person, and the negotiation between them.

Each agent sees only its own person's inferred preferences and calendar (busy blocks
on the candidate days, plus the free gap around each slot). It never sees the
other person's calendar, only the other agent's pick and one-line reason.
"""
import json
from datetime import datetime, timedelta

from parse_plan import ask_json

MAX_ROUNDS = 2

SYSTEM = """You are {name}'s personal scheduling agent inside Ketchup.
{name} and {other} just agreed to "{activity}". A fair helper already found
time slots when BOTH are free. Your job: get the slot that is best for {name},
while being reasonable so the two of you agree quickly.

What you know about {name} (keep it private, don't invent more):
- Preferences inferred from {name}'s own calendar patterns:
{profile}
- {name}'s own calendar is given below. You cannot see {other}'s calendar.

Reply with strict JSON only, no prose, no code fences:
{{"ranking": [slot numbers, best for {name} first, every slot exactly once],
  "decision": "propose" | "accept" | "counter",
  "pick": slot number you want,
  "reason": "one short sentence, friendly, under 20 words"}}
Rules:
- Rank with the inferred preferences, weighted by their confidence. A slot
  whose gap before or after is smaller than min_buffer_min is worse.
- "propose" only when you go first. Otherwise "accept" (pick = the other
  agent's pick) or "counter" (pick = a different slot).
- Accept if the other pick is fine for {name}, even if not the very best.
  Counter only when it clearly clashes with the preferences."""


def fmt(t):
    return t.strftime("%a %b %-d, %-I:%M %p ET")


def calendar_context(busy, slots, minutes):
    """Plain-text view of one person's own calendar around each slot."""
    days = sorted({s.date() for s in slots})
    lines = []
    for d in days:
        blocks = [f"{s:%-I:%M %p}-{e:%-I:%M %p}" for s, e in busy if s.date() == d]
        lines.append(f"{d:%a %b %-d} busy: {', '.join(blocks) or 'nothing'}")
    lines.append("Slots (gap = free time right before / after on your calendar):")
    for i, s in enumerate(slots, 1):
        end = s + timedelta(minutes=minutes)
        before = max([e for _, e in busy if e <= s], default=None)
        after = min([b for b, _ in busy if b >= end], default=None)
        gb = f"{int((s - before).total_seconds() // 60)} min" if before and before.date() == s.date() else "open"
        ga = f"{int((after - end).total_seconds() // 60)} min" if after and after.date() == s.date() else "open"
        lines.append(f"  {i}. {fmt(s)} ({minutes} min) - gap before: {gb}, after: {ga}")
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


def agent_turn(me, other, activity, context, round_no, profiles, last=None, n=3):
    """One agent move. `last` is the other agent's latest {pick, reason} or None."""
    system = SYSTEM.format(name=me["name"], other=other["name"], activity=activity,
                           profile=json.dumps(profiles[me["name"]], indent=2))
    user = f"{me['name']}'s calendar:\n{context}\n\n"
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


def negotiate(proposer, other, activity, slots, busy_by_cal, minutes, profiles, log=print):
    """`profiles` maps name -> that person's inferred profile (infer_prefs.py).
    Proposer's agent picks first, the other accepts or counters, up to
    MAX_ROUNDS rounds; then the best combined rank wins. Returns
    {"agreed_index", "how", "turns", "combined_rank"}."""
    n = len(slots)
    if n == 1:
        log("  Only one shared slot, nothing to negotiate.")
        return {"agreed_index": 0, "how": "only one shared slot", "turns": [],
                "combined_rank": None}
    ctx = {p["name"]: calendar_context(busy_by_cal[p["calendar_id"]], slots, minutes)
           for p in (proposer, other)}
    turns, rankings, last = [], {}, None
    order = [proposer, other]
    for round_no in range(1, MAX_ROUNDS + 1):
        for i, me in enumerate(order):
            them = order[1 - i]
            t = agent_turn(me, them, activity, ctx[me["name"]], round_no, profiles, last, n)
            turns.append(t)
            r = t["reply"]
            rankings[me["name"]] = r["ranking"]
            log(f"  [round {round_no}] {me['name']}'s agent {r['decision']}s "
                f"slot {r['pick']} ({fmt(slots[r['pick'] - 1])}): {r['reason']}")
            if r["decision"] == "accept":
                return {"agreed_index": r["pick"] - 1, "how": f"agreed in round {round_no}",
                        "turns": turns, "combined_rank": None}
            last = {"pick": r["pick"], "reason": r["reason"]}
    # No agreement: lowest summed rank position wins; ties go to the proposer's order.
    score = {s: sum(rk.index(s) for rk in rankings.values()) for s in range(1, n + 1)}
    best = min(score, key=lambda s: (score[s], rankings[proposer["name"]].index(s)))
    log(f"  No agreement after {MAX_ROUNDS} rounds; best combined rank is slot {best}.")
    return {"agreed_index": best - 1, "how": "best combined rank",
            "turns": turns, "combined_rank": score}
