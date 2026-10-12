"""The errors bodh raises on purpose, and what each means at the command line: one plain
line on stderr and an exit code a script can act on. 2 the configuration, 3 the reader
(BazQux), 4 the model, 5 a run already in progress, 1 anything else."""

from __future__ import annotations


class BodhError(Exception):
    """The base of every error raised on purpose; the CLI prints `str(exc)` and exits with
    `exit_code`. Anything else that escapes is a bug and keeps its traceback."""

    exit_code = 1


class ConfigError(BodhError):
    """config.yaml, .env or the profile: missing, malformed or still the example."""

    exit_code = 2


class ReaderError(BodhError):
    """The feed service (BazQux) could not be reached, refused the login, or answered badly."""

    exit_code = 3


class ModelError(BodhError):
    """The model provider: a rejected key or model id, a budget reached, an answer with
    nothing in it."""

    exit_code = 4


class BudgetExceeded(ModelError):
    """The daily budget is reached; judging pauses until midnight."""


class ModelConfigError(ModelError):
    """The API rejected the key, the permission or the model id. Retrying is pointless;
    the run stops at once so the problem is visible instead of 27 identical tracebacks."""


class BatchNotFound(ModelError):
    """The API no longer knows the batch (deleted in the console, or submitted under another
    key): it is closed as lost and its articles are pending again, nothing else stops."""


class NoJudgments(ModelError):
    """The model answered, but with nothing a judgment could be read from."""


class LockedError(BodhError):
    """Another run holds the lock; the command refuses rather than run alongside it."""

    exit_code = 5
