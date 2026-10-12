# Changelog

Every change a user could notice, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/) from 1.0.0 on.

## [Unreleased]

### Added

- **The service for the Mac app.** `bodh serve --app --port 0 --token-file PATH --home DIR` is the
  engine the app runs: a free port on 127.0.0.1, a per-launch token every request must carry
  (`Authorization: Bearer`, or `?token=` for an event source), the port as one JSON line on stdout
  and in `data/serve.json`, a clean exit on SIGTERM and when stdin closes. The console's server
  gained the routes the app needs, all in `docs/json.md`: `/api/health`, `/api/list`, `/api/why`,
  `/api/find`, `/api/tags`, the settings (`GET`/`PUT /api/settings`, `GET /api/settings/schema`),
  `/api/folders`, the keys (`GET /api/keys`, `PUT`/`DELETE /api/keys/{provider}`,
  `POST /api/keys/import-env`), the profile (`GET`/`PUT /api/profile`, `/api/profile/diff`), the
  live checks of onboarding (`POST /api/check/reader`, `POST /api/check/provider`), the attention
  states (`GET /api/attention`, `POST /api/attention/dismiss`) and the levers (`/api/run`,
  `/api/stop`, `/api/pause`, `/api/steer`, `/api/prune`, `/api/undo`, `/api/clear_old`,
  `/api/catch_up`, `/api/safety/reset`). `/api/events` carries named events (`run`, `attention`,
  `settings`) beside the ticks, replayed from `Last-Event-ID` on a reconnect. The page serves one
  section at a time (`/?section=list`) for the app's window. `bodh status --json` gained
  `attention` (every banner, with a fingerprint), `paused_until` and `serve`.
- **Settings from one source.** Every field of the configuration models carries its help and its
  place in the Settings window; `bodh settings` lists every value with its source, `bodh settings
  set KEY VALUE` changes one (validated by the models; the file's comments and order kept, old key
  names moved to their new places on the way), `bodh settings schema --json` prints what the app
  draws, and `CONFIG.md` is generated from the same source (`scripts/gen_config_md.py`; the suite
  fails when it is stale). The writer uses `ruamel.yaml`, a new dependency.
- **A pause.** `POST /api/pause` (and the app's menu) holds `bodh serve`'s schedule for an hour,
  until tomorrow or until resumed; `bodh status` shows `paused_until`.
- **The attention states** are a list now, not one banner: the safety limit, the reader's
  sign-in, the provider's refusal (new: a run that ended with `ModelConfigError`), a failed run,
  the budget, the Sunday rewrite; each can be dismissed and comes back when its condition does.
- `bodh migrate-home FROM TO`: a project moved whole to another folder (config.yaml, .env, data,
  profile, dashboard, prompts), nothing rewritten, idempotent; the app's first launch uses it.
- The console's child runs end cleanly when `bodh serve` is stopped mid-run: the child gets
  Ctrl-C and twenty seconds, then SIGTERM (it used to be killed outright).

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

- The status and progress JSON use the glossary's names: `console` (was `panel`), `keep` and
  `cutoff` under `config` (were `target` and `floor`), `judge` (was `teacher`), and
  `folders[].cutoff` (was `floor`); `dashboard/progress.json` carries `schema: 1`.

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
- `compose` sums the weighted sub-scores exactly (`math.fsum`), so the same verdict gives the
  same score on every Python: 3.12 changed `sum()` for floats, and a sum landing on a half
  (80.45) rounded to 80.4 on 3.10 and 80.5 on 3.13. Nothing stored is recomputed.
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

- The dashboard is one page, in three forms: the console's `/` with the levers (Run now, Stop,
  Pause, the other actions folded away, a verdict menu on every row with a key per grade, a
  confirmation with the numbers before anything that writes to BazQux), the static export
  `dashboard/index.html` (the same page without the levers, the document embedded, "Exported
  16:56; open the console for live data"), and `/?mode=app` for a window that supplies its own
  chrome. Six sections answer the reader's questions in order: what happened today, what to
  read, is it judging well for you, what it is learning, your feeds, what it costs and how it
  runs. Every number comes from one JSON document, `GET /api/dashboard` (`docs/json.md`), that
  the export writes beside itself as `data.json`. Live: `/api/events` streams ticks (the state
  line, the meter and today's tiles move every two seconds while a run is in progress; the
  page refetches itself and swaps what changed, node by node, when a run ends, an action runs,
  a verdict is recorded or `config.yaml` changes), with polling every 30 s as the fallback;
  the "updated 12 s ago" stamp turns to the warning colour after two missed heartbeats. The
  page works without JavaScript (the script adds the live layer, the filters, the tooltips and
  the verdict menus), honours Reduce Motion, light and dark, 390 px, and makes no request to any
  host but the console's own. The design system (`bodh/web/tokens.py`, validated by numbers;
  `docs/design.md`; the living style guide at `/design`) is the only place a colour, a size or a
  distance is defined.
