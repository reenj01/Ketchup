"""Meeting lengths are always whole multiples of 5 minutes.

Anything else is rounded DOWN (37 -> 35, 37.5 -> 35, 52 -> 50), never below
`length_min` (15) or above `length_max` (240). A middle ground is
floor(average / 5) * 5, so 30 and 45 meet at 35, not 37. Used after every
answer mapping, every agent pick, and in any text shown or spoken.
"""
import json
import math
import re
from pathlib import Path

SETTINGS = json.loads((Path(__file__).resolve().parent.parent / "config" / "settings.json").read_text())

STEP = SETTINGS.get("length_step_min", 5)
LO = SETTINGS.get("length_min", 15)
HI = SETTINGS.get("length_max", 240)


def clean(minutes):
    """Round down to a multiple of STEP, clamped to LO..HI."""
    return min(HI, max(LO, int(math.floor(float(minutes) / STEP)) * STEP))


def middle(a, b):
    """The middle ground of two lengths, rounded down: 30 + 45 -> 35."""
    return clean((a + b) / 2)


_IN_TEXT = re.compile(r"\b(\d{1,3}(?:\.\d+)?)(\s*-?\s*)(min|mins|minutes|minute)\b", re.I)


def fix_text(s):
    """Rewrite any '37 min' / '37.5 minutes' in agent text to the clean number."""
    return _IN_TEXT.sub(lambda m: f"{int(float(m[1]) // STEP) * STEP}{m[2]}{m[3]}", s) if s else s
