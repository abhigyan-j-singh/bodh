# bodh

[![ci](https://github.com/abhigyan-j-singh/bodh/actions/workflows/ci.yml/badge.svg)](https://github.com/abhigyan-j-singh/bodh/actions/workflows/ci.yml)
[![weekly](https://github.com/abhigyan-j-singh/bodh/actions/workflows/weekly.yml/badge.svg)](https://github.com/abhigyan-j-singh/bodh/actions/workflows/weekly.yml)
![python](https://img.shields.io/badge/python-3.10%E2%80%933.13-blue)
![licence](https://img.shields.io/badge/licence-MIT-green)

Bodh (बोध: understanding, insight) keeps your BazQux unread list worth reading. A judge (Claude
Sonnet 5.5 by default; any model from Anthropic, OpenAI, Google, OpenRouter or a local Ollama,
see `docs/models.md`) reads every unread article once as teaching material for you and writes
its judgment: writing quality, what you learn, level fit, breadth, relevance. The worst are marked read in BazQux
itself; you choose how deep the cut goes and can run it again. Any reader that syncs with BazQux
(NetNewsWire, Reeder, the BazQux apps) simply follows. Every machine action is a tag, every cut can
be undone, and nothing but mark-as-read ever changes in your reader.

This file is the overview and the install; `RUNBOOK.md` is the operator's sheet; `docs/glossary.md`
has one word per thing. The project was called `triage` until October 2026, and several things were
renamed with it (the floor is the cut-off, the panel is the console, the learner model is the learned
profile); the old commands, keys and tags keep working for one release, and `CHANGELOG.md` lists every
old name against its new one.

## What is in this repository

| Path | What | Committed? |
| --- | --- | --- |
| `RUNBOOK.md` | The operator's sheet: what runs by itself, the daily routine, every command, every knob, what to do when something looks wrong | yes |
| `docs/glossary.md` | One word per thing: the judge, the review, the cut-off, autocut, the prune, the console, with the old names in brackets | yes |
| `CHANGELOG.md`, `LICENSE` | What changed, by release; the MIT licence | yes |
| `PLAN.md` | The complete prompt: the three plans combined in the order they run, with the gates between them | yes, until the release |
| `OPEN_SOURCE_PLAN.md` | The plan for the public release: the audit's findings, the test plan, ten phases with acceptance criteria; exported from its Claude Doc | yes, until the release |
| `MAC_APP_PLAN.md` | The plan for the Mac app: a native menu bar shell around the bundled engine, in four stages with acceptance criteria; exported from its Claude Doc | yes, until the app ships |
| `DASHBOARD_PLAN.md` | The specification of the dashboard: the five questions, the design system, every panel, the chart rules, the data contract, the quality bar; replaces Phase 7 of the open-source plan | yes, until the release |
| `src/bodh/` | The package: the BazQux client, the store, the judge, the cut, the feedback, the profiler, the dashboard, the progress page, the console | yes |
| `src/bodh/prompts/` | The judge's, the reviewer's and the profiler's prompts (`./prompts/` next to `config.yaml` overrides them) | yes |
| `tests/` | the test suite, with fakes for BazQux and the models; `pytest -q` runs in seconds | yes |
| `launchd/`, `install.sh`, `install-launchd.sh` | Install and schedule on a Mac | yes |
| `config.example.yaml`, `.env.example`, `profile.example/` | Templates; `./install.sh` copies them to `config.yaml`, `.env`, `profile/` | yes |
| `contrib/` | Scripts outside the package: `replay_archive.py` judges another filter's archive offline | yes |
| `shortcuts/` | The Prune button: an Apple Shortcut over SSH | yes |
| `config.yaml` | Your folders, cut-offs, models, budget | never (yours; `config.example.yaml` is the template) |
| `.env` | BazQux password, Anthropic key, heartbeat URL | never |
| `profile/` | Your student profile: `about.md` (yours) and `learned.md` (the profiler's), with their own git history | never |
| `data/`, `dashboard/` | The SQLite cache, logs, the rendered dashboard | never |

Nothing in the code is tied to one account: clone it, run `./install.sh`, fill in `.env`, replace the
worked example in `profile/about.md` with yourself, and name your own folders in `config.yaml`.

## What runs

| Command | What it does |
| --- | --- |
| `bodh run` | The timer's job, every 30 minutes: collect feedback (stars, reads, rescues, verdicts), fetch new articles, collect the judgments the Batches API finished, submit the new articles, tag judged ones `score-90` … `score-0`, apply autocut (with an Opus 5.5 review of close calls that have more to read), regenerate the dashboard, ping the heartbeat. Cuts nothing while `autocut.enabled: false`. `--sync` waits for every model call instead of batching; `--shadow` is one run without cutting even with autocut on. |
| `bodh catch-up` | One deliberate pass over everything pending, as fast as the API allows: synchronous, 8 calls at once (`--workers`), rounds of 1,000 until nothing is pending, with the quick judge (`catch_up.model`, Haiku: 20x cheaper; it never touches the judge's judgments, and the judge reads again what it rates 50 or more on the next runs, `catch_up.rejudge_at`). `--model` names another model, the judge itself for a full-price pass. `--all-ages` includes the articles older than `limits.max_age_days`; `--budget 40` raises today's cap; `--max-seconds 600` stops starting calls after ten minutes. Judges and tags, cuts nothing; Ctrl-C and run again to continue. |
| `bodh progress [--watch]` | How far the judge has got: judged, with the model, pending, by folder; while a run is going, its phase, the rate per minute and the ETA; batches; spend and cache hits; the log tail. `--watch` redraws every 5 s. |
| `bodh prune` | Your cut: keep the 40 best (`--keep N`), or cut a share (`--fraction 0.3`), or everything under a score (`--below 65`). It shows the boundary and what would go, then asks (`--yes` skips the question; `--dry-run` only shows). Run it again to cut deeper. |
| `bodh undo` | Reverse the last cut. What comes back is left alone by autocut from then on. |
| `bodh safety [reset]` | The safety limit (a run whose autocut would cut nearly everything newly judged trips it, and autocut stays off until a person resets it) and the articles the judge gave up on. |
| `bodh clear-old --days 14` | Day 0: mark everything older than two weeks read, stars and protected articles exempt. Reversible. |
| `bodh status`, `bodh why ID` | The whole system at a glance: what is running, the launchd jobs, the console, the unread list, spend, the last runs; why one article got its score. |
| `bodh console` | The console in the browser (`http://127.0.0.1:8766`, kept alive by launchd): the same status live, buttons for run, stop, pause/resume the schedule, autocut on/off, prune (dry run first), undo, clear-old, catch-up, weekly, report, doctor; command output and the log streamed; the dashboard underneath. |
| `bodh stop [--all]` | Stop the run, prune or catch-up in progress, the same as Ctrl-C in its terminal. Fail open: what was judged stays; a cut half-written to BazQux is finished by the next run. `--all` stops everything: pauses all four launchd jobs, interrupts the run in progress, ends every other bodh process including the console; `bodh schedule resume` brings the jobs back. |
| `bodh schedule [status\|pause\|resume\|kick]` | The launchd jobs: show them, pause them all (nothing fires until resume, the console included; `--job run` for one), resume, or kick a run now (refused while a run holds the lock). |
| `bodh autocut on\|off` | Flip autocut in config.yaml: on cuts under the cut-off from the next run, off is scoring-only mode. |
| `bodh mark ID great\|good\|meh\|bad\|basic\|heavy\|more\|less\|rescue` | Your verdict on an article from the terminal, the same as the tags in BazQux; `rescue` brings a cut article back. |
| `bodh steer more\|less "x"` | Steer the judge now instead of waiting for Sunday: a dated line in the learned profile (the `more` and `less` tags do the same from BazQux). |
| `bodh profile [show\|revert]` | The learned profile and its versions; `revert` restores the previous one. |
| `bodh weekly` | Sunday: the profiler rewrites the learned profile from the week's reading (kept only if the eval gate passes), writes the weekly note, runs the drift check. |
| `bodh report` | Monthly feed report with proposals (unsubscribe, move to the strictest folder, protect). |
| `bodh doctor [--live] [--values]` | Configuration and connectivity checks, the database's integrity and backups; exit code 1 when one fails. Run after every update. `--values` prints every setting in effect with its source (config.yaml or the default). |
| `bodh backup` | Copy the database to `data/backups/bodh-<date>.db` (`VACUUM INTO`); seven kept. The run does this once a day on its own. |
| `bodh rebuild [--dry-run]` | After a lost database: read the band tags, the cut tag and the stars back from BazQux, so nothing already judged is paid for again and nothing already cut is cut again. Rebuilt scores carry the band's threshold and a low confidence (autocut leaves them alone); the unread ones rated 50 or more are read again by the judge on the next runs. |
| `bodh eval [--model] [--cap 0.50]` | Judge the sixty-article evaluation set (`evals/`) for the example student and print the agreement by band and the cost. A few cents; the figure below comes from it. |
| `bodh keys [list\|set <provider>]` | Which provider keys are set (never a value); `set` asks for one with hidden input and writes `.env` owner-only (`--keychain` for the system keychain). |
| `bodh models [list\|compare --with provider/model]` | The four roles with their models, prices and capabilities, and the providers with keys; `compare` judges a sample of already-judged unread articles with the judge and a second model and prints the band agreement, the largest disagreements, the cost per 1,000 and the seconds per article for both. Nothing stored changes. |
| `bodh run --rejudge`, `bodh catch-up --rejudge` | After a switch of `models.judge`: the judge reads again everything the previous judge rated, whatever the score (by default only what it rated 50 or more is re-judged, `catch_up.rejudge_at`). |

## Install

Requirements: macOS, Python 3.10+ (`brew install python`), git. Tailscale only for the dashboard and the Shortcut.
Keep the checkout somewhere plain such as `~/bodh`: launchd cannot run jobs that live in `~/Documents`,
`~/Desktop` or `~/Downloads` (macOS privacy protection denies them with "Operation not permitted"), and a
path with spaces makes pip wrap the command in `/bin/sh`, which is denied first. The installer refuses both.

```bash
git clone https://github.com/abhigyan-j-singh/bodh ~/bodh && cd ~/bodh
./install.sh                      # venv, package; config.yaml, .env and profile/ from the templates; doctor
$EDITOR .env                      # BazQux email and password, Anthropic key (the file says where each comes from;
                                  # `bodh keys set <provider>` writes a key without showing it)
$EDITOR profile/about.md          # replace the worked example with fifteen lines about you
$EDITOR config.yaml               # your folders, what the prune keeps, the cut-off; leave autocut off for the first days
.venv/bin/bodh doctor --live      # logs in, lists folders, calls both models once (a few cents)
./install-launchd.sh              # laptop: agents, run while logged in, no sudo
./install-launchd.sh --daemon     # an always-on Mac: daemons, run without login (asks for sudo)
```

`doctor` exits 1 until `.env` and `about.md` are yours; `install.sh` runs it last and says so.

In BazQux, before relying on it:

- **The `Must read` stream.** A smart stream is a saved search shown as a feed; bodh reads it as a
  label, and its unread members are never cut. Create it once: with **Latest** selected in the sidebar,
  search for the terms that mark an article you must see, for example
  `subject:("my company" OR "my town" OR "the grant programme")`, and in the results choose
  **New smart stream**; name it `Must read` (or name yours in `protect.must_read_stream`). Keep it
  narrow: it is a guarantee, not a reading list. Edit it later under Settings → Filters & streams.
  `bodh doctor --live` says whether it exists; without it, a run judges and tags but cuts nothing.
- **Folders.** Put the feeds you always read in a folder named **Always read**, the comics in
  **Comics**, the general-news firehoses in **News**; `folders` in `config.yaml` names them
  (never cut; never judged; a stricter cut-off). Folder names are matched exactly, and moving a feed
  between folders in BazQux changes how it is treated.

## Day 0 to steady state

1. **Clear-old or catch-up.** `bodh clear-old --days 14` clears the old unread (`bodh undo` if you regret it); or `bodh catch-up --all-ages` judges every unread article once, now, for a few dollars, so the list can be pruned on merit (the judge then re-judges the promising ones). Otherwise the backlog is judged 1,200 articles a run through the batch API, newest first.
2. **Coming from another filter?** `contrib/replay_archive.py` judges a sample of its archive
   offline, plus everything you starred, and checks that no starred article falls below the cut-off;
   its header says what archive it expects. About a dollar for 500 articles; it touches nothing live.
3. **Scoring-only days.** With the launchd jobs installed and `autocut.enabled: false`: for two or three
   days, look at `bodh prune --dry-run` and the dashboard's near misses. Articles get judged and tagged;
   nothing is cut.
4. **Autocut.** `bodh autocut on`, with `autocut.cutoff: 25`. Autocut removes clear junk; you prune to
   40 with the Shortcut or `bodh prune` whenever the list is long.
5. **Raise the bar.** After a week, set the cut-off to the boundary you have been choosing (the dashboard
   shows it), give the firehose folders their own cut-off in `folders`, act on the first feed report. From
   then on: weekly note on Sundays, feed report on the 1st, `about.md` reviewed when your priorities change.

## Cost

Two readers, priced to run all day. The first pass goes through the **Message Batches API** (half
price): a run submits what arrived, the next run, thirty minutes later, records the judgments and tags
them. The judge writes a compact judgment (five numbers, a twelve-word lesson, one-word codes), which
halves its output tokens, the dear part. Its prompt prefix is cached for an hour, so runs share it. The
**review** (Opus, full text) is only taken when the article has more text than the opening the first
pass saw, at most once per article, and travels through the same batch: a close call waits one run
before it can be cut. Unread articles older than `limits.max_age_days` (14) are never judged;
`bodh clear-old` is for them. Folders with `judge: false` are never sent to the judge.

At about 1,300 arrivals a day that comes to roughly $0.65 per 1,000 judged, under a dollar a day,
against $1.90 per 1,000 before. `bodh progress` shows the actual rate and the cache hit share;
`models.daily_budget_usd` still caps a bad day, across every provider. Another provider's model, or
a local one that costs nothing per token, goes in `config.yaml` as `provider/model-id` with its price
in `models.prices_per_mtok`; `bodh models compare` shows what it would judge differently before you
switch (`docs/models.md`). Trade-offs: a judgment lands 30 to 60 minutes after an
article arrives instead of at once (the Batches API finishes most batches within the hour), and a
close call survives one extra run. `bodh run --sync` restores the immediate path when you want it
(`doctor --live` always runs synchronously; `limits.parallel_calls` model calls run at once).

## launchd

`install-launchd.sh` renders the four templates in `launchd/` (the run, the Sunday rewrite, the monthly
report, the console) with this checkout's path and installs them:
as LaunchAgents (`~/Library/LaunchAgents`, no sudo, run while you are logged in; the laptop) or, with
`--daemon`, as LaunchDaemons (`/Library/LaunchDaemons`, run without login; an always-on Mac). Run it
again after moving the folder.

```bash
launchctl print gui/$(id -u)/bodh.run      # agent status (daemon: sudo launchctl print system/…)
launchctl kickstart -k gui/$(id -u)/bodh.run   # run once now
launchctl bootout gui/$(id -u)/bodh.run    # stop
tail -f ~/Library/Logs/bodh/bodh.log
```

The job takes a lock, so an overrun never overlaps the next start, and it kills itself after 15 minutes;
the next run picks up what is left. A laptop that sleeps simply misses runs and catches up on wake; a
backlog drains at `limits.per_run_batch` (1,200) articles per run, or `limits.per_run_sync` (400)
with `--sync`, where `limits.parallel_calls` calls run in parallel. For an always-on Mac, also keep it
awake and self-healing:

```bash
sudo pmset -a sleep 0 disksleep 0 displaysleep 10 autorestart 1
```

## The console

`./install-launchd.sh` also installs `bodh.console`, a small local web server kept alive
by launchd. Open `http://127.0.0.1:8766` on the Mac. For the iPhone, publish it on your tailnet once:

```bash
tailscale serve --bg 8766         # then https://<your-mac>.<tailnet>.ts.net
```

The page shows the state of the system (idle or what is running and its phase, the mode, the next
scheduled run), the launchd jobs, the unread list, spend, today's counts, the last runs, the log, and the
dashboard underneath, refreshed every three seconds. The buttons start the same commands you would
type, as child processes whose output streams into the page; the ones that change BazQux ask first.
Stop interrupts whatever holds the run lock, started from the page or not. The server listens on
127.0.0.1 only, answers only to a Host of 127.0.0.1, localhost or a `*.ts.net` name (a page that
rebinds a DNS name to this machine gets nothing), and accepts actions only from its own page;
Tailscale adds the identity check on the tailnet. From the terminal the same levers are `bodh
status`, `stop`, `schedule`, `autocut`, `safety`.

The installer waits up to 15 s for the page to answer and, if it does not, prints the job's state and
the last lines of `~/Library/Logs/bodh/console.err.log` and `data/console.log`, where the server writes
why it could not start (a port in use, say). Later, `bodh status` says the same; `bodh console` in a
terminal runs it in the foreground with the error on screen.

## Heartbeat and alerts

Create a check on [healthchecks.io](https://healthchecks.io) with period 30 minutes and grace 30
minutes; put its ping URL in `.env` as `HEALTHCHECK_URL`. Every good run pings it with the run
summary; a failed run pings `/fail` with the error; an hour of silence (machine off, network down,
launchd not firing) raises an alert. Choose email there, or add its ntfy or Pushover integration for
a phone push. The weekly note is written to the dashboard folder and, if `obsidian_note` is set in
`config.yaml`, into your vault.

## Dashboard

`bodh run` regenerates `dashboard/index.html` after every run. Publish it on your tailnet:

```bash
tailscale serve --bg --set-path / "$(pwd)/dashboard"
tailscale serve status            # shows the https URL, open it on the Mac or the iPhone
```

The progress section is live: the page polls `progress.json` every 4 seconds, which every run (and
`bodh catch-up`) rewrites as it goes, so the bars, the rate, the ETA, the phase ("judging 400 of 1,200"),
the folder table and the batches move while you watch; the dot pulses while a run holds the
lock. The log stays out of this page and of `progress.json` (both may be published); the console
and `bodh progress` show it. The polling needs the page served over http (`tailscale serve`, or `python3 -m http.server 8765`
in the `dashboard` folder); opened as a file it shows the numbers as of the last render.

Sections: progress (judged, with the model, pending, by folder; batches; cost per 1,000), flow, calibration
(read rate per score band; the check on the judge), near misses (the best of what was cut, rescue by
marking unread), feeds (volume, mean score, cut, read, star, rescue rates), learnings (the week's
lessons by concept), map (concepts read in 90 days and the frontier), the learned profile (current text and
its diff), health (runs, spend, heartbeat). The same progress numbers in the terminal: `bodh progress`.

## Tags in BazQux

| Tag | Who writes it | Meaning |
| --- | --- | --- |
| `score-90`, `score-70`, `score-50`, `score-30`, `score-0` | bodh | The article's score band. Open a tag with the unread filter on and you have a ranked view. |
| `cut` | bodh | Marked read by bodh (autocut, prune or clear-old). Mark it unread anywhere to rescue it. |
| `rescue` | you | A command, on a cut article you wanted: it is marked unread again, counts as a rescue and as the strongest positive, and is never cut again by any path. The run removes the tag when done (`wrong`, the old name, still works). |
| `prune` | you | A command, on any article: the next run with autocut on prunes to what the prune keeps. Removed when done. |
| `great`, `good`, `meh`, `bad` | you | Your grade, one per article: the best kind (more like this; the Sunday rewrite may not lose it), worth the time, not worth it, junk the judge should not have kept. |
| `basic`, `heavy` | you | The level missed: too elementary for you, too dense or specialist for you now. |
| `more`, `less` | you | A steer: more or less of this article's concept, written into the learned profile as a dated line that the judge reads from the next run. |

Stars are yours: a starred article is never cut and counts like `great`. Verdict tags stay on the
article as your own record (open the `great` tag in BazQux and you have your best-of list) and are
recorded once; add a different grade to change your mind (when two are on, the harsher wins), and
removing a tag alone retracts nothing. `bodh why ID` shows what is recorded for an article; the
dashboard's "Your verdicts" section sets your grades against the judge's bands and lists where you
disagree most. How all of this changes the judging is in RUNBOOK.md, "How it learns".

## Updating

```bash
cd ~/bodh && git pull && ./install.sh
```

`install.sh` reinstalls the package and runs `bodh doctor`; after a change to the launchd templates,
`./install-launchd.sh` too. The running launchd jobs pick up the new code on their next start. Prompts live in `src/bodh/prompts/`; a copy in `./prompts/` next to
`config.yaml` overrides the packaged one. Changing `judge.prompt_version` re-judges unread articles.

## When something breaks

| Symptom | Where to look | What happens meanwhile |
| --- | --- | --- |
| Heartbeat alert, no error | Is the Mac on and online? `launchctl print gui/$(id -u)/bodh.run` (daemon: `sudo launchctl print system/bodh.run`) | Nothing is cut; unread grows |
| launchd job: `last exit code = 126`, "Operation not permitted" in the logs | The checkout sits in Documents, Desktop or Downloads, or its path has spaces; move it (see Install) and reinstall | Nothing runs on the schedule; manual commands work |
| "BazQux login failed (YearSubscriptionExpired)" | Renew BazQux | Run aborts before any write |
| "daily model budget … reached" | `models.daily_budget_usd` in config.yaml | Judging pauses until midnight; nothing is cut |
| "with the model" stays high for hours | `bodh progress`: a batch still in progress; the API finishes most within the hour, all within 24 | Articles stay unread until judged; a batch not seen to end in 26 h is given up on and resubmitted |
| "requests failed: … output_config" | The judge's model rejected structured outputs; bodh switches itself to plain JSON (`plain_json` in the kv table) | The failed requests are resubmitted next run |
| "autocut would cut … stopping before any write" | The prompt or model misbehaved; `bodh why` on a few new articles, then `bodh safety reset` | Nothing is cut, this run or the next, until the reset |
| "came back with no judgment" in a batch note | The model answered without a judgment, three times for the same articles and they are left alone; `bodh safety` counts them | They stay unread, unjudged, until `bodh safety reset` |
| "gone upstream at once; not believed" | BazQux answered with an empty or truncated unread list, or you marked everything read | Nothing recorded this run; the next run within three hours decides |
| Rescues above 5% in a week | The cut-off is too high for your taste; it drops to 25 on its own and alerts | Manual prune unaffected |
| "daily budget reached: nothing cut this run" | The budget stopped the judging part way; a half-judged run cuts nothing (its close calls could not have their reviews) | Tomorrow's runs cut as usual; `models.daily_budget_usd` raises the cap |
| Database lost | Restore `data/bodh.db` from `data/backups/` (the run keeps seven daily copies); without one, `bodh rebuild` reads the band tags, the cut tag and the stars back from BazQux | Nothing already cut is cut again; the unread keepers are judged again, a few cents |

Logs: `~/Library/Logs/bodh/bodh.log`. State: `data/bodh.db` (an audit cache; BazQux is the
record). The learned profile's history: `git log` in `profile/`.

## Development

```bash
make check                 # ruff, ruff format --check, mypy --strict, the suite; CI runs the same
make coverage              # the suite with coverage, and the gate CI applies: 85% on the core modules
make perf                  # the 50,000-article timings (BODH_PERF=1; CI runs them on Linux)
make audit                 # pip-audit over the installed dependencies
pre-commit install         # the same checks before every commit (pip install pre-commit first)
```

The suite runs offline against the fakes in `tests/fakes.py`: a reader in memory, BazQux's HTTP
API behind a mock transport (login, the write token, ids by page, contents, edit-tag, and the
failures seen live), the model SDK scripted by title (a failure on the nth call, structured
outputs or the 1-hour cache rejected once, batches that expire or vanish), and the real client
wrapper over it. `tests/contracts/` holds the suites a new reader or model provider must pass;
`tests/test_drills.py` the failure drills (BazQux down for a run, the model out mid-run, a crash
between the tag and the read mark, a laptop asleep two days, the budget reached mid-run, two runs
at once, a corrupt `progress.json`, a typo in `config.yaml`, a model id the provider does not
know); `tests/snapshots/` the rendered prompts, so a prompt change shows as a diff and
`BODH_UPDATE_SNAPSHOTS=1` accepts it once `CHANGELOG.md` says whether it re-judges. The parser,
`compose`, the ids and `canonical_url` run under hypothesis; `scripts/make_fixture_db.py` builds
a year of invented use for the timings. CI (`.github/workflows/ci.yml`) runs the lot on Linux,
macOS and Windows with Python 3.10 and 3.13, one leg on the oldest supported SDK, and weekly
against the newest SDKs with the evals (`weekly.yml`, the owner's key as a secret).

Agreement on the evaluation set (`bodh eval`, sixty articles for the example student): not yet
measured; the owner runs it once and puts the figure and its date here.

`docs/glossary.md` has the words, `docs/internals.md` the rules the code keeps (threads and
connections, the run lock, failing open, exit codes, stored words), `CONTRIBUTING.md` how to add a
test, a reader or a provider, `docs/live-checks.md` the few checks only the owner's account can run. Every error bodh raises on purpose exits with a code a script can
act on: 2 configuration, 3 BazQux, 4 the model, 5 a run in progress, 1 anything else (`bodh run`
alone answers 0 when another run holds the lock: the timer's own overlap is not an error). Prose
is in British spelling; identifiers follow Python's conventions (`normalize`, `color` in CSS).
