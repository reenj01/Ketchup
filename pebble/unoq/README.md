# Ketchup Dot on the Arduino UNO Q (Frankie's Dot)

This is the same Dot as the Pi 5 one (`pebble/pi_pebble.py`), built for the UNO Q. It uses the same
server, endpoints, and environment variables, and the same lights and voice. The UNO Q has two halves:

- **The MCU (the STM32 side)** reads the magnet sensor and the pressure pad and drives the LED. This is
  `ketchup-dot/sketch/sketch.ino`.
- **Linux (the Qualcomm side)** handles the microphone, the speaker, the voice, and talking to the server
  on the Mac. This is `unoq_dot.py`. It imports `pi_pebble.py` and runs it unchanged. Only the pins are
  swapped for Bridge calls.

The two halves talk over Arduino's **Bridge** (the `arduino-router` service on the board).

```
magnet / pad ─► sketch.ino ──Bridge──► unoq_dot.py (+ pi_pebble.py) ──HTTP──► tap_server.py on the Mac
LED          ◄─ set_led()  ◄─Bridge──┘        │
                                    USB sound card (mic + speaker)
```

## Wiring (MCU header)

| Part | Pin | Notes |
|---|---|---|
| Hall sensor KY-003 | D2 | Signal pin. Active LOW. Internal pull-up is on. Power it from 5V/3.3V and GND |
| FSR (pressure pad) | D4 → GND | Internal pull-up is on. Pressed = LOW |
| RGB LED, common cathode | red D3, green D5, blue D6 | 100 Ω on each colour. Cathode to GND. These are PWM pins, so the breathing works |
| Waveshare USB sound card | USB-C hub | **Plug the wall charger into the hub's PD port.** The card does not work without it |

## Lights (same as the Pi)

- Dark: the Linux side isn't running yet. The sketch keeps the LED off until Linux sets it.
- Dim white: idle.
- Bright white: tapped, or a time is ready for you. Tap the Dot or press the pad to hear it.
- Yellow breathing in and out: the agents are deciding (after the tap, and again after anyone asks
  for a change). The sketch breathes the LED itself (`breathe_rgb` over Bridge); an older sketch
  without it still breathes, with Linux stepping `set_led`.
- Blue: the Dot is talking.
- Solid bright yellow: the mic is on ("keep it", or "no, pick a time at night").
- Green: booked on both calendars. Lasts about 60 s, then back to dim white.
- No red.

The pad is a toggle: press once to turn the mic on, press again to turn it off and send. It stops on
its own after 20 s. A press while the Dot is talking cuts it off and turns the mic on. It is never
always-listening.

## 1. Put the files on the board

From the Mac, with the board on the same Wi‑Fi (the default user is `arduino`):

```bash
scp -r pebble arduino@<unoq-ip>:~/ketchup/            # pi_pebble.py + unoq/
scp -r pebble/unoq/ketchup-dot arduino@<unoq-ip>:~/ArduinoApps/
```

You can also open **Arduino App Lab** on the Mac, connect to the board, create an app named
`ketchup-dot`, and paste in `app.yaml`, `sketch/sketch.ino`, `sketch/sketch.yaml` and `python/main.py`.
App Lab keeps apps in `/home/arduino/ArduinoApps/`.

## 2. Start the app (this flashes the sketch)

In App Lab, press **Run** on *Ketchup Dot*. Or, over ssh:

```bash
arduino-app-cli app start ~/ArduinoApps/ketchup-dot
arduino-app-cli app logs ~/ArduinoApps/ketchup-dot      # if anything looks wrong
# optional: start it on every boot
arduino-app-cli properties set default ~/ArduinoApps/ketchup-dot
```

The app's own `python/main.py` does nothing except keep the app (and the sketch) alive. App Lab runs
app Python inside a Docker container that has no `arecord`, `aplay`, or espeak and no sound card. So the
voice part runs directly on the board's Linux (step 4).

## 3. Install packages (once, over ssh)

```bash
sudo apt update
sudo apt install -y espeak-ng alsa-utils python3-venv
python3 -m venv ~/dotvenv
~/dotvenv/bin/pip install arduino-router-bridge      # Arduino's Python client for the Bridge
# optional, for the nicer MMS voice (the Pi uses this; espeak is the fallback):
~/dotvenv/bin/pip install torch --index-url https://download.pytorch.org/whl/cpu
~/dotvenv/bin/pip install transformers
```

If MMS is too slow or runs out of memory (the 2 GB board), use `export PEBBLE_TTS=espeak`.

## 4. Run Frankie's Dot

On the Mac, start the server as usual (`python server/tap_server.py`, port 8787). Then on the board:

```bash
cd ~/ketchup
export KETCHUP_SERVER=http://<mac-ip>:8787
export PEBBLE_ID=frankie                                 # the default here anyway
export PEBBLE_AUDIO=plughw:CARD=Device,DEV=0             # only if arecord -l shows a different name
read -s META_API_KEY; export META_API_KEY                # type it; it isn't shown or saved
~/dotvenv/bin/python pebble/unoq/unoq_dot.py
```

It prints `Ketchup Dot pebble-frankie -> http://<mac-ip>:8787 (UNO Q, Bridge)` and the LED goes white.

All the environment variables are the same as on the Pi:

