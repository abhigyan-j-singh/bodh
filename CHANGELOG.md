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
