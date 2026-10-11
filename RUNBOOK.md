# bodh: how to run it, every day

Everything needed to run the system on your own, several times a day, on every new article.
The code lives in `~/bodh` (a git repository); the design and the internals are in README.md;
the words are in `docs/glossary.md`. This file is the operator's sheet: the commands, the
routine, the knobs, what to do when something looks wrong.

Conventions: every command below is typed in Terminal on the Mac, in `~/bodh`, as
`.venv/bin/bodh ...`. To type `bodh ...` from anywhere, add two lines to `~/.zshrc` once:

    export BODH_HOME="$HOME/bodh"
    alias bodh="$HOME/bodh/.venv/bin/bodh"

(then open a new Terminal window). `BODH_HOME` matters: without it, `bodh` run from another
folder looks for `config.yaml` there and, finding none, refuses to do anything (exit code 2).


## 1. What runs by itself

Once `./install-launchd.sh` has been run, launchd does the daily work while you are logged in:

| Job | When | What |
| --- | --- | --- |
| `bodh.run` | every 30 minutes | `bodh run`: one full pass over everything new (section 3) |
| `bodh.weekly` | Sunday 07:00 | `bodh weekly`: the profiler rewrites the learned profile from the week's stars, reads and rescues; writes the weekly note; drift check |
| `bodh.report` | 1st of the month, 07:30 | `bodh report`: the feed report with proposals, in `dashboard/` |
| `bodh.console` | always (kept alive) | the console at http://127.0.0.1:8766 |

A laptop that is asleep simply misses runs and catches up on wake. Nothing needs to be typed for
new articles to be judged; the rest of this file is for looking, cutting, teaching and fixing.


## 2. The daily routine

Three or four times a day, in this order; two minutes each time.

1. Look: open http://127.0.0.1:8766 (or `bodh status`). The top line says idle or what is
   running and its phase; then the mode (scoring-only or autocut), the next scheduled run, the
   unread list (unread, judged, with the model, pending), today's spend, the last runs.
2. Read in BazQux or in a reader that syncs with it. In BazQux the judgments are tags: `score-90` (assign it),
   `score-70` (worth the hour), `score-50` (reference), `score-30` and `score-0` (skip, junk).
   Open a tag with the unread filter on. Star what was good.
3. Cut, when the unread list is too long:

       bodh prune --dry-run        # shows the boundary score and what would go
       bodh prune                  # shows the same, asks, then keeps the best 40 (prune.keep)
       bodh undo                   # if you regret it: everything from the last cut comes back unread

   `bodh prune --keep 60`, `--fraction 0.3` (cut the worst 30%), `--below 65` (cut everything
   under 65) are the other shapes; `--yes` skips the question (a script, the console).
   Starred articles, the `Must read` stream, protected folders and anything you rescued never
   move. What an undo brought back is left alone by autocut from then on (a prune may still
   take it).
4. Teach, in passing (section 5): star or tag `great` what was best, `good`, `meh` or `bad` the
   rest; `rescue` on a cut article you wanted; `more` or `less` to steer by an article's concept.

Judgments arrive within an hour or two of an article (the next run submits it, the Batches API
finishes most batches within the hour, the run after that collects them; half price). When you
want the unread list judged right now:

    bodh run --sync                # every model call now, full price, up to 400 articles
    bodh progress --watch          # live: judged, with the model, pending, rate per minute, ETA


## 3. What one run does (`bodh run`)

In order, with the phase shown on the console and in `bodh progress`:

1. talking to BazQux: reads what is unread and starred now; collects your feedback (stars,
   articles you read, the `rescue` and `prune` commands, your verdict tags: grades, levels, steers).
2. fetching new articles: everything unread the database has not seen, with its text.
3. collecting finished batches: the judgments submitted last time come back and are stored
   (in every mode: a batch submitted before `models.batch` was turned off is still collected).
4. submitting a batch: up to 1,200 new articles go to the judge (Sonnet 5.5), 20 per call.
   Articles older than 14 days (`limits.max_age_days`) and the folders with `judge: false` are
   never sent; an article the model has answered about three times with no judgment is left
   alone (`bodh safety` counts them, `bodh safety reset` tries again).
5. tagging in BazQux: every judged article gets its `score-…` tag.
6. autocut, only when `autocut.enabled: true`: unread articles under their folder's cut-off
   are marked read and tagged `cut`, at most 500 a run; a close call (within 10 points of the
   cut-off, with more text than the judge saw) gets an Opus 5.5 review first and waits one
   run; 20% of what autocut leaves is reserved for new ground; a `prune` tag seen in step 1
   prunes to `prune.keep` as well. In scoring-only mode (`autocut.enabled: false`) nothing is cut, ever.
