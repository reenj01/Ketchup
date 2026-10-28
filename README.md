# Ketchup

Two pocket pebbles that turn "let's catch up sometime" into a meeting on both calendars.

"We should catch up" usually goes nowhere, because someone has to compare calendars
and pick a time. With Ketchup, two friends tap their pebbles together, each person's
AI agent reads their own calendar, and the agents agree on a time and pencil it in.

Built for the Meta Global AI Developer Hackathon (Oct 26 to Nov 4, 2026), Indie track.

## Prizes we're going for

- Best Agentic Use Case
- Best Build on Meta Open Weight Models

## Demo

- Demo video: **TODO: add link**
- Live try-it page: **TODO: add link**

The demo uses two seeded mock Google Calendars, Mayo and Frankie. Judges don't need to
log in or connect any account. The organizers allow mock data.

## How it works

```
 Mayo's pebble ──tap──> Frankie's pebble     (two taps within 2 s = paired)
        │                       │
        ▼                       ▼
  Mayo's agent  <──negotiate──>  Frankie's agent    (Muse Spark Open)
  (Mayo's calendar)            (Frankie's calendar)
        │                       │
        └──> tentative event on both calendars <──┘
```

1. **Tap.** After someone says a plan ("let's catch up Thursday"), both pebbles are
   tapped together. A magnet and Hall sensor detect the tap, and the server pairs two
   taps that arrive within 2 s.
2. **Ask.** Each pebble asks its person two questions: what time of day (morning, noon,
   night) and how long to meet.
3. **Negotiate.** Each person has their own agent. It sees its person's answers and
   calendar, plus only the other person's free windows (never event titles). The agents
   propose, accept or counter in strict JSON. Habits are inferred from each calendar,
   never typed in, and what a person says always beats their habits.
4. **Hard rules in code, not just in the prompt.** At least 30 minutes free around every
   other event, a time already offered is never offered again, lengths are multiples
   of 5 minutes (30 and 45 meet at 35).
5. **Tentative hold.** A tentative event goes on both calendars with an agreed backup
   time. It auto-confirms at 10 PM ET (11:59 PM if made at or after 10 PM), or 30 minutes
   before the meeting if that's sooner.
6. **Change.** A reply like "not Thursday, something later" becomes hard constraints
   that add up across rounds. The hold moves to the backup if it fits, otherwise the
   agents renegotiate, widening the search up to 8 weeks. The other person only hears
   "Mayo would prefer a different day or time."

The web page shows the whole flow on one screen: Mayo's chat, the two agents
negotiating, and Frankie's chat. The **Ketchup** button stands in for the tap.

## Meta AI used

| Model | Role | Status |
|---|---|---|
| Muse Spark Open (`meta-models/muse-spark-open-1026`) on Baseten | Main brain: parses the plan, infers habits from each calendar, runs both agents, maps free-text answers and change reasons | In the code (`brain/parse_plan.py`) |
| Meta Model API `muse-spark-1.3` | Fallback when a Baseten call fails or returns bad JSON | In the code (`brain/parse_plan.py`) |
| Muse Voice Transcribe (`muse-voice-transcribe-1.0`) | Speech-to-text on the pebbles | Planned, not in the repo yet |

Every model Ketchup calls at runtime is a Meta model. Voice output on the pebbles will
be espeak, a non-AI synthesizer (planned). Today the web page takes typed answers.

Every run writes a trace to `traces/` with each model call, agent turn, timing and the
events created.

## Privacy and listening

- The pebbles are never always listening. They listen only for a short window after a
  tap or when the two pebbles are near each other.
- Only that window's audio goes to Meta for transcription.
- Agents share free windows, not event titles.
- A person's reason for a change is never passed to the other person.

## Built during the hackathon

All code in this repo was written during the event. The first commit is Oct 27, 2026,
and the full history is in `git log`. Nothing pre-existing was reused. Outside
dependencies are only `google-auth` and `google-api-python-client`; the servers use
the Python standard library.

## Run it locally

Needs Python 3.9+, a Baseten key, and a Google service account that both demo
calendars are shared with.

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env     # then fill in the keys below
```

Environment variables (names only):

- `BASETEN_API_KEY`: Muse Spark Open on Baseten (required)
- `META_API_KEY`: Meta Model API fallback (optional)
- `GOOGLE_SA_FILE` (path to the key JSON, default `service-account.json`) or
  `GOOGLE_SA_JSON` (the key JSON itself)

Web page (reads `.env` on its own), then open http://localhost:8000:

```
.venv/bin/python web/server.py
```

"Dry run" is on by default, so nothing is written to the calendars.

Tap server plus simulated pebble taps (export the variables above first):

```
.venv/bin/python server/tap_server.py --dry-run     # port 8787
.venv/bin/python scripts/fake_taps.py               # paired taps, then expired taps
```

Command line:

```
.venv/bin/python brain/demo.py "let's catch up Thursday" --mayo morning --frankie night --dry-run
```

Tests:

```
for t in tests/test_*.py; do .venv/bin/python "$t"; done
```

## Hardware

Two identical pebbles, one on a Raspberry Pi 5 and one on an Arduino UNO Q. Each has a
mic, a speaker, a KY-003 Hall sensor, a magnet for the tap, and a power bank. The
pebble code is not in the repo yet; the pebbles talk to `server/tap_server.py` over HTTP.

## Connecting real calendars

In the demo, a Google service account reads and writes the two seeded calendars, which
are shared with it, so nobody signs in. A real user would connect with Google sign-in
(OAuth) and grant calendar access. Their agent would then read that calendar and add
holds to it the same way. The other person's agent still only sees free windows.
