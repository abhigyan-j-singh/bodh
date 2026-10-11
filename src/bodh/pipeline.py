"""`bodh.pipeline` was the module of the run until October 2026; it is `bodh.run` now. This name
goes away in the release after 1.0.0."""

import warnings

from .run import *  # noqa: E402,F401,F403

warnings.warn(
    "bodh.pipeline is bodh.run since October 2026; the old name goes away in the next release",
    DeprecationWarning,
    stacklevel=2,
)