7. rendering the dashboard; heartbeat ping.

A run takes a lock: a second `bodh run` while one is going says so and exits. A run starts no
model call after 14 minutes and ends itself after 15; the next one picks up what is left. If
BazQux cannot serve the `Must read` stream, the run judges and tags but cuts nothing.


## 4. Modes: scoring-only and autocut

You start in **scoring-only mode** (`autocut.enabled: false`): everything is judged and tagged,
nothing is cut automatically; only `bodh prune` and `bodh clear-old` cut, and only when you run them.

When the scores have looked right for a few days (open `score-30` and `score-0`: nothing you
would have read?), switch autocut on:

    bodh autocut on                # edits config.yaml; the next run cuts under the cut-offs
    bodh autocut off               # back to scoring-only

The cut-offs: `autocut.cutoff: 35` for every folder, and in `folders` the ones with their
own (in the example config, `News` cuts below 70 and `Aggregators` below 60). Start with 25
if you want it gentle; raise it when the kept list is still too long. `bodh run --shadow` is
one run without cutting even with autocut on.

The safety limit: an autocut that would cut more than 95% of at least 50 newly judged articles
stops before writing, the run fails, and autocut stays off (every later run says so in its notes,
`bodh status` says SAFETY LIMIT TRIPPED) until you have looked at a few scores and typed
`bodh safety reset`.


## 5. Teaching the judge: the tags

Your opinion of an article is a tag on it in BazQux (web or phone; most synced readers carry stars only).
Three small sets: a grade, the level, a steer. Use one word or all three; a word you do not use
costs nothing. The run reads them every 30 minutes.

| Tag | Put it on | It says |
| --- | --- | --- |
| `great` | the best article you read | the best kind; more like this. Counts like a star: the Sunday rewrite is rejected if it would push any `great` article under the cut-off |
| `good` | an article worth the time | a positive example |
| `meh` | one that was not worth it | a negative example |
| `bad` | junk the judge kept | the strongest negative; a `bad` on a high score is what the Sunday rewrite learns from most |
| `basic` | an article too elementary for you | introductions to this concept no longer fit; the judge scores their `fit` low from the next run |
| `heavy` | one too dense or specialist for you now | gentler entries to this concept fit better |
| `more` | any article | more of its concept: a dated steer goes into the learned profile and the judge reads it from the next run |
| `less` | any article | less of its concept, the same way |

Rules: one grade per article (add another to change your mind; when two are on, the harsher one
wins; removing a tag alone retracts nothing). The tags stay on the article as your own record, so
the `great` tag in BazQux is your best-of list. Each is recorded once.

Two tags are commands rather than opinions, and the run removes them when done:

| Tag | It does |
| --- | --- |
| `rescue` | on a cut article you wanted: it comes back unread, counts as a rescue and as the strongest positive, and is never cut again by any path; many rescues in a week lower the cut-off to 25. On an article that was never cut it is the positive only. `wrong`, its name before October 2026, is read the same way |
| `prune` | on any article: the next run with autocut on prunes to `prune.keep` (the request is kept until one does) |

Without a tag: a star counts like `great` and protects the article from every cut; an article you
read (it goes read in BazQux without a cut) is a mild positive, inferred within 7 days of arrival.
From the terminal: `bodh steer more "liquid cooling economics"` and `bodh steer less "celebrity"` are
`more` and `less` by phrase; `bodh mark ID great` (or any other verdict, or `rescue`) is the tag by id;
`bodh why ID` shows an article's score, sub-scores, lesson, concept, which model judged it, and what
you recorded on it. `ID` is the BazQux item id, long form (`tag:google.com,2005:reader/item/…`) or the
number. The two biggest levers are not tags: `profile/about.md`, fifteen lines on who the student
is, and `profile/learned.md`, which the profiler (Opus) rewrites on Sundays (`bodh profile show`,
`bodh profile revert`).


## 5a. How it learns, for you specifically

The judge never trains; what changes is what it is told about you, in three layers, each on
its own clock.

1. **Every call, the concept lists.** The message with each batch of articles opens with lists
   drawn from your record of the last 90 days: concepts you loved (stars, `great`), concepts you
   read or marked `good` or rescued, concepts you marked `bad` or `meh` or dismissed, and the
   concepts where the level missed (`basic`, `heavy`), each with a count. The judge uses them
   for three of the five sub-scores: `fit` (your level, from what you finish and what you call
   basic or heavy), `breadth` (new ground is rewarded against what you already read), and
   `relevance`. This layer moves the moment a run has collected the tag.
