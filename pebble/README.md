# Pi pebble

`pi_pebble.py` runs one pebble on a Raspberry Pi and talks to `server/tap_server.py`.

| Part | Pi pin (BCM) | Notes |
|---|---|---|
| RGB LED red / green / blue | 27 / 22 / 23 | common cathode, 100 ohm on each color leg |
| Pressure sensor (FSR) | 24 | 10K pulldown to GND, pressed = high |
| Hall sensor KY-003 (S) | 17 | low when a magnet is near |
| USB sound card | `plughw:CARD=Device,DEV=0` | mic + speaker |

LED: dim white = idle; bright white = tapped, or a time is ready for you; yellow
breathing in and out = the agents are deciding (right after the tap, and again after anyone
asks for a change); blue = the Dot is talking; solid bright yellow = the mic is on; green =
booked on both calendars (60 s, then dim white). No red.

There is no question up front: tap both Dots within 5 s (the server's
`KETCHUP_PAIR_WINDOW_S`) and the agents pick a day and time from both calendars in the
background. On pairing, Mayo's Dot says "Dots connected. We will get back to you once we are done."
slowly, with a pause after each sentence (made with MMS at startup, so it plays at once); its LED
stays bright white while it plays, then breathes yellow. Frankie's Dot stays silent. When a time is ready the LED
stops breathing and goes bright white, and the Dot says nothing until you tap it or press the
sensor to hear it ("How about Tuesday at 6 PM? ... Want to change it?"). Then answer: "keep it",
or a change like "no, pick a time at night". A change sends both agents back to deciding,
and once you have answered, each new time is spoken as soon as it is ready. When both
owners keep a time it is booked.

The pressure sensor is a toggle: press once and the mic turns on (the terminal prints
`MIC ON`), press again and it turns off and sends (`MIC OFF`). It stops on its own after
20 s. A press while the Dot is talking cuts the voice off and turns the mic on. Presses
are read on the edge with a 50 ms debounce, so one press is one toggle. The mic is
never always-listening.

The clip goes to Muse Voice Transcribe, the text goes to your agent, and the reply is
spoken by Meta MMS-TTS (`facebook/mms-tts-eng`, open weights, CC-BY-NC 4.0), which runs
on the pebble. It speaks sentence by sentence: the next sentence is made while the current
one plays, the model's silence at the start and end of each sentence is cut, and there is
a 150 ms pause between sentences. One `aplay` plays the whole reply. espeak (an OS voice,
not a model) speaks until MMS has loaded, and is the fallback if it is not installed. The
temp WAV is deleted as soon as it is read. The terminal prints `TIME first voice ... s
after the text` for each reply.

## Run on the Pi

```
sudo apt install -y espeak-ng python3-gpiozero python3-venv alsa-utils
python3 -m venv ~/pebvenv --system-site-packages     # keeps gpiozero
~/pebvenv/bin/pip install torch --index-url https://download.pytorch.org/whl/cpu
~/pebvenv/bin/pip install transformers
export META_API_KEY=...                         # Meta Model API key
export KETCHUP_SERVER=http://<laptop-ip>:8787   # where tap_server.py runs
export PEBBLE_ID=mayo                           # or frankie
~/pebvenv/bin/python pi_pebble.py
```

The first run downloads the MMS model (about 145 MB) from Hugging Face. The pebble
prints `VOICE Meta MMS-TTS ready` once it has loaded.

Optional: `PEBBLE_AUDIO` (ALSA device), `PEBBLE_PHRASE` (the plan, e.g. "let's
catch up Thursday"), `PEBBLE_GREEN_S` (green hold, default 60), `PEBBLE_TTS=espeak`
(skip MMS and use espeak only), `PEBBLE_BREATHE_S` (one breath, default 3),
`PEBBLE_ANNOUNCE=auto` (speak every new time without waiting for a tap/press; default `tap`),
`PEBBLE_SAY_CONNECTED` (`1` = say the connected line on pairing; default `1` for mayo, `0`
otherwise), `PEBBLE_CONNECTED_LINE` (default "Dots connected. We will get back to you once we are done."),
`PEBBLE_CONNECTED_SPOKEN` (what MMS reads for that line; the default line is read as
"dawts connekted. we will get back to you once we are done." so MMS says "Dots connected" clearly,
for this line only; espeak and the terminal use the written line),
`PEBBLE_CONNECTED_RATE` (MMS speaking rate for that line, default 0.75; other speech is 0.85),
`PEBBLE_CONNECTED_PAUSE_MS` (pause between its sentences, default 450; 200 ms tail),
`PEBBLE_CONNECTED_ESPEAK_WPM` (espeak speed for it, default 130),
`PEBBLE_TALK_MIN_S` (after a change, breathe at least this long, default 2.5),
`PEBBLE_DIM` (idle brightness, default 0.12), `PEBBLE_DEBOUNCE_MS` (press debounce, default
50), `PEBBLE_REC_MAX_S` (mic auto-stop, default 20), `PEBBLE_GAP_MS` (pause between
sentences, default 150).

## Try it without a Pi

`scripts/sim_dot.py` runs this file with mock GPIO pins, stub `arecord`/`espeak`/`aplay`
and a stub for speech recognition, against a local `tap_server.py --dry-run`.
`scripts/sim_dots.py` runs both Dots at once (Mayo on this Pi code, Frankie on the UNO Q
code): tap, the agents pick a time, Mayo presses while the Dot reads it out (the voice stops,
the mic turns on), says "no, pick a time at night", presses again, they pick again, both keep it.
Each press has a 10 ms bounce in it. Each Dot checks its LED went dim white, bright white,
yellow breathing, bright white, blue, yellow (mic on), ..., green, dim white, and that every
MIC ON has one MIC OFF. Needs `pip install gpiozero` for the Pi side.
