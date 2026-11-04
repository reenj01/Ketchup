"""Copy stdin to stdout and to a log file, with key-like values blanked (server/live.py redact()).
Used by scripts/demo_up.sh to keep the filmed run's tap server log in provenance/."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
from live import redact  # noqa: E402

with open(sys.argv[1], "a") as log:
    for line in sys.stdin:
        line = redact(line.rstrip("\n")) + "\n"
        sys.stdout.write(line)
        sys.stdout.flush()
        log.write(line)
        log.flush()
