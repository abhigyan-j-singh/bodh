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
and exits; a bug keeps its traceback in the log.

## Stored words

Database column names and stored values keep their first spelling (`bucket`, `exploration`,
`floor`, `bankruptcy`, `blast`, `nudge`, `wrong`, `second_look`, `teacher`); the glossary's
names are used at the boundary (`store.cut_mode_name` and friends, the console, `--json`).
Schema changes are numbered migrations in `store.py`, each idempotent and tested from the
previous shape; the kv keys are the named constants in `store.py`.
