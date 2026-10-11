"""`bodh.bazqux` was the module of the BazQux reader until October 2026; it is `bodh.readers.bazqux` now. This name
goes away in the release after 1.0.0."""

import warnings

from .readers.bazqux import *  # noqa: E402,F401,F403

warnings.warn(
    "bodh.bazqux is bodh.readers.bazqux since October 2026; the old name goes away in the next release",
    DeprecationWarning,
    stacklevel=2,
)
