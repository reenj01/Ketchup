# Ketchup

Two pocket pebbles that turn "let's catch up sometime" into a real plan.

When one of you says something like "let's catch up next week," both pebbles glow.
You tap them together to agree. Each person's agent checks their calendar, and
the agents settle on a time. The meeting lands on both calendars.

Built for the Meta Global AI Developer Hackathon (Oct 26 – Nov 4, 2026), Indie track.
All code in this repo was written during the event.

## Brain

Muse Spark Open (`meta-models/muse-spark-open-1026`) on Baseten reads what was said
and pulls out the plan. Meta's Model API (`muse-spark-1.3`) is the backup.

```
export BASETEN_API_KEY=...
python3 brain/parse_plan.py "We should totally catch up next week, maybe grab lunch?"
```

## Run the brain end to end

Say a plan. Ketchup finds up to 3 times when Mayo and Frankie are both free, then
**each person's agent negotiates** and the agreed time lands on both calendars.

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
export BASETEN_API_KEY=... META_API_KEY=... GOOGLE_SA_JSON="$(cat service-account.json)"
.venv/bin/python brain/demo.py "let's catch up Thursday"            # agents negotiate, books
.venv/bin/python brain/demo.py "let's catch up Thursday" --dry-run  # negotiate, don't book
.venv/bin/python brain/demo.py "let's catch up Thursday" --pick 2   # manual override
```

Real run:

```
Agents negotiating:
  [round 1] Mayo's agent proposes slot 1 (Thu Oct 29, 9:00 AM ET): Morning slot keeps Mayo fresh and offers plenty of buffer time.
  [round 1] Frankie's agent counters slot 2 (Thu Oct 29, 3:00 PM ET): Afternoon is Frankie's favorite and still gives us great buffer time!
  [round 2] Mayo's agent accepts slot 2 (Thu Oct 29, 3:00 PM ET): 3 PM works well with good buffer and avoids late evening tiredness!
Agreed: Thu Oct 29, 3:00 PM ET (agreed in round 2)
```

### Two agents that negotiate

- One Muse Spark Open agent per person (`brain/agents.py`). Each sees only its own
  person's preferences (`config/people.json`) and its own calendar around each slot,
  never the other person's calendar.
- The person who said the phrase goes first: their agent ranks the slots and proposes one
  with a one-line reason. The other agent accepts or counters. Up to 2 rounds; if they
  still disagree, the slot with the best combined rank wins. No shared time: look at next week.
- Agents reply in strict JSON (`ranking`, `decision`, `pick`, `reason`); malformed replies
  are retried, then fall back to Meta's `muse-spark-1.3`.

## Tap server

The pebbles call a tiny standard-library server when they are tapped together.

```
.venv/bin/python server/tap_server.py --dry-run   # port 8787
.venv/bin/python scripts/fake_taps.py             # case 1: 0.5 s apart -> paired; case 2: 3 s apart -> expired
```

- `POST /tap {"device_id": "pebble-mayo", "phrase": "..."}` waits up to 2.0 s for a tap
  from the other pebble. Both get the same `session_id`, or `expired`.
- On pairing the agents run with the phrase heard by `pebble-mayo` (the Pi with the mic;
  `pebble-frankie` is the UNO Q).
- `GET /sessions/<id>` returns `negotiating`, `booked`, `agreed (dry run, not booked)` or `failed`,
  with each agent's turn and the agreed slot.

## Files

- `brain/parse_plan.py`: Muse Spark Open turns the phrase into a plan (activity, date window, 30 min default). Falls back to Meta's `muse-spark-1.3`.
- `brain/calendar_slots.py`: free/busy for both calendars, 9 AM–9 PM ET, never in the past; offers the earliest, middle and latest shared slot.
- `brain/agents.py`: the two agents and the negotiation.
- `brain/pipeline.py`: phrase -> plan -> slots -> negotiation -> booking, used by the CLI and the server.
- `brain/book.py`: puts the same event on each calendar (via a Google service account both calendars are shared with, so nobody signs in).
- `traces/`: one JSON per run with the phrase, the plan, free/busy, the candidate slots, every agent turn (prompt, model reasoning, JSON reply), the agreement and the created events.
