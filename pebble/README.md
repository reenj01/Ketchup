# Pi pebble

`pi_pebble.py` runs one pebble on a Raspberry Pi and talks to `server/tap_server.py`.

| Part | Pi pin (BCM) | Notes |
|---|---|---|
| RGB LED red / green / blue | 27 / 22 / 23 | common cathode, 100 ohm on each color leg |
| Pressure sensor (FSR) | 24 | 10K pulldown to GND, pressed = high |
| Hall sensor KY-003 (S) | 17 | low when a magnet is near |
| USB sound card | `plughw:CARD=Device,DEV=0` | mic + speaker |

LED: white = idle, yellow = agents negotiating, red = a question for you (it is
spoken once), green = confirmed by both (60 s, then white).

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
(skip MMS and use espeak only).

## Try it without a Pi

`scripts/sim_pebble.py` runs the same file with mock GPIO pins and stub
`arecord`/`espeak`/`aplay`, against a local `tap_server.py --dry-run`, and
checks the LED goes white, yellow, red, green, white.
