# Ketchup Dot, App Lab side. App Lab needs a python/main.py; this one only keeps the App
# (and so the sketch) running. The Dot itself (mic, speaker, server) runs on the board's
# Linux shell, outside the App container, because it needs arecord/aplay/espeak-ng and the
# USB sound card:  python3 ~/ketchup/pebble/unoq/unoq_dot.py   (see pebble/unoq/README.md)
# It reaches the sketch through the same arduino-router the App uses.
from arduino.app_utils import App

print("Ketchup Dot sketch is running. Start the voice part: python3 pebble/unoq/unoq_dot.py", flush=True)
App.run()
