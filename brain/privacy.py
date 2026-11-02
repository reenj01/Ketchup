"""What the OTHER person hears when someone asks for a change.

A person's own chat or pebble may echo their own words ("You said not
Thursday, so ..."). The other person never gets the requester's words or
reason, quoted or paraphrased: only a fixed template with the requester's
name and the new time. These lines are built here from templates, never from
model text, so nothing the requester said can leak through. The middle
agent-to-agent chat still shows the constraint; the agents need it.
"""


class Neutral:
    """A templated line for the other person. `kind`: "new" | "backup"."""

    def __init__(self, name, kind="new"):
        self.name, self.kind = name, kind

    def text(self, when):
        if self.kind == "backup":
            return f"{self.name} would prefer a different day or time, so I moved it to our backup: {when}."
        return f"{self.name} would prefer a different day or time, so here's a new one: {when}."

    def spoken(self, when):
        """The same template, short and plain for a pebble to say out loud."""
        if self.kind == "backup":
            return f"{self.name} wants a different time. I moved it to our backup, {when}."
        return f"{self.name} wants a different time. How about {when}?"


def both(q):
    """A compromise shown without revealing the other person's answer."""
    return f"For both of you, {q}"
