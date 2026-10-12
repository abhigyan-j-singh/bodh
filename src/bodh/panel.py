"""`bodh.panel` was the module of the console until October 2026; it is `bodh.console` now. This name
goes away in the release after 1.0.0."""

import warnings

from .console import *  # noqa: F403

warnings.warn(
    "bodh.panel is bodh.console since October 2026; the old name goes away in the next release",
    DeprecationWarning,
    stacklevel=2,
)
