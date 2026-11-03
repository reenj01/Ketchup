# Pi pebble

`pi_pebble.py` runs one pebble on a Raspberry Pi and talks to `server/tap_server.py`.

| Part | Pi pin (BCM) | Notes |
|---|---|---|
| RGB LED red / green / blue | 27 / 22 / 23 | common cathode, 100 ohm on each color leg |
| Pressure sensor (FSR) | 24 | 10K pulldown to GND, pressed = high |
| Hall sensor KY-003 (S) | 17 | low when a magnet is near |
| USB sound card | `plughw:CARD=Device,DEV=0` | mic + speaker |

LED: solid white = idle, or a time is ready for you; slow white breathing = the agents
are talking (right after the tap, and again after anyone asks for a change); yellow =
you are holding the pressure sensor and talking; green = booked on both calendars (60 s,
then white). No red.

There is no question up front: on the tap the agents pick a day and time from both
calendars. When one is ready the LED stops breathing; tap the Dot or give the sensor a
short press to hear it ("How about Tuesday at 6 PM? ... Want to change it?"). Then hold
the sensor and answer: "keep it", or a change like "no, pick a time at night". A change
sends both agents back to talking, and once you have answered, each new time is spoken
as soon as it is ready. When both owners keep a time it is booked.

Hold the pressure sensor to talk. The mic records only while it is pressed. On
release the clip goes to Muse Voice Transcribe, the text goes to your agent, and
the reply is spoken by Meta MMS-TTS (`facebook/mms-tts-eng`, open weights,
CC-BY-NC 4.0), which runs on the pebble. espeak (an OS voice, not a model) speaks
until MMS has loaded, and is the fallback if it is not installed. The temp WAV is
deleted as soon as it is read.

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
`PEBBLE_ANNOUNCE=auto` (speak every new time without waiting for a tap/press),
`PEBBLE_TALK_MIN_S` (after a change, breathe at least this long, default 2.5).

## Try it without a Pi

`scripts/sim_dot.py` runs this file with mock GPIO pins, stub `arecord`/`espeak`/`aplay`
and a stub for speech recognition, against a local `tap_server.py --dry-run`.
`scripts/sim_dots.py` runs both Dots at once (Mayo on this Pi code, Frankie on the UNO Q
code): tap, the agents pick a time, Mayo says "no, pick a time at night", they pick again,
both keep it. Each Dot checks its LED went white, white breathing, white, yellow, white
breathing again, white, yellow, green, white. Needs `pip install gpiozero` for the Pi side.
