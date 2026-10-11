# triage

Keeps your BazQux unread list worth reading. A teacher (Claude Sonnet 5.5) scores every unread
article once as teaching material for you: writing quality, what you learn, level fit, breadth,
relevance. The worst are marked read in BazQux itself; you choose how deep the cut goes and can
run it again. NetNewsWire on the Mac and the iPhone simply syncs. Every machine action is a tag,
every cut can be undone, and nothing but mark-as-read ever changes in your reader.

Design: the "RSS Triage System Design" doc in the RSS reader project. This file is the runbook.

## What is in this repository

| Path | What | Committed? |
| --- | --- | --- |
| `RUNBOOK.md` | The operator's sheet: what runs by itself, the daily routine, every command, every knob, what to do when something looks wrong | yes |
| `src/triage/` | The package: BazQux client, store, teacher, cut, feedback, learner, dashboard, progress, control room | yes |
| `src/triage/prompts/` | The teacher, second-look and learner prompts (`./prompts/` next to `config.yaml` overrides them) | yes |
| `tests/` | 57 tests with fakes for BazQux and the models; `pytest -q` runs in seconds | yes |
| `launchd/`, `install.sh`, `install-launchd.sh`, `laptop-install.sh` | Install and schedule on a Mac | yes |
| `config.example.yaml`, `.env.example`, `profile.example/` | Templates: copy to `config.yaml`, `.env`, `profile/` | yes |
| `config.yaml` | Your folders, floors, models, budget | yes (nothing secret in it) |
| `.env` | BazQux password, Anthropic key, heartbeat URL | never |
| `profile/` | Your seed and learner model, with their own git history | never |
| `data/`, `dashboard/` | The SQLite cache, logs, the rendered dashboard | never |

Anyone else can clone it, run `./install.sh`, fill in `.env`, write `profile/seed.md` and set their own
folders in `config.yaml`; nothing in the code is tied to one account.

## What runs

