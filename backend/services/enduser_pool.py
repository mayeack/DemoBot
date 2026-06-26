"""Pool of synthetic enduser_id values for the test/demo DemoBot app.

These are deliberately fake username-style identifiers used to populate the
`enduser_id` field on governance, escalation, audit, and error log events so
downstream analytics (Splunk, etc.) can group events by simulated end user.

This is a test-app population identifier, not a security primitive, so
`random.choice` is appropriate.
"""

import random
from typing import Final

ENDUSER_IDS: Final[tuple[str, ...]] = (
    "jsmith",
    "sarah.connor",
    "mrodriguez",
    "dkim",
    "t.nguyen",
    "aoyelaran",
    "lpatel",
    "mchen",
    "rgarcia",
    "kowens",
    "bwilliams",
    "ejohnson",
    "fzhang",
    "n.brown",
    "ahernandez",
    "j.lee",
    "mtaylor",
    "skowalski",
    "rdavis",
    "cmiller",
    "p.singh",
    "lwhite",
    "dthompson",
    "yokafor",
    "g.martinez",
    "h.robinson",
    "ebaker",
    "iwright",
    "qcarter",
    "wphillips",
    "ohall",
    "mlopez",
    "vyoung",
    "j.allen",
    "kscott",
    "rgreen",
    "tadams",
    "f.nakamura",
    "bperez",
    "u.murphy",
    "z.cooper",
    "a.bailey",
    "skim",
    "n.rivera",
    "l.morris",
    "p.jackson",
    "h.foster",
    "x.collins",
    "rstewart",
    "j.ramirez",
)

assert len(ENDUSER_IDS) == 50, "ENDUSER_IDS pool must contain exactly 50 entries"


def pick_enduser_id() -> str:
    """Return one of the 50 synthetic enduser_id values uniformly at random."""
    return random.choice(ENDUSER_IDS)
