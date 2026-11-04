# Ketchup

Two pocket Dots that turn "let's catch up sometime" into a meeting on both calendars.

**Try it live:** [Ketchup simulator](https://ireenjeang0726--ketchup-sim-web.us-east.modal.run). No login needed. Use See calendar to check the sample calendars the agents read.

"We should catch up" usually goes nowhere, because someone has to compare calendars
and pick a time. With Ketchup, two friends tap their Dots together, each person's
AI agent reads their own calendar, and the agents agree on a time and pencil it in.

Built for the Meta Global AI Developer Hackathon (Oct 26 to Nov 4, 2026), Indie track.

## Prizes I'm going for

- Indie Developer
- Best Agentic Use Case
- Best Build on Meta Open Weight Models

## Demo

- Demo video: **TODO: add link**
- Live try-it page: [ketchup-sim on Modal](https://ireenjeang0726--ketchup-sim-web.us-east.modal.run). Two virtual Dots: drag them together to pair, then hold to talk (or type). The model-call list shows each Meta model as it runs. The first visit can take a few seconds if the page has been idle.

The demo uses two seeded mock Google Calendars, Mayo and Frankie. Judges don't need to
log in or connect any account. The organizers allow mock data.

## How it works

```
   Mayo's Dot ───tap───>  Frankie's Dot     (two taps within 5 s = paired)
        │                       │
        ▼                       ▼
  Mayo's agent  <──negotiate──>  Frankie's agent    (Muse Spark Open)
  (Mayo's calendar)            (Frankie's calendar)
        │                       │
        └──> tentative event on both calendars <──┘
```

1. **Tap.** After someone says a plan ("let's catch up sometime next week"), both Dots
   are tapped together. A magnet and Hall sensor detect the tap, and the server pairs two
   taps that arrive within 5 s (`KETCHUP_PAIR_WINDOW_S`). Both LEDs go bright white. Mayo's
   Dot says "Dots connected. We will get back to you once we are done." slowly, still bright white,
   then breathes yellow while the agents pick a time (Frankie's Dot is silent and breathes
   right away). Nothing else is spoken until someone presses their Dot.
2. **Negotiate first.** Each person has their own agent. Right after the tap, with no
   questions up front, the two agents pick a day and time. Each agent sees its own
   person's calendar, plus only the other person's free windows (never event titles).
   The agents propose, accept or counter in strict JSON. Habits are inferred from each
   calendar, never typed in.
3. **Hard rules in code, not just in the prompt.** At least 30 minutes free around every
   other event, a time already offered is never offered again, lengths are multiples
   of 5 minutes (30 and 45 meet at 35).
4. **Hear it.** When a time is ready the LED breathes bright orange in and out: a time is
   waiting for your answer. Frankie answers after Mayo, so Frankie's Dot keeps breathing yellow
   while Mayo's is orange, and turns orange once Mayo has answered. Tap the Dot (or press the Pi's pressure sensor) and it says the
   time out loud.
5. **Answer.** On Mayo's Pi Dot, press the pressure sensor (the LED turns yellow) and say
   "keep it" or what you want instead. Frankie's UNO Q Dot has no mic: a short press keeps the
   day, a 1 s hold asks for a different time. A spoken change is, for example, "no, pick a time at night". It becomes a hard constraint, and constraints
   add up across rounds. Both Dots breathe yellow again while the agents find a new time
   (the agreed backup if it fits, otherwise a new search up to 8 weeks out). This repeats
   until you like it. The other person only hears "Mayo would prefer a different day or
   time", never your words or reason.
6. **Booked.** When both people say "keep it", the event goes on both calendars and the
   LEDs turn green for 10 seconds, then back to white. A held time also auto-confirms at
   10 PM ET (11:59 PM if made at or after 10 PM), or 30 minutes before the meeting if
   that's sooner.

The live web simulator shows the same agents on one screen: both Dots, the agents'
conversation as a chat, each person's sample calendar (See calendar), and a list of every
Meta model call. Dragging the Dots together (or the Pair button) stands in for the tap.

**Live hardware demo page.** The simulator's "Connect to hardware demo" button opens `/live`,
which shows the real Dots as they run: what each person said, the agents' negotiation, every
Meta model call (model, what it did, short input and output, time taken), and every line the
Mac's tap server prints (DOT ONLINE, taps, pairing, LED colours, bookings, errors). To turn it
on, put `KETCHUP_LIVE_BASE=<the simulator's URL>` in `.env` and run `bash scripts/demo_up.sh`.
It prints a private link (`.../live#<session>.<token>`); the token in it is the password, so
don't share it. The feed lives only in the simulator's memory, and the Mac blanks out anything
that looks like a key before it leaves.

## Meta AI used

| Model | Role | Status |
|---|---|---|
| Muse Spark Open (`meta-models/muse-spark-open-1026`) on Baseten | Main brain: parses the plan, infers habits from each calendar, runs both agents, maps free-text answers and change reasons | In the code (`brain/parse_plan.py`) |
| Meta Model API `muse-spark-1.3` | Fallback when a Baseten call fails or returns bad JSON | In the code (`brain/parse_plan.py`) |
| Muse Voice Transcribe (`muse-voice-transcribe-1.0`) | Speech-to-text on the Dots (hold-to-talk) | In the code (`pebble/pi_pebble.py`) |
| MMS-TTS (`facebook/mms-tts-eng`, Meta's open weights, CC-BY-NC 4.0) | The Dot's voice, run on the Dot itself | In the code (`pebble/pi_pebble.py`) |

Every model Ketchup calls at runtime is a Meta model. Everything else is a plain OS
feature, not a model: `arecord`/`aplay` for audio, GPIO for the LED and sensors, and
espeak as a backup voice while MMS loads or if it is not installed. The web page takes
typed answers.

Every run writes a trace to `traces/` with each model call, agent turn, timing and the
events created.

## What's real and what's simulated

- **Real hardware:** Mayo's Dot is a Raspberry Pi 5 running `pebble/pi_pebble.py`, with
  the Hall sensor tap, the RGB LED, the hold-to-talk pressure sensor, the mic and speaker,
  and MMS-TTS running on the Pi. The demo video shows it.
- **Frankie's Dot (Arduino UNO Q):** code is in `pebble/unoq/`. The board's microcontroller
  reads the tap and pressure sensors and drives the LED; its Linux side runs the same Dot
  logic as the Pi. It passes the mock run but has not run on the real board yet. Frankie can
  also be played from a laptop terminal with `scripts/frankie_terminal.py`.
- **Without hardware:** `scripts/sim_dots.py` runs both Dots' code (Pi and UNO Q) on a laptop
  with mock pins, a fake sketch and stub audio tools (see the comment at the top of the file).
- **Proof:** every run writes a trace to `traces/` with each model call, agent turn,
  timing and the events created. Three sample traces are in the repo: a full web run
  that ends agreed (Oct 29) and two runs from the Pi Dot on Nov 2.

Muse Spark Open runs on Baseten's hosted endpoint and does the real work: it parses the
plan, infers habits from each calendar, runs both agents, and maps free-text answers.

## Privacy and listening

- The Dots are never always listening. Mayo's Pi Dot records only between two presses
  of the pressure sensor. Frankie's UNO Q Dot has no mic at all (press to answer).
- Only that clip goes to Meta for transcription, and it is deleted from the Dot
  right after it is read. No audio is saved.
- Agents share free windows, not event titles.
- A person's reason for a change is never passed to the other person.

## Built during the hackathon

All code in this repo was written during the event. The first commit is Oct 27, 2026,
and the full history is in `git log`. Nothing pre-existing was reused. Outside
dependencies are only `google-auth`, `google-api-python-client` and `gpiozero` (the
Dots' pins); the servers use
the Python standard library.

## Run with no keys (labelled replay)

No keys, accounts or hardware needed. With no `BASETEN_API_KEY` or `META_API_KEY` set, Ketchup
replays real Muse Spark Open and Muse Voice Transcribe calls recorded on Nov 3, 2026
(`replays/demo/`), and labels them: a `REPLAY:` banner at start and a
`[replay] <model> (recorded 2026-11-03)` line for every call. The clock is frozen at the
recorded time (Tue Nov 3, 2026, 10:00 AM ET) so the agents see the same free times. Simulated:
both Dots (`scripts/sim_dots.py`: mock pins, stub mic and speaker), the calendars (the mock
calendar, `config/mock_calendar.json`), and Mayo's voice (a recorded clip,
`replays/demo/mayo_says.wav`, sent to speech to text). The demo video shows the live models.

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python server/tap_server.py --dry-run &
sleep 2
.venv/bin/python scripts/sim_dots.py
.venv/bin/python scripts/sim_dots.py --renegotiate
kill %1
```

Each sim ends with `both Dots: ALL GOOD`. Run them in this order on a fresh tap server, as
recorded; anything else stops with a `REPLAY MISS` message. To call the models live instead,
set a key as in "Run it locally" below.

## Run it locally

Needs Python 3.9+. For live model calls, a Baseten key (or a Meta Model API key); with
neither, it runs the labelled replay above. For the real calendars, a Google service
account that both demo calendars are shared with; without one, it uses the mock calendar.

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env     # then fill in the keys below
```

Environment variables (names only):

- `BASETEN_API_KEY`: Muse Spark Open on Baseten (without it or `META_API_KEY`: the replay)
- `META_API_KEY`: Meta Model API fallback (optional)
- `GOOGLE_SA_FILE` (path to the key JSON, default `service-account.json`) or
  `GOOGLE_SA_JSON` (the key JSON itself); optional, the mock calendar without it

Web page (reads `.env` on its own), then open http://localhost:8000:

```
.venv/bin/python web/server.py
```

"Dry run" is on by default, so nothing is written to the calendars.

Tap server plus simulated Dot taps (export the variables above first):

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

Two identical Dots, one on a Raspberry Pi 5 and one on an Arduino UNO Q. Each has a
mic, a speaker, a KY-003 Hall sensor, a magnet for the tap, an RGB LED, a pressure
sensor for hold-to-talk, and a power bank. The Pi Dot is `pebble/pi_pebble.py`
(pins and run steps in `pebble/README.md`); it talks to `server/tap_server.py` over HTTP.

## Connecting real calendars

In the demo, a Google service account reads and writes the two seeded calendars, which
are shared with it, so nobody signs in. A real user would connect with Google sign-in
(OAuth) and grant calendar access. Their agent would then read that calendar and add
holds to it the same way. The other person's agent still only sees free windows.
