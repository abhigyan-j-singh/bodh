"""`bodh.learner` was the module of the profiler until October 2026; it is `bodh.profiler` now. This name
goes away in the release after 1.0.0."""

import warnings

from .profiler import *  # noqa: E402,F401,F403

warnings.warn("bodh.learner is bodh.profiler since October 2026; the old name goes away in the next release",
              DeprecationWarning, stacklevel=2)