| Command | What it does |
| --- | --- |
| `triage run` | The timer's job, every 30 minutes: collect feedback (stars, reads, rescues), fetch new articles, collect the judgments the Batches API finished, submit the new articles, tag scored ones `score-90` … `score-0`, apply the automatic floor (with an Opus 5.5 second look on close calls that have more to read), regenerate the dashboard, ping the heartbeat. Cuts nothing while `cut.enforce: false`. `--sync` waits for every model call instead of batching. |
| `triage blast` | One deliberate pass over everything pending, as fast as the API allows: synchronous, 8 calls at once (`--workers`), rounds of 1,000 until nothing is pending. `--model claude-haiku-5-5` uses the fastest model as a stand-in (20x cheaper; it never touches the teacher's judgments, and the teacher re-judges what it rates 50 or more on the next runs, `scoring.rejudge_min_score`). `--all-ages` includes the articles older than the scoring window; `--budget 40` raises today's cap; `--max-seconds 600` stops starting calls after ten minutes. Scores and tags, cuts nothing; Ctrl-C and run again to continue. |
| `triage progress [--watch]` | How far the teacher has got: judged, in flight, pending, by folder; while a run is going, its phase, the rate per minute and the ETA; batches; spend and cache hits; the log tail. `--watch` redraws every 5 s. |
| `triage prune` | Your cut: keep the 40 best (`--keep N`), or cut a share (`--fraction 0.3`), or everything under a score (`--below 65`). `--dry-run` shows the boundary first. Run it again to cut deeper. |
| `triage undo` | Reverse the last cut. |
| `triage bankruptcy --older-than 14` | Day 0: mark everything older than two weeks read, stars and protected articles exempt. Reversible. |
| `triage status`, `triage why ID` | The whole system at a glance: what is running, the launchd jobs, the control room, the pile, spend, the last runs; why one article got its score. |
| `triage panel` | The control room in the browser (`http://127.0.0.1:8766`, kept alive by launchd): the same status live, buttons for run, stop, pause/resume the schedule, enforce on/off, prune (dry run first), undo, bankruptcy, blast, weekly, report, doctor; command output and the log streamed; the dashboard underneath. |
| `triage stop` | Stop the run, prune or blast in progress, the same as Ctrl-C in its terminal. Fail open: what was judged stays; a cut half-written to BazQux is finished by the next run. |
| `triage halt` | Stop everything: pause all four launchd jobs, interrupt the run in progress, end every other triage process including the control room. `triage schedule resume` brings the jobs back. |
| `triage schedule [status\|pause\|resume\|kick]` | The launchd jobs: show them, pause them all (nothing fires until resume; `--job run` for one), resume, or kick a run now. |
| `triage enforce on\|off` | Flip the automatic floor in config.yaml: on cuts from the next run, off is shadow mode. |
| `triage want "x"`, `triage meh "x"` | Nudge the learner model now instead of waiting for Sunday (the `more` and `less` tags do the same from BazQux). |
| `triage weekly` | Sunday: rewrite the learner model from the week's reading (kept only if the eval gate passes), write the weekly note, run the drift check. |
| `triage report` | Monthly feed report with proposals (unsubscribe, move to Firehose, protect). |
| `triage replay --archive … --golden …` | Judge a sample of FeedBrain's archive offline; check the gate before anything goes live. |
| `triage doctor [--live]` | Configuration and connectivity checks. Run after every update. |

## Install

Requirements: macOS, Python 3.10+ (`brew install python`), git. Tailscale only for the dashboard and the Shortcut.
Keep the checkout somewhere plain such as `~/triage`: launchd cannot run jobs that live in `~/Documents`,
`~/Desktop` or `~/Downloads` (macOS privacy protection denies them with "Operation not permitted"), and a
path with spaces makes pip wrap the command in `/bin/sh`, which is denied first. The installer refuses both.

**One command on a laptop** (reuses a FeedBrain `../.env` if one sits beside the checkout, installs the
launchd agents in shadow mode, scores a first batch):

```bash
bash ~/triage/laptop-install.sh
```

Step by step, or on the Mac mini:

```bash
cd ~/triage
./install.sh                      # venv, package, config.yaml and .env (copied from FeedBrain's), doctor
$EDITOR config.yaml               # folders, target, floor; leave enforce: false for the shadow days
$EDITOR profile/seed.md           # who the student is; 15 lines, rarely touched (copied from profile.example)
.venv/bin/triage doctor --live    # logs in, lists folders, calls both models once (a few cents)
./install-launchd.sh              # laptop: agents, run while logged in, no sudo
./install-launchd.sh --daemon     # Mac mini: daemons, run without login (asks for sudo)
```

In BazQux, before relying on it: create a smart stream named **Must read** with your highest-intent
terms (its members are never cut), and put the feeds you always read in a folder named **Always read**
and the general-news firehoses in a folder named **Firehose**. Folder names are matched against
`cut.folders` in `config.yaml`; moving a feed between folders in BazQux changes how it is treated.

## Day 0 to steady state

1. **Bankruptcy or blast.** `triage bankruptcy --older-than 14` clears the pile (`triage undo` if you regret it); or `triage blast --model claude-haiku-5-5 --all-ages` judges every unread article once, now, for a few dollars, so the pile can be pruned on merit (the teacher then re-judges the promising ones). Otherwise the backlog is scored 1,200 articles a run through the batch API, newest first.
2. **Replay.** `triage replay --archive ../feedfilter/data/feedfilter.db --golden ../evals/golden.jsonl`
   judges 500 archived articles plus everything you starred. The gate passes when no starred article
   falls below the floor. Costs about a dollar.
3. **Shadow days.** With the launchd jobs installed and `cut.enforce: false`: For two or three days,
   look at `triage prune --dry-run` and the dashboard's near misses. Articles get scored and tagged;
   nothing is cut.
4. **Enforce.** Set `cut.enforce: true` and `cut.default_floor: 25`. The floor removes clear junk; you
   prune to 40 with the Shortcut or `triage prune` whenever the list is long.
5. **Raise the bar.** After a week, set the floor to the boundary you have been choosing (the dashboard
   shows it), set `Firehose` to 70, act on the first feed report. From then on: weekly note on Sundays,
   feed report on the 1st, seed reviewed when your priorities change.

## Cost

Two readers, priced to run all day. The first pass goes through the **Message Batches API** (half
price): a run submits what arrived, the next run, thirty minutes later, records the judgments and tags
them. The teacher writes a compact judgment (five numbers, a twelve-word lesson, one-word codes), which
halves its output tokens, the dear part. Its prompt prefix is cached for an hour, so runs share it. The
**second look** (Opus, full text) is only taken when the article has more text than the opening the
first pass saw, at most once per article, and travels through the same batch: a close call waits one
run before it can be cut. Unread articles older than `scoring.max_age_days` (14) are never scored;
`triage bankruptcy` is for them. Folders with `score: false` are never sent to the teacher.

At about 1,300 arrivals a day that comes to roughly $0.65 per 1,000 judged, under a dollar a day,
against $1.90 per 1,000 before. `triage progress` shows the actual rate and the cache hit share;
`models.daily_budget_usd` still caps a bad day. Trade-offs: a judgment lands 30 to 60 minutes after an
article arrives instead of at once (the Batches API finishes most batches within the hour), and a
close call survives one extra run. `triage run --sync` restores the immediate path when you want it
(`doctor --live` and `replay` always run synchronously; `scoring.workers` model calls run at once).

## launchd

`install-launchd.sh` renders the four templates in `launchd/` (the run, the Sunday rewrite, the monthly
report, the control room) with this checkout's path and installs them:
as LaunchAgents (`~/Library/LaunchAgents`, no sudo, run while you are logged in; the laptop) or, with
`--daemon`, as LaunchDaemons (`/Library/LaunchDaemons`, run without login; the Mac mini). Run it again
after moving the folder.

```bash
launchctl print gui/$(id -u)/com.antriksh.triage      # agent status (daemon: sudo launchctl print system/…)
launchctl kickstart -k gui/$(id -u)/com.antriksh.triage   # run once now
launchctl bootout gui/$(id -u)/com.antriksh.triage    # stop
tail -f ~/Library/Logs/triage/triage.log
```

The job takes a lock, so an overrun never overlaps the next start, and it kills itself after 15 minutes;
the next run picks up what is left. A laptop that sleeps simply misses runs and catches up on wake; a
backlog drains at `scoring.batch_max_per_run` (1,200) articles per run, or `scoring.max_per_run` (400)
with `--sync`, where `scoring.workers` calls run in parallel. For an always-on Mac mini also keep it
awake and self-healing:

```bash
sudo pmset -a sleep 0 disksleep 0 displaysleep 10 autorestart 1
```

## Control room

`./install-launchd.sh` also installs `com.antriksh.triage.panel`, a small local web server kept alive
by launchd. Open `http://127.0.0.1:8766` on the Mac. For the iPhone, publish it on your tailnet once:

```bash
tailscale serve --bg 8766         # then https://<your-mac>.<tailnet>.ts.net
```

The page shows the state of the system (idle or what is running and its phase, the mode, the next
scheduled run), the launchd jobs, the pile, spend, today's counts, the last runs, the log, and the
dashboard underneath, refreshed every three seconds. The buttons start the same commands you would
type, as child processes whose output streams into the page; the ones that change BazQux ask first.
Stop interrupts whatever holds the run lock, started from the page or not. The server listens on
127.0.0.1 only and accepts actions only from its own page; Tailscale adds the identity check on the
tailnet. From the terminal the same levers are `triage status`, `stop`, `halt`, `schedule`, `enforce`.

The installer waits up to 15 s for the page to answer and, if it does not, prints the job's state and
the last lines of `~/Library/Logs/triage/panel.err.log` and `data/panel.log`, where the server writes
why it could not start (a port in use, say). Later, `triage status` says the same; `triage panel` in a
terminal runs it in the foreground with the error on screen.

## Heartbeat and alerts

Create a check on [healthchecks.io](https://healthchecks.io) with period 30 minutes and grace 30
minutes; put its ping URL in `.env` as `HEALTHCHECK_URL`. Every good run pings it with the run
summary; a failed run pings `/fail` with the error; an hour of silence (machine off, network down,
launchd not firing) raises an alert. Choose email there, or add its ntfy or Pushover integration for
a phone push. The weekly note is written to the dashboard folder and, if `obsidian_note` is set in
`config.yaml`, into your vault.

## Dashboard

`triage run` regenerates `dashboard/index.html` after every run. Publish it on your tailnet:

```bash
tailscale serve --bg --set-path / "$(pwd)/dashboard"
tailscale serve status            # shows the https URL, open it on the Mac or the iPhone
```

The progress panel is live: the page polls `progress.json` every 4 seconds, which every run (and
`triage blast`) rewrites as it goes, so the bars, the rate, the ETA, the phase ("judging 400 of 1,200"),
the folder table, the batches and the log tail move while you watch; the dot pulses while a run holds the
lock. The polling needs the page served over http (`tailscale serve`, or `python3 -m http.server 8765`
in the `dashboard` folder); opened as a file it shows the numbers as of the last render.

Panels: progress (judged, in flight, pending, by folder; batches; cost per 1,000), flow, calibration
(read rate per score band; the check on the teacher), near misses (the best of what was cut, rescue by
marking unread), feeds (volume, mean score, cut, read, star, rescue rates), learnings (the week's
lessons by concept), map (concepts read in 90 days and the frontier), learner model (current text and
its diff), health (runs, spend, heartbeat). The same progress numbers in the terminal: `triage progress`.

## Tags in BazQux

| Tag | Who writes it | Meaning |
| --- | --- | --- |
| `score-90`, `score-70`, `score-50`, `score-30`, `score-0` | triage | The article's score band. Open a tag with the unread filter on and you have a ranked view. |
| `cut` | triage | Marked read by triage (floor, prune or bankruptcy). Mark it unread anywhere to rescue it. |
| `wrong` | you | A command, on a cut article you wanted: it is marked unread again, counts as a rescue and as the strongest positive. The run removes the tag when done. |
| `prune` | you | A command, on any article: the next enforcing run prunes to the target. Removed when done. |
| `great`, `good`, `meh`, `bad` | you | Your grade, one per article: the best kind (more like this; the Sunday rewrite may not lose it), worth the time, not worth it, junk the teacher should not have kept. |
| `basic`, `heavy` | you | The level missed: too elementary for you, too dense or specialist for you now. |
| `more`, `less` | you | A steer: more or less of this article's concept, written into the learner model as a dated nudge that the teacher reads from the next run. |

Stars are yours: a starred article is never cut and counts like `great`. Verdict tags stay on the
article as your own record (open the `great` tag in BazQux and you have your best-of list) and are
recorded once; add a different grade to change your mind (when two are on, the harsher wins), and
removing a tag alone retracts nothing. `triage why ID` shows what is recorded for an article; the
dashboard's "Your verdicts" panel sets your grades against the teacher's bands and lists where you
disagree most. How all of this changes the judging is in RUNBOOK.md, "How it learns".

## Updating

```bash
cd ~/triage && git pull && ./install.sh
```

`install.sh` reinstalls the package and runs `triage doctor`. The running launchd jobs pick up the new
code on their next start. Prompts live in `src/triage/prompts/`; a copy in `./prompts/` next to
`config.yaml` overrides the packaged one. Changing `scoring.prompt_version` re-scores unread articles.

## When something breaks

| Symptom | Where to look | What happens meanwhile |
| --- | --- | --- |
| Heartbeat alert, no error | Is the Mac mini on and online? `sudo launchctl print system/com.antriksh.triage` | Nothing is cut; unread grows |
| launchd job: `last exit code = 126`, "Operation not permitted" in the logs | The checkout sits in Documents, Desktop or Downloads, or its path has spaces; move it (see Install) and reinstall | Nothing runs on the schedule; manual commands work |
| "BazQux login failed (YearSubscriptionExpired)" | Renew BazQux | Run aborts before any write |
| "daily model budget … reached" | `models.daily_budget_usd` in config.yaml | Scoring pauses until midnight; nothing is cut |
| "in flight" stays high for hours | `triage progress`: a batch still in progress; the API finishes most within the hour, all within 24 | Articles stay unread until judged; a batch not seen to end in 26 h is given up on and resubmitted |
| "requests failed: … output_config" | The teacher model rejected structured outputs; triage switches itself to plain JSON (`plain_json` in the kv table) | The failed requests are resubmitted next run |
| "floor would cut … stopping before any write" | The prompt or model misbehaved; `triage why` on a few new articles | Nothing is cut this run |
| Rescues above 5% in a week | The floor is too high for your taste; it drops to 25 on its own and alerts | Manual prune unaffected |
| Database lost | Run once; bucket tags in BazQux rebuild the scored set, unread articles are re-scored | Costs a few dollars |

Logs: `~/Library/Logs/triage/triage.log`. State: `data/triage.db` (an audit cache; BazQux is the
record). Learner model history: `git log` in `profile/`.

## Development

```bash
.venv/bin/pytest -q        # 57 tests: ids, scoring rules, cuts, breakers, client, feedback, batches, blast, progress, pipeline end to end
.venv/bin/ruff check src tests
```
