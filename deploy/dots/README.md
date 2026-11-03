# Dots start themselves on boot

After a one-time install, demo day is:

1. On the Mac, in the repo: `bash scripts/demo_up.sh`
2. Plug in both boards (Pi and UNO Q).
3. Wait for `BOTH DOTS ONLINE: ready to film` in the Mac terminal (about 30–90 s after power on).

Nothing else runs on the Mac, and nothing gets typed on the boards. Each Hall tap sends its
own `/tap`, so touching the two Dots together (or tapping each within 5 s,
`KETCHUP_PAIR_WINDOW_S`) pairs them. No curl is needed. On pairing both LEDs go bright white.
Mayo's Dot says "Dots connected! We're now starting auto schedule." (still bright white), then
breathes yellow while the agents pick a time; Frankie's Dot is silent and breathes right away. Nothing else is spoken until someone presses
their Dot's pad (or taps it).

## What's in here

| File | Goes to | What it does |
|---|---|---|
| `ketchup-dot-pi.service` | Pi: `/etc/systemd/system/ketchup-dot.service` | Starts `~/pi_pebble.py` on boot. Restarts 3 s after any exit, forever. |
| `ketchup-dot-unoq.service` | UNO Q: `/etc/systemd/system/ketchup-dot.service` | Starts the `ketchup-dot` App (the sketch), waits for `/var/run/arduino-router.sock`, then starts `unoq_dot.py`. Same restarts. |
| `ketchup-dot-pi.env.example` / `ketchup-dot-unoq.env.example` | `~/ketchup-dot.env` on each board (chmod 600) | `KETCHUP_SERVER`, `PEBBLE_ID`, `PEBBLE_AUDIO`, `PEBBLE_TTS`, `META_API_KEY`. The key lives only here, never in the unit or in shell history. |
| `dot_heartbeat.py` | Pi: `~/dot_heartbeat.py`; UNO Q: `~/ketchup/deploy/dots/` | Runs the Dot. While it's alive, it POSTs `/hello` every 5 s, so the Mac prints `DOT ONLINE`. |
| `../../scripts/demo_up.sh` | Mac | Loads `.env`, starts the real `tap_server.py` (it books), keeps the Mac awake with `caffeinate`, and prints the check-in lines. |

The server prints one line per change: `DOT ONLINE  pebble-mayo (Mayo's Dot)`, `DOT ONLINE  pebble-frankie
(Frankie's Dot)`, `BOTH DOTS ONLINE: ready to film`, and `DOT OFFLINE ...` after 15 s with no check-in.
`curl http://localhost:8787/health` shows the same thing as JSON.

## The Mac's name (so you never edit an IP)

The boards find the Mac by its Bonjour name. On the Mac, run:

`scutil --get LocalHostName`

If it prints `Ireens-MacBook-Air`, the line in each board's `~/ketchup-dot.env` is
`KETCHUP_SERVER=http://Ireens-MacBook-Air.local:8787`. `demo_up.sh` prints this line too.

On the first run, macOS may ask whether Python can accept incoming connections. Click **Allow**.

## One-time install: Pi (Mayo's Dot)

On the Mac, in the repo folder, type one line at a time:

`scp pebble/pi_pebble.py deploy/dots/dot_heartbeat.py deploy/dots/ketchup-dot-pi.service ireenj@ireenpi.local:~/`

`scp deploy/dots/ketchup-dot-pi.env.example ireenj@ireenpi.local:~/ketchup-dot.env`

`ssh ireenj@ireenpi.local`

Then on the Pi:

`nano ~/ketchup-dot.env`

Set `YOUR-MAC-NAME` and paste the Meta key after `META_API_KEY=`. Use no quotes. Save with Ctrl-O, Enter, then Ctrl-X.

`chmod 600 ~/ketchup-dot.env`

`getent hosts YOUR-MAC-NAME.local`

This should print the Mac's IP. If it prints nothing, see "Can't find the Mac" below.

`pkill -f pi_pebble.py`

This stops any Dot you started by hand, because two copies would fight over the pins.

`sudo cp ~/ketchup-dot-pi.service /etc/systemd/system/ketchup-dot.service`

`sudo systemctl daemon-reload`