- `KETCHUP_SERVER`, `PEBBLE_ID`, `PEBBLE_AUDIO`, `META_API_KEY`
- `PEBBLE_TTS` (`mms` or `espeak`), `PEBBLE_GREEN_S`, `PEBBLE_PHRASE`
- `PEBBLE_ANNOUNCE` (default `tap`: a ready time is said only on a tap or press),
  `PEBBLE_SAY_CONNECTED` (default `0` here, so Frankie's Dot stays silent on pairing)

These are UNO Q only:

- `DOT_POLL_MS`: how often the inputs are read. Default 25.
- `APP_SOCKET`: the router socket. Default `unix:///var/run/arduino-router.sock`.

## 5. Test it

1. **Magnet**: touch the magnet to the Hall sensor, then to Mayo's Dot (or `curl` its `/tap`). The LED
   breathes yellow while the agents pick a time, then goes bright white. Tap again or press the pad
   to hear the time (blue while it talks).
2. **Pad**: press the FSR once (`MIC ON`, solid yellow), say your answer, press again (`MIC OFF`). The reply is
   spoken. A bounce inside one press doesn't toggle twice.
3. A change makes both Dots breathe again until there's a new time. When both of you keep it, the LED goes green for about a minute, then dim white.

**Without the board** (on the Mac, server running with `--dry-run`):

```bash
.venv/bin/python server/tap_server.py --dry-run &
python scripts/sim_dots.py                # both Dots: tap → agents talk → a change → talk again → keep → green
pip install arduino-router-bridge gpiozero && python scripts/sim_dots.py --router   # Frankie through the real Bridge client
python scripts/sim_dots.py --router --old-sketch   # a sketch without breathe_rgb(): Linux steps set_led
python pebble/unoq/unoq_dot.py --mock     # type t = tap, p = press, r = release, q = quit
```

`sim_dot.py` stubs out the mic, the speaker, and speech recognition. The Dot side calls no Meta APIs
during the sim.

## Troubleshooting

**No sound or no mic**

- `lsusb` should list the Waveshare card ("USB Audio" / "Device").
- `arecord -l` and `aplay -l` show the card name. Set `PEBBLE_AUDIO=plughw:CARD=<name>,DEV=0` to match.
- If the card is missing or keeps dropping, **the wall charger must be in the hub's PD port**. Laptop or
  phone power isn't enough.
- Quick mic check: `arecord -D plughw:CARD=Device,DEV=0 -f S16_LE -r 16000 -c 1 -d 3 /tmp/t.wav && aplay -D plughw:CARD=Device,DEV=0 /tmp/t.wav`

**"Cannot reach arduino-router"**

- Run `systemctl status arduino-router`.
- Check `ls -l /var/run/arduino-router.sock`. Your user needs read/write access. If it's root-only, try
  once with `sudo -E`, or add yourself to the socket's group.

**LED stays dark**

- The sketch isn't running (start the app again, step 2), or `unoq_dot.py` isn't running.

**LED works but taps or presses do nothing**

- Check the wiring. The Hall signal goes to D2 and the FSR goes between D4 and GND.
- `unoq_dot.py --mock` lets you test the Linux side on its own.

**`get_inputs` / `set_led` errors in the log**

- The app's sketch isn't the Ketchup Dot sketch. Re-run the app from step 2.

## Fallback (if the Bridge won't cooperate on Wednesday)

The voice and server logic doesn't depend on the Bridge. Only `RouterBoard` in `unoq_dot.py` does. In
order of effort:

1. Run the same Dot on the Pi 5 (`pebble/pi_pebble.py`). It's proven.
2. On the UNO Q, have the sketch print taps and presses over `Monitor` and read them from Linux instead
   of the Bridge.
3. Use the web sim / `--mock` for the demo.

## How it uses the Bridge (calls used, with sources)

- **Sketch**: `#include "Arduino_RouterBridge.h"`, `Bridge.begin()`, and
  `Bridge.provide_safe("set_led", ...)` / `Bridge.provide_safe("get_inputs", ...)`.
  - `provide_safe` runs the function inside `loop()`'s context, so normal Arduino calls such as
    `analogWrite` are safe. That's why `loop()` never blocks.
  - Docs: https://docs.arduino.cc/software/app-lab/bridge/bridge-api/
  - Example: https://docs.arduino.cc/software/app-lab/bridge/get-started-with-bridge/
  - Library: https://github.com/arduino-libraries/Arduino_RouterBridge
- **Linux**: `from arduino.router_bridge import Bridge, DEFAULT_ADDRESS`, then
  `Bridge(addr).connect(timeout=5)`, `bridge.call("set_led", r, g, b, timeout=2)` and
  `bridge.call("get_inputs", timeout=2)`.
  - PyPI: https://pypi.org/project/arduino-router-bridge/
  - Source: https://github.com/arduino/arduino-router-bridge-py
- **Python only calls and never provides.** The `get_inputs` result packs two things: the number of
  magnet taps × 2, plus 1 if the pad is held. So a tap is never missed between polls.
- **App layout**: `app.yaml`, `python/main.py`, `sketch/sketch.ino` and `sketch/sketch.yaml`.
  - Docs: https://docs.arduino.cc/software/app-lab/apps/about-apps/
  - Blink example: https://github.com/arduino/app-bricks-examples/tree/main/core-and-foundational/01-led-blink/03-blinking-an-led-from-python
- **App CLI** (`arduino-app-cli app start/logs/stop`, apps in `~/ArduinoApps/`): https://docs.arduino.cc/tutorials/uno-q/debian-guide
  and https://docs.arduino.cc/tutorials/uno-q/ssh
- **Board pins** (PWM on D3, D5 and D6): https://docs.arduino.cc/hardware/uno-q/

The sketch compiles with `arduino-cli compile -b arduino:zephyr:unoq` (arduino:zephyr 1.1.0): 93.6 KB
flash (11%), 37.7 KB RAM (14%).
