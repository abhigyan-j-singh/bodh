"""`bodh.cut` was the module of the cut until October 2026; it is `bodh.cutting` now. This name
goes away in the release after 1.0.0."""

import warnings

from .cutting import *  # noqa: E402,F401,F403

warnings.warn(
    "bodh.cut is bodh.cutting since October 2026; the old name goes away in the next release",
    DeprecationWarning,
    stacklevel=2,
)
