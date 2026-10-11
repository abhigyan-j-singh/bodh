# Changelog

Every change a user could notice, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/) from 1.0.0 on.

## [Unreleased]

### Renamed

One word per thing, the same word everywhere (`docs/glossary.md`). Every old name keeps working
for one release: an old command runs and prints the new name on stderr, an old configuration key
is mapped on load with one warning that lists the moves (`bodh doctor` lists them too), the old
tag is read forever, and the old file names are used in place when the new ones are absent. All
of the aliases go away in the release after 1.0.0.

- The project is **bodh** (बोध: understanding, insight); it was `triage`. The package, the command, the pip name,
  the launchd labels (`bodh.run`, `bodh.weekly`, `bodh.report`, `bodh.console`), the root
  variable (`BODH_HOME`), the database (`data/bodh.db`) and the log folder
  (`~/Library/Logs/bodh`) carry the new name. The `triage` command runs with a note, `TRIAGE_HOME`
  is honoured with a warning, an existing `data/triage.db` is kept as it is, and
  `./install-launchd.sh` boots out the old `com.antriksh.triage*` jobs when it finds them. The log
  moves to `~/Library/Logs/bodh/`; the old folder can be deleted. To upgrade an install from before
  the rename: `triage halt`, update the checkout, `./install.sh && ./install-launchd.sh`.
- Commands: `bodh catch-up` (was `blast`; the quick judge is now its default model), `bodh clear-old
  --days N` (was `bankruptcy --older-than N`), `bodh stop --all` (was `halt`), `bodh autocut on|off`
  (was `enforce`), `bodh console` (was `panel`), `bodh run --shadow` (was `score`), `bodh mark ID
  great|good|meh|bad|basic|heavy|more|less|rescue` (was `label ID star|junk`), `bodh steer more|less
  "x"` (was `want "x"` and `meh "x"`), `bodh schedule pause --job console` (was `--job panel`).
- Configuration sections and keys. `models.judge`, `models.reviewer`, `models.profiler` (were
  `teacher`, `second_look`, `learner`). `judge.*` (was `scoring.*`): `articles_per_call` (was
  `batch_size`), `text_chars` (was `opening_chars`); `review.window|chars|max_per_run` (were
  `scoring.second_look_window|_chars|_max_per_run`); `catch_up.rejudge_at` (was
  `scoring.rejudge_min_score`) and the new `catch_up.model`; `limits.per_run_sync|per_run_batch|
  parallel_calls|max_age_days` (were `scoring.max_per_run|batch_max_per_run|workers|max_age_days`).
  `autocut.enabled|cutoff|max_per_run|new_ground_share` (were `cut.enforce|default_floor|
  max_cuts_per_run|exploration_share`); `prune.keep|max_share_per_concept` (were
  `cut.target_unread|concept_cap`); `protect.must_read_stream` (was `cut.must_read_stream`);
  top-level `folders` (was `cut.folders`) with `cutoff` and `judge` per folder (were
  `auto_cut_below` and `score`); `tags.bands` (was `buckets`), `tags.rescue` (was `wrong`);
  `safety.*` (was `breakers.*`): `min_judged` (was `min_new_for_breaker`), `lowered_cutoff`
  (was `lowered_floor`), `mark_all_read_threshold` (was `bulk_dismiss_threshold`);
  `profile.concept_window_days` (was `feedback.concept_window_days`).
- The tag `rescue` (was `wrong`), read under both names for good.
- The student profile: `profile/about.md` (was `seed.md`) and `profile/learned.md` (was
  `learner.md`, "the learner model"); `./install.sh` renames an existing pair, and the old names
  are read in place until then. The `## Steers` heading in the learned profile (was `## Nudges`).
- The words on the console, in `bodh status` and `bodh progress`, in the logs and on the dashboard:
  the judge (was the teacher), the review (was the second look), the profiler (was the learner),
  the cut-off (was the floor), autocut (was enforcing, the floor), scoring-only mode (was shadow
  mode), the safety limit (was the breaker), the console (was the control room, the panel), with
  the model (was in flight), waiting for review (was held), the quick judge (was the stand-in), a
  band (was a bucket), new ground (was exploration), a steer (was a nudge), the unread list (was
  the pile), the learned profile (was the learner model). The stored values in an existing
  database (`floor`, `bankruptcy`, `blast`, `nudge`, `wrong`) are shown under the new names; no
  migration rewrites them.
- The prompts: `review.v1` (was `second_look.v1`), `profiler.v1` (was `learner.v1`). The judge's
  prompt keeps its version string, `teacher.v2`, because a new one would re-judge the whole unread
  list; its reworded headings only rewrite the cached prefix once.