- The commands in seven groups named by when they are used (set up, every run, look, cut,
  teach, control, housekeeping): `bodh --help` lists them in that order as plain text, every
  command's help ends with examples, shell completion is on (`bodh --install-completion`),
  and `--json` prints one object with `schema` first on `version`, `keys`, `run`, `status`,
  `progress`, `why`, `find`, `list` and `tags` (`docs/json.md`). Behind every command is
  `bodh.api.App`, the one implementation the console and scripts call too; the command line
  parses and prints.
- `bodh init [DIR]`: a new project in one sitting. The directory, the BazQux login and a
  provider's key (each checked live unless `--no-live`), five questions that become
  `profile/about.md`, the timezone; then `doctor` runs and the schedule is offered. Every
  answer has a flag, so a script can do it; the example files ship inside the package.
- `bodh serve [--every 30]`: the schedule and the console in one process (a run every N
  minutes, the weekly on Sunday mornings, the report on the first of the month, each a child
  process), for Docker, Windows and any machine without launchd or systemd.
- `bodh schedule install|uninstall [--dry-run]`: launchd agents on a Mac, systemd user units
  on Linux, one Task Scheduler entry running `bodh serve` on Windows; every entry runs the
  interpreter bodh was installed with, so pipx, uv, a venv and a checkout schedule alike.
- `bodh find "words" [--unread]` (titles and lessons: id, score, band, state, feed),
  `bodh list [--band --folder]` (today's reading list in text), `bodh tags` (the vocabulary
  and what each tag does), `bodh undo --run ID`, `bodh profile edit` (about.md in `$EDITOR`),
  `bodh upgrade [--run]` (the upgrade for the channel detected: pipx, uv, Homebrew, a
  one-file executable, a checkout, pip) and `bodh version --check` (the newest on PyPI).
  `bodh why` and `bodh mark` take the article's URL as well as its id.
- `bodh prune --dry-run` prints the boundary, the counts by band and by folder, and the ten
  best that would go with their lessons; the questions a prune and a clear-old ask state the
  number, the mode, that `bodh undo` reverses it, and the run id it will create.
- A folder with no project says so and names `bodh init`; a data folder is never created
  where there is no config.yaml.
- Models from five providers. Every role (`models.judge`, `models.reviewer`, `models.profiler`,
  `catch_up.model`) takes `provider/model-id`: `anthropic` (a bare id still means Anthropic, so
  an old `config.yaml` reads the same), `openai` (the Responses API, the Batch API, structured
  outputs), `gemini` (`generateContent` with a response schema, the batch API), `openrouter`
  (Chat Completions, no batch), and `openai-compatible` for Ollama, llama.cpp, vLLM or LM Studio
  (the base URL selects it; with every role local nothing leaves the machine). Each provider
  is one file under `src/bodh/providers/` behind one interface; the contract suite runs the
  same twelve behaviours over all five. The database stores and `models.prices_per_mtok` is
  keyed by the same name (`claude-sonnet-5-5`, `openai/gpt-5-mini`). `docs/models.md`.
- `bodh keys [list|set <provider>]`: which keys are set, never a value; `set` reads the key
  with hidden input and writes `.env` owner-only, or the system keychain with `--keychain`
  (`pip install bodh[keychain]`). Keys are looked up in `.env`, then the environment, then the
  keychain. `bodh doctor` names the missing key per provider and the roles that need it;
  `doctor --live` probes the profiler and the quick judge too when they are models of their own.
- `bodh models [list|compare --with provider/model]`: the roles, the keys present, the prices,
  the capabilities; `compare` judges a sample of already-judged unread articles with the judge
  and a second model and prints the band agreement, the mean difference, the largest
  disagreements with both lessons, the cost per 1,000 and the seconds per article. Nothing
  stored changes but the costs (purpose `compare`).
- `bodh run --rejudge` and `bodh catch-up --rejudge`: the judge reads again everything another
  model judged, whatever it rated (the standing rule re-judges only what was rated
  `catch_up.rejudge_at` or more). `catch-up --rejudge` is the judge's pass, not the quick judge's.
- The dashboard's calibration section gains a table by the model that judged, when two models
  have each judged twenty or more articles in the last thirty days: how many, the mean score,
  how often their articles were read or starred, the grades for and against.
- A per-run circuit breaker: five failed model calls in a row stop the judging for that run
  (the provider is down, or the prompt is broken for that model); the rest stays pending, the
  run is still ok and its summary says why; a catch-up stops after that round instead of
  spinning on a dead provider.
- `bodh progress` says when a model's spend is an estimate (its service reports no token
  counts; four characters a token).
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

- The three old HTML renderers: the console's inline page with its polling script, the old
  `dashboard.py` page and CSS, and the progress section of the old export with its script
  (`html.py` with them). `dashboard/progress.json` is still written by every run and by
  `bodh dashboard`; `data.json` now holds the dashboard document instead of five fields.
- `laptop-install.sh`; its steps are the README's install section.
- The `replay` command (see `contrib/replay_archive.py`).
- The `teacher.v1` prompt: it carried one person's rules and the current parser could not read
  its answers; `judge.prompt_version` is `teacher.v2`.
- `install.sh` no longer reads a sibling project's `.env`.
