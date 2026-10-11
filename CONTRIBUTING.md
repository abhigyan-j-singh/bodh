# Contributing

Bodh is one person's reading tool made shareable; changes are welcome when they keep it
small, honest and cheap to run. This file says how to check a change, how to add a test, a
reader or a model provider, and what the mutation run found. `docs/internals.md` has the rules
the code keeps; `docs/glossary.md` the words.

## Checks

```bash
python -m pip install -e ".[dev]"
make check        # ruff, ruff format --check, mypy --strict, the suite (what CI runs on every push)
make coverage     # the suite with coverage, and the gate: 85% on store, cutting, feedback, judgment, run, config
make perf         # the 50,000-article timings (BODH_PERF=1); CI runs them on Linux
make audit        # pip-audit over the installed dependencies
pre-commit install
```

A change is ready when `make check` is green on Python 3.10 and 3.13, the new behaviour has a
test, and `CHANGELOG.md` says what a user would notice. No test may use the network, the wall
clock (patch `time.time` or `store.utcnow`) or another test's leftovers; a flaky test is fixed,
never retried.

## Adding a test

The suite runs offline against the fakes in `tests/fakes.py`: `FakeReader` (the BazQux client's
interface in memory, every write recorded), `FakeBazQuxServer` (the HTTP API behind
`httpx.MockTransport`, with the failures seen live as knobs: a stale token, a bad login, a 500
once, a looping continuation), `FakeAnthropic` (the model SDK, scripted by title: `gold` is
excellent, `junk` is junk, `edge` is saved by the review; a failure on the nth call, structured
outputs or the 1-hour cache rejected once, batches that expire, are cancelled or vanish, prose
instead of JSON) and `FakeLLM`, the real `llm.LLM` over it. `conftest.seed_scored` puts judged
articles in a database in one line. A new behaviour gets one test named after it, with a
docstring that says what it pins; the failure drills in `tests/test_drills.py` show the shape
for anything that goes wrong at run time.

A prompt change changes `tests/snapshots/`; accept it with `BODH_UPDATE_SNAPSHOTS=1 pytest
tests/test_judgment.py` and say in `CHANGELOG.md` whether it re-judges (a new
`judge.prompt_version` does; a rewording under the same version only rewrites the cached prefix).

## Adding a reader or a provider

A reader is anything that lists unread and starred ids, serves contents and label streams,
and writes tags and read marks; a provider is anything that answers a judgment request,
synchronously or in a batch. Each has a contract suite in `tests/contracts/`, parametrised over
the implementations: add yours to the fixture's list and make it pass against a fake of its
own service (never the live one). The suite is what "works" means here; a reader or provider
that cannot pass it is not accepted, and one that passes needs no further argument.

## What the mutation run found

`mutmut` ran once over `cutting.py`, `judgment.py` and `feedback.py` (October 2026): 1,599
mutants, 1,450 killed, 149 accepted. Most of the first pass's 316 survivors were killed by
tests that pin a boundary the suite had only reached by accident: a score exactly at the
cut-off survives, the safety limit's share is compared strictly, the prune's `keep` beats the
configured default, new ground needs quality 60 and a concept not read in the window (which
is not capped at the store's default of 40 concepts), `clear-old`'s cut-off is in microseconds
and stars, protected articles and rescues are each exempt, `finish_pending_cuts` writes the
flags as exactly 1 (the rescue inference reads them), reads are inferred to the millisecond of
the seven-day window, a mass flip is believed at exactly 80% and suspected at exactly 20
articles. The 149 accepted survivors, by class:

- A `sqlite3.Row` key written in upper case (45): Row access is case-insensitive.
- Cosmetic text (37): log lines and their arguments, exception messages, notes.
- SQL keywords or identifiers re-cased (9): SQLite does not care.
- Provably equivalent for the data the function receives (58): `rescued=None` where the
  store reads it as false; a crawl-time tie-break the SQL ordering already settles; a
  new-ground early return with a quota of 0; `limit=501` against 500 known concepts; a
  concept string used only as a set key; `zip(strict=...)` after a length check; `or ""`
  defaults that land in the same "not recognised" branch; `"medium"` removed from the accepted
  confidences when "medium" is the fallback; an unreachable `except` after `isdecimal()`; the
  fence stripping before a brace search that takes the same slice; a `dict(verdict)` that
  equals `model_dump()` for flat fields; a duplicate-index threshold moved on one side only;
  initial statistics overwritten before they are returned; an idempotency check before an
  `INSERT OR IGNORE`.

Run it again after a change to those three modules: `pip install mutmut`, then from a copy of
the checkout without the `pythonpath` setting in `pyproject.toml` (mutmut sets the path
itself), `mutmut run` with `paths_to_mutate = ["src/bodh"]` and `only_mutate` naming the
module. The signal-driven tests (`test_locks_and_logs.py`, `test_jobs_and_console.py`) are
left out of the copy: the runner ignores SIGINT and the child processes inherit that.