2. **From the next run, the steers.** `more`, `less` and `bodh steer` write a dated
   line into the learned profile ("the student wants more on liquid cooling economics"), which sits
   in the judge's prompt from the next run on.
3. **Every Sunday, the learned profile.** The profiler (Opus) reads `about.md`, the current learned
   profile and the week's evidence: every positive, negative, level and steer, each with the score
   the judge gave, the lesson and the concept. It rewrites the profile's five sections: what you
   have been learning, what you are moving past, the concepts covered, two or three expansion
   topics for the coming week (articles on them get +10), and notes to the judge on how to judge
   for you now. The disagreements are the richest lines: a `bad` on an 85 or a `great` on a 40
   says what the judge has been getting wrong for you, and the notes carry the correction.

Guardrails, so a week of odd reading cannot wreck it: the rewrite may move no topic more than one
band a week; it may not contradict `about.md`; it is rejected if it is over 400 words; and it is
rejected by the eval gate if, with the new profile in place, any article you starred, marked `great`
or rescued would score under the cut-off. Fewer than five positives in a week and the profile is kept
as it is. Every version is kept (`bodh profile revert`); the dashboard shows the week's diff.

Two more loops run beside the profile. Rescues (`rescue`, or marking a cut article unread) above 5%
of a week's cuts (twenty cuts at least; clear-old and undone runs do not count) lower the cut-off
to 25 until they fall. And 20% of what autocut would leave is reserved for new ground: articles of
decent quality on a concept not in your record, so the judge keeps showing you fields you have not
read yet.

What to expect: the concept lists act on the next run; the steers within the hour; the learned
profile after the first Sunday with five or more positives. The dashboard's "Your verdicts" section
sets your grades against the judge's bands and lists where you disagree most; when the
disagreements shrink week by week, it is learning you.


## 6. Big unread lists

| Situation | Command |
| --- | --- |
| thousands pending and you want them judged now, cheaply | `bodh catch-up` (synchronous, 8 calls at once, 1,000 a round, with the quick judge: 20x cheaper; the judge re-judges what it rates 50 or more on the following runs; Ctrl-C and run again to continue) |
| the same, with the judge itself | `bodh catch-up --model claude-sonnet-5-5` (full price) |
| the list includes articles older than 14 days | add `--all-ages` |
| today's budget would stop it | add `--budget 20` |
| only ten minutes now | add `--max-seconds 600` |
| the old unread is not worth judging | `bodh clear-old --days 14 --dry-run`, then without `--dry-run`: everything older is marked read and tagged `cut` (stars and protected folders exempt; `bodh undo` reverses it) |


## 7. Stopping and restarting

