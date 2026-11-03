#!/bin/bash
# Demo day on the Mac: one command. Loads .env, starts the real tap server (books on the
# calendars), keeps the Mac awake, and prints a line as each Dot checks in:
#   DOT ONLINE  pebble-mayo (Mayo's Dot)
#   DOT ONLINE  pebble-frankie (Frankie's Dot)
#   BOTH DOTS ONLINE: ready to film
# Usage (from the repo):  bash scripts/demo_up.sh            (extra args go to tap_server.py,
#                         e.g. bash scripts/demo_up.sh --dry-run   to never book)
# Stop: Ctrl-C.
cd "$(dirname "$0")/.." || exit 1
PORT=8787
if [ -f .env ]; then set -a; . ./.env; set +a; else echo "no .env here: using the mock calendar"; fi
PY=.venv/bin/python; [ -x "$PY" ] || PY=python3
if command -v lsof >/dev/null && lsof -ti tcp:$PORT -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port $PORT is already in use (an old tap server?). Stop it first: kill \$(lsof -ti tcp:$PORT -sTCP:LISTEN)"
  exit 1
fi
NAME=$(scutil --get LocalHostName 2>/dev/null || hostname -s)
echo "Dots should have KETCHUP_SERVER=http://$NAME.local:$PORT in ~/ketchup-dot.env"
echo "Power on both Dots now. Waiting for them to check in (about 30-60 s after boot)..."
AWAKE=(); command -v caffeinate >/dev/null && AWAKE=(caffeinate -i)   # don't let the Mac sleep mid-demo
exec "${AWAKE[@]}" "$PY" -u server/tap_server.py --port "$PORT" "$@"
