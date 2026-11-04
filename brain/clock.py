"""The brain's one clock. KETCHUP_NOW (ISO time) freezes it, and a replay freezes it at the
recorded time, so the prompts (dates, free times) match the recording."""
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import replay

TZ = ZoneInfo("America/New_York")
AT = os.environ.get("KETCHUP_NOW") or (replay.recorded_now() if replay.ON else None)
FROZEN = datetime.fromisoformat(AT).astimezone(TZ) if AT else None


def now():
    return FROZEN or datetime.now(TZ)
