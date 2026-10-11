"""`bodh.control` was the module of the schedule's jobs until October 2026; it is `bodh.jobs` now. This name
goes away in the release after 1.0.0."""

import warnings

from .jobs import *  # noqa: E402,F401,F403

warnings.warn(
    "bodh.control is bodh.jobs since October 2026; the old name goes away in the next release",
    DeprecationWarning,
    stacklevel=2,
)
