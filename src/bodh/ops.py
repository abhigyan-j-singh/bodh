"""`bodh.ops` was split in October 2026 into `bodh.locks` (the run lock, the pid file, the timeout,
the signals), `bodh.logs` (the log file, the redactor) and `bodh.heartbeat`. This name goes away in
the release after 1.0.0."""

import warnings

from .heartbeat import *  # noqa: F403
from .locks import *  # noqa: F403
from .logs import *  # noqa: F403

warnings.warn(
    "bodh.ops is bodh.locks, bodh.logs and bodh.heartbeat since October 2026; the old name goes away "
    "in the next release",
    DeprecationWarning,
    stacklevel=2,
)
