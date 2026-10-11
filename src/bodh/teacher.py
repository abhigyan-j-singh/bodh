"""`bodh.teacher` was the module of the judge until October 2026; it is `bodh.judge` now. This name
goes away in the release after 1.0.0."""

import warnings

from .judge import *  # noqa: E402,F401,F403

warnings.warn(
    "bodh.teacher is bodh.judge since October 2026; the old name goes away in the next release",
    DeprecationWarning,
    stacklevel=2,
)
