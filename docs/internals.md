# Internals: the rules the code keeps

Short notes for whoever changes the engine. The glossary (`glossary.md`) has the words.

## Threads and connections

One SQLite connection per thread, never shared. The main thread owns the connection it
opened (`store.connect`) and every write to articles, scores, state, runs and feedback goes
through it. The model client (`llm.LLM`) runs its calls from worker threads (`judge.score_pending`,
`judge.reviews`) and records their costs and the two provider flags on a private connection of
its own, under its own lock: sqlite3 shares prepared statements per connection, and two threads
running the same statement on one connection hand each other empty results (seen live, in the
first catch-up). The judgments themselves are handed back to the main thread, which stores them.
`tests/test_judge_and_progress.py` runs sixteen workers over one judge to keep this honest.

## One run at a time

`locks.run_lock` is a flock on `data/bodh.lock`, released by the kernel when the process ends,
however it ends; `data/bodh.pid` beside it is advisory (pid, kind, start time) and is what
`bodh stop`, `bodh status` and the console read. A pid file naming a dead process is ignored and
overwritten. Without flock (Windows) the lock file holds the pid and a stale one is taken over.
`run_in_progress` probes the pid file first so the console's polling never steals the lock from
a run that is about to start. A run gets a deadline a minute short of its fifteen-minute limit:
no model call starts after it, and `locks.time_limit` is the backstop.

## Failing open

A run that cannot finish leaves the unread list as it was: cuts are written tag-then-read with
`finish_pending_cuts` on the next run, judgments are stored as they land, a review that could not
be had keeps its article, a BazQux answer that would flip most of the pool is believed only on
the second sighting, and the safety limit stops a run before any write. Every broad `except` in
the code says beside it why failing open is right there.

## Errors and exit codes

`errors.BodhError` and its kinds carry the exit codes the CLI uses: 2 configuration, 3 the
reader, 4 the model, 5 a run in progress, 1 anything else. A command prints one line on stderr
and exits; a bug keeps its traceback in the log. `bodh run` alone answers 0 when another run
holds the lock, since the timer's own overlap is not an error. The CLI's error line replaces any
secret value it finds in a message before printing it, and `llm._classify` scrubs the API key
from every error it turns into a `ModelConfigError`.

## Time

Every timestamp in the database is UTC ISO text with the offset (`store.utcnow_iso`); the
costs table is compared as text, so the budget's "today" is `store.local_midnight_iso`, the
start of the day in the student's zone turned into the same UTC text, which is right across a
DST change (a 25-hour day) and in a half-hour zone. Local time is computed for that and for
display, nowhere else. `tests/test_store.py` stands in London and Kolkata to check it.

## Backups and the rebuild

The day's first run copies the database with `VACUUM INTO` to `data/backups/bodh-<date>.db`
and keeps seven (`recovery.backup_daily`; a failure is a log line). `recovery.rebuild` reads
the band tags, the cut tag and the stars back from BazQux into an empty or thinner database in
one transaction: a rebuilt score carries the band's threshold as every sub-score,
`model = "rebuilt"` and a low confidence, so autocut leaves it alone while a prune counts it;
the unread ones at or above `catch_up.rejudge_at` are pending for the judge again, as a quick
judge's would be. Nothing BazQux shows is changed by a rebuild.

## Performance

`scripts/make_fixture_db.py` invents a year of daily use (50,000 articles) in nine seconds;
`tests/test_perf.py` (`BODH_PERF=1`, CI on Linux) times the pending query (budget 100 ms), the
dashboard render (1 s), the progress snapshot (500 ms), a whole run with the reader and the
model faked (5 s) and the feedback pass over half the pile (1 s), failing at three times the
budget. The rules that keep them: a bulk write is one transaction (`store.bulk`), a phase change
writes the live file at most once a second, the per-folder counts walk the unread pile and the
status index rather than every article, and migration 5 holds the indexes for the queries by
time (`store.INDEXES`).

## The fakes and the contracts

The suite never touches the network. `tests/fakes.py` is the whole cast: `FakeReader` (the
client's interface in memory, writes recorded), `FakeBazQuxServer` (BazQux's HTTP API behind
`httpx.MockTransport`, with the failures seen live as knobs), `FakeAnthropic` (the SDK's
surface, scripted by title) and `FakeLLM`, the real `llm.LLM` over it, so cost accounting, the
budget, the fallbacks and the batch lifecycle run as in production. `tests/contracts/` holds
the suites a new reader or provider must pass; the fake reader passes the reader suite beside
the real client, so it cannot drift. `tests/test_drills.py` plays the failures a year brings,
one test each. `tests/snapshots/` holds the rendered prompts: a prompt change shows as a diff,
`BODH_UPDATE_SNAPSHOTS=1` accepts it, and `CHANGELOG.md` says whether it re-judges.

## Providers

`llm.LLM` is the one door to every model: it checks at construction that the three roles'
providers have their secret, builds a provider the first time a model of its kind is asked for
(`make_provider`), keeps the budget and the cost accounting, and turns a request into the
provider's shape. A provider (`providers/base.Provider`) answers a judgment, a completion and
the three batch calls, reports its capabilities per model (batch, structured output, prompt
cache) and says how to adapt after a rejected request shape; `HttpProvider` adds the client,
the bounded retry with backoff and jitter on 429, 5xx and connection errors, and the
translation of a status into the error kinds the run knows (401, 403 and a 404 naming the
model are `ModelConfigError`, exit 4; the rest is one failed call). A model is `ModelRef`:
`provider/model`, a bare id being Anthropic's; `ModelRef.name` is what the database stores and
the prices are keyed by. Usage is `Usage`, with `estimated=True` when the service reported
none; `LLM.record` remembers such models under `llm.estimated_usage_models` in `kv` and the
progress views say so. Five failed calls in a row halt the feeder in `Judge.score_pending`
(`CIRCUIT_BREAKER_FAILURES`); the stats carry the reason and the run and catch-up summaries
print it.

## Stored words

Database column names and stored values keep their first spelling (`bucket`, `exploration`,
`floor`, `bankruptcy`, `blast`, `nudge`, `wrong`, `second_look`, `teacher`); the glossary's
names are used at the boundary (`store.cut_mode_name` and friends, the console, `--json`).
Schema changes are numbered migrations in `store.py`, each idempotent and tested from the
previous shape; the kv keys are the named constants in `store.py`.
