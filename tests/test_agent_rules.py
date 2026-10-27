"""Code-enforced negotiation rules (no model calls): no repeats, said windows first."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain"))
from agents import allowed_picks, problem  # noqa: E402

slots = [(None, "noon"), (None, "night"), (None, "morning"), (None, "night")]
# going first: own said window wins, even though the habit would say otherwise
assert allowed_picks(slots, set(), "night", "noon", True) == [2, 4]
# no answer of my own: the other person's said window, not my habit
assert allowed_picks(slots, set(), None, "noon", True) == [1]
# counter: any said window, minus everything already offered
assert allowed_picks(slots, {2, 1}, "night", "noon", False) == [4]
# all said windows used up: any slot not yet offered
assert allowed_picks(slots, {1, 2, 4}, "night", "noon", False) == [3]
# re-proposing an offered slot is caught; accepting is always fine
assert problem({"decision": "counter", "pick": 2}, [4])
assert problem({"decision": "accept", "pick": 2}, [4]) is None
assert problem({"decision": "counter", "pick": 4}, [4]) is None
print("OK: repeats blocked, said windows beat habits")
