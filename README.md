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
   `config/settings.json`, with no gaps between them: morning 8:00–11:59 AM,
   noon 12:00–4:59 PM, night 5:00–9:00 PM (ET). A slot belongs to the window it
   starts in, and must end by 9 PM, so the last 30-min start is 8:30 PM. If someone
   doesn't answer within 30 s, their agent uses the habit it inferred from that
   person's own calendar.
4. **Agents pick a time.** Each person's agent shares only their free windows with
   the other agent (times, never event titles). Answers are **soft preferences**:
   slots in the asked-for window rank first (other windows stay on the list as
   next-best options for "change"). If nothing there works for both, slots
   in the other windows the same day are offered instead, nearest first, and everyone
   is told plainly ("Nothing worked at noon on Thursday, so how about Thu Oct 29 at
   10:00 AM?"). **What a person says always beats their habits.** Different answers
   (morning vs night): slots in the two said windows rank first, never a middle window
   nobody asked for.
   Next week only if nothing works in any window on the plan's dates.
5. **Want to change it?** Both pebbles ask "How about Thu Oct 29 at 12:30 PM? Want to
   change it?" (each pebble says why if it's a fallback or a middle ground for its person).
   "Change" from either person offers the next-best option, up to 2 times.
   It's booked on both calendars only when **both** say keep.

**Hard rule:** a proposed meeting always leaves at least 30 minutes free before and
after every event on either calendar (`buffer_min` in `config/settings.json`). This is
enforced in code before the agents see any option, so 7:30 PM is never offered when
there's an event at 8 PM. Check it, along with the windows and the noon-blocked
fallback: `.venv/bin/python tests/test_slots.py`

Voice answers are mapped by Muse Spark Open, so "ugh, mornings are rough, evening
please" becomes `night` and "can we do a different time?" becomes `change`. Plain
answers skip the model to save time: `mornings`, `evening`, `tonight`, `lunch`,
`30 min`, `an hour`, `yes`, `no` and similar map instantly with a keyword table.
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

- `--extra-busy tests/fixtures/noon_blocked_thu.json`: add synthetic busy blocks for a
  dry run (never written to a calendar), e.g. to force a fallback.

Real dry run where both said noon and synthetic blocks fill Thursday's noon:

```
Preferred: noon 12:00 PM-4:59 PM; other windows only if nothing fits (always 30 min clear of other events)
  (dry run with synthetic busy blocks added: Mayo Thu 12:00 PM-2:00 PM; Frankie Thu 2:30 PM-5:00 PM)
Options both are free for, best fit first:
  1. Thu Oct 29, 5:30 PM ET [night] (fallback, Mayo: nothing worked at noon on Thursday; ...)
  2. Thu Oct 29, 10:00 AM ET [morning] (fallback, Mayo: nothing worked at noon on Thursday; ...)
Agents negotiating:
  [round 1] Mayo's agent proposes slot 2 (Thu Oct 29, 10:00 AM ET, morning): Nothing worked at noon, so how about 10 AM Thursday?
  [round 1] Frankie's agent accepts slot 2 (Thu Oct 29, 10:00 AM ET, morning): Nothing worked at noon, so how about 10 AM Thursday?
Both pebbles say: "Nothing worked at noon on Thursday, so how about Thu Oct 29 at 10:00 AM? Want to change it?"
```

### The agents

- One Muse Spark Open agent per person (`brain/agents.py`). Each knows what its person
  said, their own calendar, and the other person's free windows only. The inferred habits
  are added to the prompt only when that person gave no answer.
- Habits are inferred, never typed: `brain/infer_prefs.py` gives Muse Spark Open the facts
  of that person's own calendar (timed events from 4 weeks back to 2 weeks ahead), and
  the model infers morning/afternoon/evening or no clear pattern, with confidence and
  evidence. Habits are the default when someone doesn't answer, and otherwise only break ties.
  Try it: `.venv/bin/python brain/infer_prefs.py mayo frankie`
- The person who said the phrase goes first and proposes a slot with a one-line reason.
  The other agent accepts or counters. Up to 2 rounds. If they still disagree, the slot with
  the best combined rank wins.
- Rules checked in code, not just asked for in the prompt: a slot already offered (in this
  negotiation or an earlier change round) is never offered again; going first, the agent
  proposes in its own person's said window; a counter stays in a window someone said. A
  reply that breaks a rule is sent back once with the reason, then moved by code to that
  agent's next allowed slot (or turned into an accept if nothing new is left). Check:
  `.venv/bin/python tests/test_agent_rules.py`. Replay an old negotiation with today's
  rules: `.venv/bin/python scripts/replay_negotiation.py traces/<file>.json`.

### Speed

`reasoning_effort` in `config/settings.json` is `low` (Muse Spark Open supports `low`,
`medium`, `high`, `xhigh`; `xhigh` is the default and reasoning can't be turned off). On
the same slot-pick prompt, Baseten took 2.6 s at the default and 0.9 s at `low`. Plus: plain
answers map by keyword, both people's answers are mapped in parallel, habits are inferred
in the background while the questions are asked (and cached until the calendar changes),
and model calls reuse one HTTPS connection. A whole web session (two answers each, one
change) went from about 126 s to about 27 s. Every trace has a `timing` block
(per call: label, seconds, tokens).
- Agents reply in strict JSON (`ranking`, `decision`, `pick`, `reason`). Malformed replies
  are retried, then fall back to Meta's `muse-spark-1.3`.

## Web page (watch everything on one screen)

A three-chat page for seeing the whole flow: Mayo and Mayo's agent on the left,
Frankie and Frankie's agent on the right, and the two agents negotiating in the
middle. Press **Ketchup** (same as tapping the pebbles), answer both questions on
each side ("What time of day do you prefer to meet?", "How long do you prefer to
meet?"), and watch the agents settle the length and then the day and time. Answers are
typed for now; on the pebbles they'll be spoken. Everything is live: Muse Spark
Open maps the answers and runs both agents, and both real calendars are read.

**Dry run (don't touch calendars)** is on by default. Turn it off to really book.

### Run it on a Mac (first time)

```
cd ~/Desktop/ketchup
git pull
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
open -e .env
```

In `.env`, paste your Baseten key after `BASETEN_API_KEY=` (`META_API_KEY` is optional,
for the backup model) and save. Then put the Google service account JSON key in the
repo folder, named `service-account.json` (the default `.env` line,
`GOOGLE_SA_FILE=service-account.json`, points at it):

```
mv ~/Downloads/<the-key-file>.json ~/Desktop/ketchup/service-account.json
```

`.env` and `service-account.json` are both gitignored, so they never get committed.

Start the server, then open **http://localhost:8000**:

```
.venv/bin/python web/server.py
```

Next time it's just `cd ~/Desktop/ketchup && git pull && .venv/bin/python web/server.py`
(run `.venv/bin/pip install -r requirements.txt` again if `requirements.txt` changed).
Stop it with Ctrl+C. Use `--port 9000` if 8000 is taken. The macOS built-in `python3`
(3.9) works.

### How the page's flow works

1. Each agent asks its person the two questions. Muse Spark Open turns free text into
   morning / noon / night and into minutes ("an hour" → 60).
2. Middle chat: if the lengths differ, the agents agree on one (real model turns, up to 2
   rounds, else the shorter). Each agent then shares its person's free times, and the agents
   negotiate the day and time over the shared slots (30 min hard buffer, soft windows,
   same-day fallback, the same rules as the pebbles).
3. It books right away (unless dry run), and each agent says "Frankie and you are free on
   <day> at <time>. I've booked it in your calendar. Do you want a different day or time?"
4. If anyone says yes, the events are deleted and the agents renegotiate without that day's
   window and without any time offered in an earlier round. Up to `max_change_rounds` (2) changes. When both say no: "Plan booked with <other>."

This book-then-ask order applies only to the page. The pebble/CLI flow (`flow.py`) still
books only after both keep it. API: `POST /api/start`, `GET /api/session/<id>?since=n`,
`POST /api/session/<id>/say`. `scripts/web_drive.py` runs a whole session through it.

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

- `config/settings.json`: the day (8 AM–9 PM), time windows, the 30 min buffer, answer timeout, max change rounds, `reasoning_effort`.
- `config/people.json`: each person's name, calendar and pebble device id.
- `brain/parse_plan.py`: Muse Spark Open turns the phrase into a plan (activity, date window, 30 min default). Falls back to Meta's `muse-spark-1.3`.
- `brain/calendar_slots.py`: free/busy, the 30 min buffer rule, each person's free windows, one candidate per day and window ranked by fit to the answers, fallback notes.
- `brain/infer_prefs.py`: infers each person's habits from their own calendar.
- `brain/agents.py`: the two agents and the negotiation.
- `brain/flow.py`: the session state machine (nudge, questions, voice answer mapping, proposal, keep/change, booking), used by the CLI and the server.
- `brain/book.py`: puts the same event on each calendar (via a Google service account both calendars are shared with, so nobody signs in).
- `brain/web_flow.py`: the web page's session (three chats, length negotiation, book-then-ask, change rounds).
- `web/server.py`, `web/index.html`: the web page (standard library server, one HTML file). `.env.example` lists the keys it reads.
- `scripts/web_drive.py`: drives a full web session over HTTP (optionally with screenshots).
- `brain/timing.py`: records the time and tokens of every model and Google call.
- `scripts/replay_negotiation.py`: re-runs a traced negotiation with the current agents (nothing booked).
- `tests/test_slots.py`: checks the 30 min rule, the windows, the same-day fallback when noon is blocked, and that said windows rank before habits.
- `tests/test_agent_rules.py`: checks the code rules (no repeats, said windows first).
- `traces/`: one JSON per run with every step: the plan, free/busy, inferred profiles (input facts, model reasoning, JSON), each answer and how it was mapped, shared free windows, every agent turn, each keep/change, and the created events.