- The console's files: `data/console.json`, `data/console.log`, `~/Library/Logs/bodh/console.out.log`
  and `console.err.log` (were `panel.*`).
- The modules, for anyone importing the package: `bodh.judge` (was `teacher`), `bodh.profiler`
  (was `learner`), `bodh.cutting` (was `cut`), `bodh.run` (was `pipeline`), `bodh.jobs` (was
  `control`), `bodh.console` (was `panel`), `bodh.readers.bazqux` (was `bazqux`), and `bodh.ops`
  split into `bodh.locks`, `bodh.logs` and `bodh.heartbeat`. The old names import with a
  `DeprecationWarning` and re-export the new modules. With them the main names: `Judge` (was
  `Teacher`), `JudgeError`, `Judge.review` and `.reviews` (were `second_look`), `SafetyTripped`
  (was `BreakerTripped`), `select_autocut` (was `select_floor`), `catch_up`, `clear_old`,
  `stop_all` (were `blast`, `bankruptcy`, `halt`), `steer`/`steers` (were `nudge`/`nudges`),
  `Console` (was `Panel`), `tag_bands` (was `tag_buckets`).
- The database's kv keys: `run_phase`, `provider.plain_json`, `provider.cache_ttl` and
  `safety_tripped` (were `phase`, `plain_json`, `cache_ttl_fallback`, `breaker_tripped`), moved
  by schema migration 4 on the first open; `breaker_tripped` is kept in step so that a rollback
  to the code before October 2026 still sees a tripped safety limit. Nothing else in the
  database changes: stored words such as `floor`, `bankruptcy`, `blast` and `nudge` stay as they
  are and are shown under the new names.

### Changed

- A run the daily budget cut short cuts nothing and says so ("daily budget reached: nothing
  cut this run"): a half-judged run's close calls could not have their reviews. Before, the
  judged junk of such a run was still cut. The next day's runs cut as usual.
- The CLI's error line replaces any secret value found in a message (an API echoing a header,
  a URL with a token) before printing it, and the model client scrubs its key from every error
  it classifies.
- `bodh weekly` exits with the error's own code (4 for the model) instead of 1 for everything;
  `bodh profile` refuses a word it does not know; `bodh version` takes `-v` like every command;
  `bodh stop --all` no longer waits a second when there is nothing to end.
- The run's phase changes no longer walk the whole database for `progress.json` every time (the
  phase goes to the database at once, the file follows at most once a second), the per-folder
  counts no longer join every article, the dashboard takes one progress snapshot instead of two,
  and bulk writes to the feedback and state tables are one transaction each (25,000 cut articles
  coming back unread took seven seconds; under one now). Migration 5 adds the indexes the queries
  by time walk; it is harmless to older code.
- `canonical_url` strips whitespace from the path, so the result is its own canonical form.
- Dependencies carry bounds: `anthropic>=0.77,<2` (0.77 is the first with structured outputs;
  the 1.x line passes the suite), `httpx<1`, `pydantic<3`, `typer<1`, `pyyaml<7`,
  `python-dotenv<2`.
- The score's caps (promo 30, off-field funding 40, duplicate 20) now come after the bonuses
  (the home angle, the expansion topics), so a bonus never lifts a capped article over the
  cut-off; vendor marketing on an expansion topic used to score 40. Nothing already judged
  is re-scored; the order is documented in `compose` and tested.
- Every error bodh raises on purpose is one line on stderr and an exit code a script can act
  on: 2 configuration, 3 BazQux, 4 the model, 5 a run in progress, 1 anything else.
- `bodh doctor --values` prints every setting in effect with its source (config.yaml or the
  default); `doctor` runs the database's `quick_check` and `integrity_check`.
- The plain-JSON and 5-minute-cache fallbacks are remembered per model, not for every model
  at once (the single flag written before October 2026 still means every model).
- A batch is submitted without the client's automatic retries, so a create that timed out
  after the server accepted it can no longer be retried into a second, paid batch.
- An article resubmitted in a later batch leaves a gap in the earlier batch's numbering, so
  the model's judgments never land on the wrong article.
- A synchronous review that moves an article's band retags it in the same run.
- An article the feed gave no text for is fetched again for six hours, not every run for two
  weeks; `bodh catch-up --chunk` is capped at 5,000.
- The console, the dashboard and the progress section share one HTML layer (`html.py`) with
  the colours as variables and a dark mode.
- `config.yaml` is no longer tracked by git: it is yours, like `.env` and `profile/`, and
  `config.example.yaml` is the template. An install that loses its `config.yaml` while it has a
  database is refused by `install.sh` until the file is restored, so a fresh copy of the template
  never silently changes folders, cut-offs or timezone.
