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

Say a plan, get 3 times that work for both Mayo and Frankie, pick one, and it lands on both calendars.

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
export BASETEN_API_KEY=... META_API_KEY=... GOOGLE_SA_JSON="$(cat service-account.json)"
.venv/bin/python brain/demo.py "let's catch up Thursday"           # asks you to pick 1-3
.venv/bin/python brain/demo.py "let's catch up Thursday" --pick 1  # no prompt
```

- `brain/parse_plan.py`: Muse Spark Open turns the phrase into a plan (activity, date window, 30 min default). Falls back to Meta's `muse-spark-1.3`.
- `brain/calendar_slots.py`: free/busy for both calendars, 9 AM–9 PM ET, never in the past; offers the earliest, middle and latest shared slot. If the window is full, it looks at next week.
- `brain/book.py`: puts the same event on each calendar (via a Google service account both calendars are shared with, so nobody signs in).
- `traces/`: one JSON per run with the phrase, the model's reasoning, the plan, free/busy, the offered slots, the pick and the created events.
