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

## How a catch-up gets planned

Both pebbles are identical: each has a mic and a speaker. Nobody presses buttons.
Everything is asked and answered by voice.

1. **Tap.** Both pebbles are tapped together after someone says a plan.
2. **Nudge.** Both pebbles say e.g. "Mayo and Frankie want to catch up Thursday!"
3. **Morning, noon, or night?** Each pebble asks its person. Windows live in
   `config/settings.json`: morning 8:00–11:30 AM, noon 11:30 AM–2:30 PM,
   night 5:00–9:00 PM (ET). If someone doesn't answer within 30 s, their agent
   uses the habit it inferred from that person's own calendar.
4. **Agents pick a time.** Each person's agent shares only their free windows with
   the other agent (times, never event titles). If the answers differ, the agents
   look at the windows in between too (morning + night also searches noon) and
   negotiate a fair slot. No shared slot means they look at next week.
5. **Want to change it?** Both pebbles ask "How about Thu Oct 29 at 12:30 PM? Want to
   change it?" "Change" from either person offers the next-best option, up to 2 times.
   It's booked on both calendars only when **both** say keep.

**Hard rule:** a proposed meeting always leaves at least 30 minutes free before and
after every event on either calendar (`buffer_min` in `config/settings.json`). This is
enforced in code before the agents see any option, so 7:30 PM is never offered when
there's an event at 8 PM. Check it: `.venv/bin/python tests/test_slots.py`

Voice answers are mapped by Muse Spark Open, so "ugh, mornings are rough, evening
please" becomes `night` and "can we do a different time?" becomes `change`. A plain
keyword (`morning`, `noon`, `night`, `keep`, `change`) is accepted as is, for testing.
If the answer is unclear, the question stays open.

## Run it from the command line

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
export BASETEN_API_KEY=... META_API_KEY=... GOOGLE_SA_JSON="$(cat service-account.json)"
.venv/bin/python brain/demo.py "let's catch up Thursday"     # asks each question interactively
.venv/bin/python brain/demo.py "let's catch up Thursday" \
    --mayo morning --frankie night --confirm change,keep --dry-run
```

- `--mayo` / `--frankie`: that person's answer (any words, or `timeout` for no answer).
- `--confirm change,keep`: keep/change per round for both. Use `--mayo-confirm` and
  `--frankie-confirm` to set each person separately.
- `--dry-run`: never book. `--pick N`: manual override that books option N.
- Anything not given on the command line is asked interactively. Press Enter for no answer.

Real run (Mayo didn't answer, Frankie said "lunchtime works for me"):

```
  Frankie said "lunchtime works for me" -> noon
  Mayo didn't answer -> agent uses calendar habit: night
Searching noon 11:30 AM-2:30 PM, night 5:00 PM-9:00 PM (always 30 min clear of other events)
  Mayo's agent shares free time: Thu 8:00 AM-7:30 PM
  Frankie's agent shares free time: Thu 8:00 AM-10:30 AM; Thu 12:30 PM-9:00 PM
Options both are free for:
  1. Thu Oct 29, 12:30 PM ET [noon]
  2. Thu Oct 29, 6:30 PM ET [night]
Agents negotiating:
  [round 1] Mayo's agent proposes slot 1 (Thu Oct 29, 12:30 PM ET, noon): Honoring Frankie's spoken noon preference with great buffer for both.
  [round 1] Frankie's agent accepts slot 1 (Thu Oct 29, 12:30 PM ET, noon): Perfectly matches Frankie's spoken noon preference!
Both pebbles say: "How about Thu Oct 29 at 12:30 PM? Want to change it?"
```

### The agents

- One Muse Spark Open agent per person (`brain/agents.py`). Each knows what its person
  said, that person's inferred habits, their own calendar, and the other person's free
  windows only.
- Habits are inferred, never typed: `brain/infer_prefs.py` gives Muse Spark Open the facts
  of that person's own calendar (timed events from 4 weeks back to 2 weeks ahead), and
  the model infers morning/afternoon/evening or no clear pattern, with confidence and
  evidence. Habits are the default when someone doesn't answer, and otherwise only break ties.
  Try it: `.venv/bin/python brain/infer_prefs.py mayo frankie`
- The person who said the phrase goes first and proposes a slot with a one-line reason.
  The other agent accepts or counters. Up to 2 rounds. If they still disagree, the slot with
  the best combined rank wins.
- Agents reply in strict JSON (`ranking`, `decision`, `pick`, `reason`). Malformed replies
  are retried, then fall back to Meta's `muse-spark-1.3`.

## Tap server

The pebbles talk to a small standard-library server.

```
.venv/bin/python server/tap_server.py --dry-run   # port 8787
.venv/bin/python scripts/fake_taps.py             # case 1: 0.5 s apart -> paired, voice answers; case 2: 3 s apart -> expired
```

- `POST /tap {"device_id": "pebble-mayo", "phrase": "..."}` waits up to 2.0 s for a tap
  from the other pebble. Both get the same `session_id`, or `expired`. Either pebble can
  send the phrase it heard.
- `GET /sessions/<id>` returns the `state` and, per device, the current question:
  `{"kind": "time_of_day" | "confirm", "say": "<text for this pebble to speak>", "choices": [...]}`.
  Each pebble polls it and speaks `say`.
- `POST /sessions/<id>/answer {"device_id": "pebble-frankie", "answer": "<voice transcript or keyword>"}`
  answers that device's open question (409 if there is none, or if it's unclear).
- States: `paired` → `asking_time_of_day` → `proposing` → `confirming` → `booked`
  (or `agreed (dry run, not booked)`) / `failed`.

## Files

- `config/settings.json`: time windows, the 30 min buffer, answer timeout, max change rounds.
- `config/people.json`: each person's name, calendar and pebble device id.
- `brain/parse_plan.py`: Muse Spark Open turns the phrase into a plan (activity, date window, 30 min default). Falls back to Meta's `muse-spark-1.3`.
- `brain/calendar_slots.py`: free/busy, the 30 min buffer rule, each person's free windows, one candidate per day and window.
- `brain/infer_prefs.py`: infers each person's habits from their own calendar.
- `brain/agents.py`: the two agents and the negotiation.
- `brain/flow.py`: the session state machine (nudge, questions, voice answer mapping, proposal, keep/change, booking), used by the CLI and the server.
- `brain/book.py`: puts the same event on each calendar (via a Google service account both calendars are shared with, so nobody signs in).
- `tests/test_slots.py`: checks the 30 min rule.
- `traces/`: one JSON per run with every step: the plan, free/busy, inferred profiles (input facts, model reasoning, JSON), each answer and how it was mapped, shared free windows, every agent turn, each keep/change, and the created events.
