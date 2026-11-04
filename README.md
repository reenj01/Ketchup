# Ketchup

Two pocket Dots that turn "let's catch up sometime" into a meeting on both calendars.

**Try it live:** [Ketchup simulator](https://ireenjeang0726--ketchup-sim-web.us-east.modal.run). No login needed. Use See calendar to check the sample calendars the agents read.

"We should catch up" usually goes nowhere, because someone has to compare calendars
and pick a time. With Ketchup, two friends tap their Dots together, each person's
AI agent reads their own calendar, and the agents agree on a time and pencil it in.

Built for the Meta Global AI Developer Hackathon (Oct 26 to Nov 4, 2026), Indie track.

Everything in this repo was built during the event. Nothing was built before.

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

## Meta models

| Model ID (as in the code) | Where it runs | What it does in Ketchup |
|---|---|---|
| `meta-models/muse-spark-open-1026` (Muse Spark Open, open weights) | Baseten (hosted endpoint `inference.baseten.co`) | The main brain: parses the plan, infers habits from each calendar, runs both agents (propose, accept or counter in strict JSON), and turns spoken answers and change reasons into rules (`brain/parse_plan.py`, `brain/agents.py`) |
| `muse-spark-1.3` (Muse Spark) | Meta Model API (`api.meta.ai/v1/chat/completions`) | Fallback for the same requests when a Baseten call fails or returns bad JSON (`brain/parse_plan.py`) |
| `muse-voice-transcribe-1.0` (Muse Voice Transcribe) | Meta Model API (`api.meta.ai/v1/asr/transcribe`) | Speech to text for what the owner says to the Pi Dot (`pebble/pi_pebble.py`) and in the web simulator (`web/sim.py`) |
| `facebook/mms-tts-eng` (MMS-TTS, open weights, CC-BY-NC 4.0) | Our own compute, through Hugging Face `transformers`: on each Dot (Raspberry Pi 5, and the UNO Q's Linux side), and on the simulator's server (Modal) | The Dot's voice: says the proposed time and the replies (`pebble/pi_pebble.py`, `web/sim.py`) |

Every model Ketchup calls at runtime is a Meta model. Everything else is a plain OS
feature or library, not a model (see "Third-party and non-Meta components" below).

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
add your own key (next section).

## Use your own Meta Model API key

```
cp .env.example .env
```

Then open `.env` and set `META_API_KEY=` to your Meta Model API key. It is used for
`muse-spark-1.3` (the brain when there is no Baseten key, and the fallback otherwise) and for
`muse-voice-transcribe-1.0` (speech to text). To run the main brain, Muse Spark Open, on
Baseten instead, also set `BASETEN_API_KEY=`. With either key set, every call is live (no
replay); `KETCHUP_REPLAY=1` forces the replay anyway. `.env` and `service-account*.json` are
in `.gitignore`: never commit a key.

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
- `META_API_KEY`: Meta Model API: `muse-spark-1.3` and `muse-voice-transcribe-1.0`
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

## Third-party and non-Meta components

None of these is an AI model, except MMS-TTS, which is a Meta model.

- **Voice output:** Meta MMS-TTS (`facebook/mms-tts-eng`) is the Dot's voice. espeak-ng, an
  OS speech synthesizer (non-Meta, not an AI model), speaks while MMS loads or if it is not
  installed. The organizers allow non-Meta text-to-speech for voice output only.
- **Audio and pins:** ALSA `arecord`/`aplay` (mic and speaker), `gpiozero` (the Pi's LED and
  sensors; mock pins in `scripts/sim_dots.py`), and the Arduino App Lab Bridge on the UNO Q
  (its sketch drives the LED and reads the sensors).
- **Calendars:** the Google Calendar API through `google-auth` and
  `google-api-python-client`. Without a Google service account, a mock calendar
  (`config/mock_calendar.json`, made up for this project) is used.
- **Running MMS-TTS:** Hugging Face `transformers`, PyTorch and NumPy, on the Dots and the
  simulator's server.
- **Hosting:** Baseten hosts Muse Spark Open. Modal hosts the optional web simulator.
- **Fonts:** the live page (`web/live.html`) loads Bricolage Grotesque, Instrument Serif,
  Space Mono and DM Sans from Google Fonts (all under the SIL Open Font License).
- **Images and audio:** `docs/sim-screens/` are screenshots of the simulator.
  `replays/demo/mayo_says.wav` was made with MMS-TTS for the replay. No Meta logos are used.

## Provenance

- **Recorded model calls:** `replays/demo/calls.jsonl` holds 17 real Muse Spark Open calls
  (`meta-models/muse-spark-open-1026` on Baseten) and 1 Muse Voice Transcribe call
  (`muse-voice-transcribe-1.0`), recorded on Nov 3, 2026 from one run of
  `scripts/sim_dots.py` and `scripts/sim_dots.py --renegotiate` against the mock calendar.
  Each call is stored with the model's reply and reasoning, under a hash of the request
  (prompt text, or the audio bytes), so a replay only answers the exact same request.
  `replays/demo/meta.json` has the date and the frozen clock time.
- **Check it:** run the commands in "Run with no keys" above, or
  `.venv/bin/python tests/test_replay.py`, which runs both sims with every key removed.
- **Traces:** every run writes a trace to `traces/` with each model call, agent turn,
  timing and the events created. Sample traces from live runs are in the repo.
- **Filming run:** `bash scripts/demo_up.sh` saves the tap server's log to
  `provenance/<date>-tapserver.log` (key-like values are blanked first).
  - Tap server log of the filmed run: **TODO (RJ): add the provenance/ file name**
  - Live page screenshots from the filmed run: **TODO (RJ): add**
  - Demo video: **TODO (RJ): add the YouTube link**

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