- The two scoring rules that were one person's are configuration. `judge.home_angle` (a short
  phrase; empty turns it off) and `judge.home_bonus` (10) replace the fixed India angle; the
  `funding` flag now means a funding round outside the student's own field, as `about.md` describes
  it. Old flag names in the database are read as the new ones.
- `timezone` defaults to the machine's own zone (`$TZ`, else `/etc/localtime`, else UTC); it is
  no longer `Asia/Kolkata`. An empty `timezone:` means the same; `bodh doctor` says which zone is
  in effect and whether it came from `config.yaml` or the computer.
- With no `BODH_HOME` and no `config.yaml` above the working directory, `~/bodh` is tried last,
  so a checkout at the default place works from any folder.
- `autocut.enabled` defaults to false: a new install judges and tags but cuts nothing until the
  owner turns autocut on. Every command but `version` refuses to run without a `config.yaml`
  (exit code 2).
- `config.yaml` is validated strictly: a misspelt key, a value out of range, an unknown timezone
  or two tags with one name are refused with the key named; a file that sets both an old key and
  its new name is refused rather than guessed at.
- The templates speak to a stranger: `profile.example/about.md` is a worked example for an
  invented student (the shape explained in a comment at the top), `config.example.yaml` has a
  comment on every key and generic folder names, `.env.example` says where each value comes from.

### Added

- `bodh backup`: a copy of the database with `VACUUM INTO` under `data/backups/`, seven kept;
  the run makes one a day on its own; `bodh doctor` reports how many and the newest.
- `bodh rebuild [--dry-run]`: after a lost database, the band tags, the cut tag and the stars
  come back from BazQux. A rebuilt score carries the band's threshold with `model = "rebuilt"`
  and a low confidence (autocut leaves it alone); the cut tag on a read article becomes a cut
  already written; the unread rebuilt keepers at or above `catch_up.rejudge_at` are read again
  by the judge on the next runs. Nothing BazQux shows is changed.
- `bodh eval`: the evaluation set (`evals/set.yaml`, sixty short articles written for the
  purpose, each with the band the example student should see it in), judged with the
  configured judge under a dollar cap; prints the agreement by band and the largest
  disagreements; `--write` keeps the result as JSON. CI runs it weekly with the owner's key.
- The test suite is the contributor's safety net now: one fake reader, one fake BazQux
  server behind a mock transport, one fake model SDK under the real client wrapper
  (`tests/fakes.py`); contract suites for readers and providers (`tests/contracts/`); the
  failure drills (`tests/test_drills.py`); the parser, `compose`, the ids and `canonical_url`
  under hypothesis; the rendered prompts as a snapshot; every command through the runner with
  a scan for secrets in the output; the 50,000-article timings (`scripts/make_fixture_db.py`,
  `BODH_PERF=1`); the budget's midnight across a DST change and a half-hour zone. GitHub
  Actions run the lot on Linux, macOS and Windows with Python 3.10 and 3.13, one leg on the
  oldest supported SDK, pip-audit and an install smoke test; a weekly workflow runs the
  newest SDKs and the evals. `CONTRIBUTING.md` says how to add a test, a reader or a provider.
- `bodh safety [status|reset]`: the safety limit, which now persists between runs (autocut and the
  tag-requested prune wait, `bodh prune` refuses, until a person resets it), and the articles
  the judge gave up on after three empty answers.
- `bodh prune --yes` for scripts and the console; the interactive prune shows the cut,
  asks, and cuts only what it showed.
- `bodh mark ID rescue` and `bodh profile [show|revert]` from the terminal.
- Database migrations (`kv.schema_version`), so a database from an earlier version opens and
  runs under the new code.
- Rescued articles (`rescue`, or marked unread again after a cut) are never cut again by any path;
  an undone cut is exempt from autocut.
- A log redactor: no secret or auth token reaches the log, a traceback or the dashboard.
- `bodh doctor` exits 1 when a check fails, and reports an `about.md` that is still the worked
  example; `bodh run` and `bodh catch-up` refuse such a profile before touching the network (exit
  code 2).
- `contrib/replay_archive.py`: judge a sample of another filter's archive offline before anything
  goes live (the `replay` command it replaces needed one particular archive).
- `docs/glossary.md`: one word per thing, with the old names in brackets.
- `CHANGELOG.md` (this file), `LICENSE` (MIT); `pyproject.toml` carries the author, the licence, the
  repository URLs and the classifiers, and reads the version from the package.

### Removed

- `laptop-install.sh`; its steps are the README's install section.
- The `replay` command (see `contrib/replay_archive.py`).
- The `teacher.v1` prompt: it carried one person's rules and the current parser could not read
  its answers; `judge.prompt_version` is `teacher.v2`.
- `install.sh` no longer reads a sibling project's `.env`.