`sudo systemctl enable --now ketchup-dot`

`journalctl -u ketchup-dot -f`

You should see `Ketchup Dot pebble-mayo -> http://...:8787`, then `heartbeat ... ok` once the Mac server is up. Press Ctrl-C to stop watching. The Dot keeps running.

## One-time install: UNO Q (Frankie's Dot)

`UNOQ` below is the board's address: `ardunioUnoQ.local` (the name in its prompt) or its IP.

On the Mac, in the repo folder:

`scp -r pebble arduino@UNOQ:~/ketchup/`

`scp -r pebble/unoq/ketchup-dot arduino@UNOQ:~/ArduinoApps/`

`ssh arduino@UNOQ mkdir -p ketchup/deploy/dots`

`scp deploy/dots/dot_heartbeat.py deploy/dots/ketchup-dot-unoq.service arduino@UNOQ:~/ketchup/deploy/dots/`

`scp deploy/dots/ketchup-dot-unoq.env.example arduino@UNOQ:~/ketchup-dot.env`

`ssh arduino@UNOQ`

Then on the board:

`nano ~/ketchup-dot.env`

Set `YOUR-MAC-NAME` and the key, the same as on the Pi.

`chmod 600 ~/ketchup-dot.env`

`getent hosts YOUR-MAC-NAME.local`

If it prints nothing, run `sudo apt install -y libnss-mdns avahi-daemon` and try again.

`arduino-app-cli app list`

Find the app's name. It's probably `user:ketchup-dot`.

`arduino-app-cli properties set default user:ketchup-dot`

This is the same as App Lab's **Run at startup** toggle (from Arduino's docs, [Run at Startup](https://docs.arduino.cc/software/app-lab/apps/run)), so the sketch starts on power-on. The service also starts the App itself, so this step is a backup. Check it with `arduino-app-cli properties get default`.

`pkill -f unoq_dot.py`

`sudo cp ~/ketchup/deploy/dots/ketchup-dot-unoq.service /etc/systemd/system/ketchup-dot.service`

`sudo systemctl daemon-reload`

`sudo systemctl enable --now ketchup-dot`

`journalctl -u ketchup-dot -f`

You should see `Ketchup Dot pebble-frankie -> http://...:8787 (UNO Q, Bridge)`. The first start can take a minute or two while the App flashes the sketch.

## Everyday commands (on either board)

- Watch the logs: `journalctl -u ketchup-dot -f`
- Is it running: `systemctl status ketchup-dot`
- After copying new code: `sudo systemctl restart ketchup-dot`
- Stop it until the next boot: `sudo systemctl stop ketchup-dot`
- Stop starting it on boot: `sudo systemctl disable ketchup-dot`
- Stop it now and on every boot: `sudo systemctl disable --now ketchup-dot`
- UNO Q, stop the App from starting on boot: `arduino-app-cli properties set default none`

To run a Dot by hand again, for debugging, first stop the service with `sudo systemctl stop ketchup-dot`.

## Troubleshooting

**Can't find the Mac** (`heartbeat ... not reachable yet` keeps showing)

- Is `demo_up.sh` running?
- Are the Mac and both boards on the same Wi-Fi?
- Venue or guest Wi-Fi often blocks `.local` names and device-to-device traffic. A phone hotspot shared by all three avoids that.
- As a last resort, put the Mac's IP in `~/ketchup-dot.env` (`ipconfig getifaddr en0` on the Mac shows it), then run `sudo systemctl restart ketchup-dot`.

**UNO Q: `Cannot reach arduino-router`** in the log

The service retries every 3 s on its own. If it keeps failing:

- Check `systemctl status arduino-router`.
- Check `ls -l /var/run/arduino-router.sock`. The `arduino` user needs read/write access.
- Check `arduino-app-cli app list`. The App must be running.

**Pi: no sound**

The service runs as `ireenj`, the same as by hand. If `arecord` works by hand but not in the service, check that `PEBBLE_AUDIO` in `~/ketchup-dot.env` matches `arecord -l`.

**The voice sounds robotic at first** (Pi)

That's espeak. It speaks until MMS has loaded, which takes about a minute after boot. Power the Pi on a couple of minutes before filming.