| Want | Command |
| --- | --- |
| stop the run, prune or catch-up in progress | `bodh stop` (the same as Ctrl-C in its terminal; a cut half-written to BazQux is finished by the next run) |
| nothing fires for a while | `bodh schedule pause` (`--job run` for one job; without it the console pauses too); `bodh schedule resume` |
| a run now, through launchd | `bodh schedule kick` (refused while a run holds the lock) |
| stop everything, console included | `bodh stop --all`; `bodh schedule resume` brings the jobs back. It ends the commands of this checkout started by their full path (launchd's jobs, the alias above); one started as `.venv/bin/bodh …` from inside the folder is not found, Ctrl-C that one |
| after a code update, or after moving the folder | `./install.sh && ./install-launchd.sh` (`bodh stop --all` first when a run may be going: a run started by the old code is not seen by the new one) |
| the console in the foreground | `bodh console` (Ctrl-C ends it; launchd restarts its own copy) |


## 8. Watching

| Where | What |
| --- | --- |
| http://127.0.0.1:8766 | the console: status, buttons for every command above, their output streamed, the log, the dashboard underneath. It answers only to a Host of 127.0.0.1, localhost or a `*.ts.net` name |
| `bodh status` | the same in the terminal |
| `bodh safety` | the safety limit, and the articles the judge gave up on |
| `bodh progress [--watch]` | the judge's progress: judged, with the model, pending, by folder, batches, spend, cache hits, cost per 1,000, the log |
| `dashboard/index.html` | the dashboard: flow, calibration, near misses, feeds, learnings, knowledge map, the learned profile, health (no log: the page may be published) |
| `~/Library/Logs/bodh/bodh.log` | the run log (`tail -f`); `launchd.*.log`, `console.err.log` next to it |
| `data/console.log` | why the console could not start, if ever |
| healthchecks.io | a check with period 30 minutes and grace 30 minutes, its ping URL in `.env` as `HEALTHCHECK_URL`: an hour of silence (laptop off, network down, launchd not firing) raises an alert; a failed run pings `/fail` with the error |
| phone | `tailscale serve --bg 8766`, then https://<your-mac>.<tailnet>.ts.net is the console |


## 9. The knobs (config.yaml)

The file is read at the start of every run; edit it with any editor, no restart needed (the
console picks it up on its next refresh). A misspelt key, a value out of range or an
unknown timezone is refused with the key named, by every command. Current values in brackets.
A file written before October 2026 still loads: every old key is mapped to its new name with one
warning that lists the moves (`bodh doctor` lists them too); the mapping goes away in the next release.

**Top level**

| Key | Means |
| --- | --- |
| `timezone` [the machine's zone] | the day boundary for the budget and the counts; an IANA name such as `Europe/Lisbon`. Left out, the zone the machine is set to (`$TZ`, else `/etc/localtime`), UTC when neither says |
| `data_dir`, `profile_dir`, `dashboard_dir` [./data, ./profile, ./dashboard] | where the database, your profile and the dashboard live |
| `obsidian_note` [empty] | a path for the weekly note, e.g. `~/Obsidian/Main/Bodh/Weekly.md` |

**models**

| Key | Means |
| --- | --- |
| `judge` [claude-sonnet-5-5] | reads every article once; the judgment |
| `reviewer` [claude-opus-5-5] | re-reads close calls with the full text; can only save an article |
| `profiler` [claude-opus-5-5] | rewrites `profile/learned.md` on Sundays |
| `batch` [true] | judgments through the Batches API: half price, within an hour or two. `false` = every call at once, full price |
| `batch_discount` [0.5] | what the API charges for batches, for the spend figures |
| `cache_ttl` [1h] | how long the judge's cached prompt prefix stays warm between runs (`5m` or `1h`) |
| `daily_budget_usd` [8.0] | the day's cap: judging pauses when passed; nothing is cut because of it. A normal day is under a dollar |
| `est_usd_per_article` [0.001] | the budget check before a batch is submitted |
| `prices_per_mtok` | input, output, cache-read price per million tokens for each model; update when prices change. A model with no price is charged as the most expensive known one, with a warning |

**judge**

| Key | Means |
| --- | --- |
| `prompt_version` [teacher.v2] | which prompt in `src/bodh/prompts/` (a `./prompts/` folder next to config.yaml overrides); changing it re-judges everything unread |
| `weights` [0.30 quality, 0.30 learning, 0.15 fit, 0.15 breadth, 0.10 relevance] | how the five sub-scores make the score |
| `expansion_bonus` [10] | points for an article on one of this week's expansion topics |
| `home_angle` [empty] | your home ground as a short phrase (`India`, `Brazil`, `the Pacific Northwest`): the judge flags an article with that angle and it gets `home_bonus`; empty turns the flag off |
| `home_bonus` [10] | the points a home-angle article gets |
| `articles_per_call` [20] | articles in one judge call |
| `text_chars` [1200] | the opening the judge sees on the first pass |

**review**

| Key | Means |
| --- | --- |
| `window` [10] | an autocut candidate within this many points of the cut-off is reviewed |
| `chars` [6000] | the text the review sees |
| `max_per_run` [50] | synchronous runs: reviews per run at most |

**catch_up**

| Key | Means |
| --- | --- |
| `model` [claude-haiku-5-5] | the quick judge `bodh catch-up` uses unless `--model` says otherwise |
| `rejudge_at` [50] | the judge re-judges what the quick judge rated at least this; `null` turns it off |

**limits**

| Key | Means |
| --- | --- |
| `per_run_sync` [400] | articles judged per synchronous run |
| `per_run_batch` [1200] | articles submitted per batch run |
| `parallel_calls` [4] | model calls running at once in synchronous runs (`--sync`; `--workers` overrides) |
| `max_age_days` [14] | unread articles older than this are never judged; `clear-old` is for them; `catch-up --all-ages` includes them once |

**autocut**

| Key | Means |
| --- | --- |
| `enabled` [false] | autocut on or off (`bodh autocut on/off`) |
| `cutoff` [35] | the cut-off for folders without their own |
| `max_per_run` [500] | the most one run may cut; a broken prompt costs one undo |
| `new_ground_share` [0.20] | share of what autocut leaves kept for new ground (good quality, concept unseen in 90 days) |

**prune**

| Key | Means |
| --- | --- |
| `keep` [40] | what `bodh prune` keeps by default |
| `max_share_per_concept` [0.25] | in a prune, no concept may hold more than this share of the survivors |

**protect**: `must_read_stream` [Must read], the BazQux smart stream whose members are never cut.

**folders**: per folder, `cutoff: N` (its own cut-off), `protected: true` (never cut), `judge: false`
(never judged). Folder names must match BazQux exactly; `bodh doctor --live` warns about ones that do not.

**tags** [score-90 … score-0, cut, rescue, prune, great, good, meh, bad, basic, heavy, more, less]:
the tag names written to and read from BazQux; change them only before the first run. `bands` maps
the score bands to their tags.

**safety**

| Key | Means |
| --- | --- |
| `max_cut_share` [0.95], `min_judged` [50] | a run that would cut more than 95% of at least 50 newly judged articles stops before writing: the prompt or model has probably broken. Autocut stays off until `bodh safety reset` |
| `rescue_rate_week` [0.05], `lowered_cutoff` [25] | rescues above 5% of a week's cuts lower the cut-off to 25 until they fall |
| `mark_all_read_threshold` [30] | this many kept articles going read in one run counts as mark-all-as-read, not as 30 reads. More than 80% of the pool going read at once is believed only when the next run sees the same (an empty answer from BazQux looks like that) |

**profile**: `concept_window_days` [90], how far back stars, reads and rescues count as what the
student knows.

**.env** (never committed, `chmod 600`): `BAZQUX_EMAIL`, `BAZQUX_PASSWORD`, `ANTHROPIC_API_KEY`,
`HEALTHCHECK_URL` (optional). `bodh doctor` says which are set without showing them. `config.yaml`
is not committed either (it is yours; the repository carries `config.example.yaml`), so back it up
with the rest of your files.

**profile/**: `about.md` is yours (the install leaves a worked example there; `bodh doctor` and the
run refuse it until it is replaced); `learned.md` is the profiler's, rewritten on Sundays,
versioned in `profile/`'s own git history and in the database (`bodh profile revert`). A `profile/`
from before October 2026 (`seed.md`, `learner.md`) is renamed by `./install.sh`, and read as it is
until then.


## 10. When something looks wrong

| Symptom | Do |
| --- | --- |
| anything | `bodh doctor` (configuration, prompts, credentials present, database; exit code 1 when something is wrong), then `bodh doctor --live` (logs in to BazQux, one call to each model, a few cents) |
| the run notes say the safety limit tripped, or `bodh status` says SAFETY LIMIT TRIPPED | look at a few newly judged articles (`bodh why ID`); if the scores are sane, `bodh safety reset`; if not, the prompt or the model changed: check `models.judge`, the prompt file, `bodh doctor --live` |
| `bodh safety` says articles were given up on | the model answered three times with nothing for them (a prompt that does not fit the model, usually); fix that, then `bodh safety reset` |
| the console does not answer | `bodh status` shows "console: not answering · last: …"; `tail -5 data/console.log`; `./install-launchd.sh` reinstalls the job and waits for it; `bodh console` runs it in the foreground with the error on screen |
| "already running" when you run something | something holds the lock: `bodh status` names it; `bodh stop` ends it |
| a launchd job shows exit code 126 or "Operation not permitted" | the checkout is under Documents, Desktop or Downloads, or in a path with spaces; keep it at `~/bodh` (`./install-launchd.sh` refuses the others) |
| scores stopped appearing | `bodh progress`: is the budget reached (spend near 8.00)? are batches with the model for more than an hour (the API is slow; they are collected on the next run, given up after 26 h and resubmitted)? `tail -50 ~/Library/Logs/bodh/bodh.log` |
| autocut cut something good | tag it `rescue` (it comes back); `bodh undo` for the whole last cut; lower the folder's cut-off in `folders` or `autocut.cutoff` |
| the judge's taste drifted | `bodh profile show`; `bodh profile revert`; edit `profile/about.md` |
| a model id was rejected ("model call rejected") | `models.*` in config.yaml, or the key in `.env`; `bodh doctor --live` |
| after `git pull` or any code change | `./install.sh` (also runs doctor), then `./install-launchd.sh`; `.venv/bin/pytest -q` runs the tests |


## 11. What it costs

Sonnet 5.5 through batches with the cached prefix: about $0.65 per 1,000 judged, so under a
dollar for a 1,300-article day; `bodh progress` shows the measured figure and the cache hit
share. A full catch-up of 30,000 articles: $35 to $40 with Sonnet synchronous (full price), about $2.50
with the quick judge. `models.daily_budget_usd` caps a bad day; nothing is ever cut because the budget ran out.
