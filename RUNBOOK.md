# triage: how to run it, every day

Everything needed to run the system on your own, several times a day, on every new article.
The code lives in `~/triage` (a git repository); the design and the internals are in README.md.
This file is the operator's sheet: the commands, the routine, the knobs, what to do when
something looks wrong.

Conventions: every command below is typed in Terminal on the Mac, in `~/triage`, as
`.venv/bin/triage ...`. To type `triage ...` from anywhere, add two lines to `~/.zshrc` once:

    export TRIAGE_HOME="$HOME/triage"
    alias triage="$HOME/triage/.venv/bin/triage"

(then open a new Terminal window). `TRIAGE_HOME` matters: without it, `triage` run from another
folder looks for `config.yaml` there and, finding none, refuses to do anything (exit code 2).


## 1. What runs by itself

Once `./install-launchd.sh` has been run, launchd does the daily work while you are logged in:

| Job | When | What |
| --- | --- | --- |
| `com.antriksh.triage` | every 30 minutes | `triage run`: one full pass over everything new (section 3) |
| `com.antriksh.triage.weekly` | Sunday 07:00 | `triage weekly`: rewrites the learner model from the week's stars, reads and rescues; writes the weekly note; drift check |
| `com.antriksh.triage.report` | 1st of the month, 07:30 | `triage report`: the feed report with proposals, in `dashboard/` |
| `com.antriksh.triage.panel` | always (kept alive) | the control room at http://127.0.0.1:8766 |

A laptop that is asleep simply misses runs and catches up on wake. Nothing needs to be typed for
new articles to be judged; the rest of this file is for looking, cutting, teaching and fixing.


## 2. The daily routine

Three or four times a day, in this order; two minutes each time.

1. Look: open http://127.0.0.1:8766 (or `triage status`). The top line says idle or what is
   running and its phase; then the mode (shadow or enforcing), the next scheduled run, the
   pile (unread, judged, in flight, pending), today's spend, the last runs.
2. Read in BazQux or NetNewsWire. In BazQux the judgments are tags: `score-90` (assign it),
   `score-70` (worth the hour), `score-50` (reference), `score-30` and `score-0` (skip, junk).
   Open a tag with the unread filter on. Star what was good.
3. Cut, when the unread list is too long:

       triage prune --dry-run        # shows the boundary score and what would go
       triage prune                  # shows the same, asks, then keeps the best 40 (cut.target_unread)
       triage undo                   # if you regret it: everything from the last cut comes back unread

   `triage prune --keep 60`, `--fraction 0.3` (cut the worst 30%), `--below 65` (cut everything
   under 65) are the other shapes; `--yes` skips the question (a script, the control room).
   Starred articles, the `Must read` stream, protected folders and anything you rescued never
   move. What an undo brought back is left alone by the automatic floor from then on (a prune
   may still take it).
4. Teach, in passing (section 5): star or tag `great` what was best, `good`, `meh` or `bad` the
   rest; `wrong` on a cut article you wanted; `more` or `less` to steer by an article's concept.

Judgments arrive within an hour or two of an article (the next run submits it, the Batches API
finishes most batches within the hour, the run after that collects them; half price). When you
want the pile judged right now:

    triage run --sync                # every model call now, full price, up to 400 articles
    triage progress --watch          # live: judged, in flight, pending, rate per minute, ETA


## 3. What one run does (`triage run`)

In order, with the phase shown on the control room and in `triage progress`:

1. talking to BazQux: reads what is unread and starred now; collects your feedback (stars,
   articles you read, the `wrong` and `prune` commands, your verdict tags: grades, levels, steers).
2. fetching new articles: everything unread the database has not seen, with its text.
3. collecting finished batches: the judgments submitted last time come back and are stored
   (in every mode: a batch submitted before `models.batch` was turned off is still collected).
4. submitting a batch: up to 1,200 new articles go to the teacher (Sonnet 5.5), 20 per call.
   Articles older than 14 days (`scoring.max_age_days`) and the folders with `score: false` are
   never sent; an article the model has answered about three times with no judgment is left
   alone (`triage safety` counts them, `triage safety reset` tries again).
5. tagging in BazQux: every judged article gets its `score-…` tag.
6. applying the floor, only when `cut.enforce: true`: unread articles under their folder's floor
   are marked read and tagged `cut`, at most 500 a run; a close call (within 10 points of the
   floor, with more text than the teacher saw) gets an Opus 5.5 second look first and is held one
   run; 20% of what the floor leaves is reserved for new ground; a `prune` tag seen in step 1
   prunes to the target as well. In shadow mode (`cut.enforce: false`) nothing is cut, ever.
7. rendering the dashboard; heartbeat ping.

A run takes a lock: a second `triage run` while one is going says so and exits. A run starts no
model call after 14 minutes and ends itself after 15; the next one picks up what is left. If
BazQux cannot serve the `Must read` stream, the run judges and tags but cuts nothing.


## 4. Modes: shadow and enforcing

You are in **shadow mode** (`cut.enforce: false`): everything is scored and tagged, nothing is
cut automatically; only `triage prune` and `triage bankruptcy` cut, and only when you run them.

When the scores have looked right for a few days (open `score-30` and `score-0`: nothing you
would have read?), switch the automatic floor on:

    triage enforce on                # edits config.yaml; the next run cuts under the floors
    triage enforce off               # back to shadow

The floors: `cut.default_floor: 35` for every folder, and in `cut.folders` the ones with their
own (`World News & Politics` and `Sports` cut below 70, `Aggregators` below 60). Start with 25
if you want it gentle; raise it when the kept list is still too long. `triage run --shadow` is
one run without cutting even while enforcing.

The breaker: a floor that would cut more than 95% of at least 50 newly judged articles stops
before writing, the run fails, and the floor stays off (every later run says so in its notes,
`triage status` says BREAKER TRIPPED) until you have looked at a few scores and typed
`triage safety reset`.


## 5. Teaching the teacher: the tags

Your opinion of an article is a tag on it in BazQux (web or phone; NetNewsWire syncs stars only).
Three small sets: a grade, the level, a steer. Use one word or all three; a word you do not use
costs nothing. The run reads them every 30 minutes.

| Tag | Put it on | It says |
| --- | --- | --- |
| `great` | the best article you read | the best kind; more like this. Counts like a star: the Sunday rewrite is rejected if it would push any `great` article under the floor |
| `good` | an article worth the time | a positive example |
| `meh` | one that was not worth it | a negative example |
| `bad` | junk the teacher kept | the strongest negative; a `bad` on a high score is what the Sunday rewrite learns from most |
| `basic` | an article too elementary for you | introductions to this concept no longer fit; the teacher scores their `fit` low from the next run |
| `heavy` | one too dense or specialist for you now | gentler entries to this concept fit better |
| `more` | any article | more of its concept: a dated nudge goes into the learner model and the teacher reads it from the next run |
| `less` | any article | less of its concept, the same way |

Rules: one grade per article (add another to change your mind; when two are on, the harsher one
wins; removing a tag alone retracts nothing). The tags stay on the article as your own record, so
the `great` tag in BazQux is your best-of list. Each is recorded once.

Two tags are commands rather than opinions, and the run removes them when done:

| Tag | It does |
| --- | --- |
| `wrong` | on a cut article you wanted: it comes back unread, counts as a rescue and as the strongest positive, and is never cut again by any path; many rescues in a week lower the floor to 25. On an article that was never cut it is the positive only |
| `prune` | on any article: the next enforcing run prunes to the target (the request is kept until one does) |

Without a tag: a star counts like `great` and protects the article from every cut; an article you
read (it goes read in BazQux without a cut) is a mild positive, inferred within 7 days of arrival.
From the terminal: `triage want "liquid cooling economics"` and `triage meh "celebrity"` are
`more` and `less` by phrase; `triage label ID star|junk` is `great`/`bad` by id; `triage why ID`
shows an article's score, sub-scores, lesson, concept, which model judged it, and what you
recorded on it. `ID` is the BazQux item id, long form (`tag:google.com,2005:reader/item/…`) or the
number. The two biggest levers are not tags: `profile/seed.md`, fifteen lines on who the student
is, and `profile/learner.md`, which Opus rewrites on Sundays (`triage profile show`, `triage
profile revert`).


## 5a. How it learns, for you specifically

The teacher never trains; what changes is what it is told about you, in three layers, each on
its own clock.

1. **Every call, the concept lists.** The message with each batch of articles opens with lists
   drawn from your record of the last 90 days: concepts you loved (stars, `great`), concepts you
   read or marked `good` or rescued, concepts you marked `bad` or `meh` or dismissed, and the
   concepts where the level missed (`basic`, `heavy`), each with a count. The teacher uses them
   for three of the five sub-scores: `fit` (your level, from what you finish and what you call
   basic or heavy), `breadth` (new ground is rewarded against what you already read), and
   `relevance`. This layer moves the moment a run has collected the tag.
2. **From the next run, the nudges.** `more`, `less`, `triage want`, `triage meh` write a dated
   line into the learner model ("the student wants more on liquid cooling economics"), which sits
   in the teacher's prompt from the next run on.
3. **Every Sunday, the learner model.** Opus reads the seed, the current learner model and the
   week's evidence: every positive, negative, level and steer, each with the score the teacher
   gave, the lesson and the concept. It rewrites the model's five sections: what you have been
   learning, what you are moving past, the concepts covered, two or three expansion topics for
   the coming week (articles on them get +10), and notes to the teacher on how to judge for you
   now. The disagreements are the richest lines: a `bad` on an 85 or a `great` on a 40 says what
   the teacher has been getting wrong for you, and the notes carry the correction.

Guardrails, so a week of odd reading cannot wreck it: the rewrite may move no topic more than one
band a week; it may not contradict the seed; it is rejected if it is over 400 words; and it is
rejected by the eval gate if, with the new model in place, any article you starred, marked `great`
or rescued would score under the floor. Fewer than five positives in a week and the model is kept
as it is. Every version is kept (`triage profile revert`); the dashboard shows the week's diff.

Two more loops run beside the model. Rescues (`wrong`, or marking a cut article unread) above 5%
of a week's cuts (twenty cuts at least; bankruptcy and undone runs do not count) lower the floor
to 25 until they fall. And 20% of what the floor would leave is
reserved for new ground: articles of decent quality on a concept not in your record, so the
teacher keeps showing you fields you have not read yet.

What to expect: the concept lists act on the next run; the nudges within the hour; the learner
model after the first Sunday with five or more positives. The dashboard's "Your verdicts" panel
sets your grades against the teacher's bands and lists where you disagree most; when the
disagreements shrink week by week, it is learning you.


## 6. Big piles

| Situation | Command |
| --- | --- |
| thousands pending and you want them judged now | `triage blast` (synchronous, 8 calls at once, 1,000 a round, full price; Ctrl-C and run again to continue) |
| the same, cheap and fastest | `triage blast --model claude-haiku-5-5` (20x cheaper; a stand-in: the teacher re-judges what it rates 50 or more on the following runs) |
| the pile includes articles older than 14 days | add `--all-ages` |
| today's budget would stop it | add `--budget 20` |
| only ten minutes now | add `--max-seconds 600` |
| the old pile is not worth judging | `triage bankruptcy --older-than 14 --dry-run`, then without `--dry-run`: everything older is marked read and tagged `cut` (stars and protected folders exempt; `triage undo` reverses it) |


## 7. Stopping and restarting

| Want | Command |
| --- | --- |
| stop the run, prune or blast in progress | `triage stop` (the same as Ctrl-C in its terminal; a cut half-written to BazQux is finished by the next run) |
| nothing fires for a while | `triage schedule pause` (`--job run` for one job; without it the control room pauses too); `triage schedule resume` |
| a run now, through launchd | `triage schedule kick` (refused while a run holds the lock) |
| stop everything, control room included | `triage halt`; `triage schedule resume` brings the jobs back. It ends the commands of this checkout started by their full path (launchd's jobs, the alias above); one started as `.venv/bin/triage …` from inside the folder is not found, Ctrl-C that one |
| after a code update, or after moving the folder | `./install.sh && ./install-launchd.sh` |
| the control room in the foreground | `triage panel` (Ctrl-C ends it; launchd restarts its own copy) |


## 8. Watching

| Where | What |
| --- | --- |
| http://127.0.0.1:8766 | the control room: status, buttons for every command above, their output streamed, the log, the dashboard underneath. It answers only to a Host of 127.0.0.1, localhost or a `*.ts.net` name |
| `triage status` | the same in the terminal |
| `triage safety` | the breaker, and the articles the teacher gave up on |
| `triage progress [--watch]` | the teacher's progress: judged, in flight, pending, by folder, batches, spend, cache hits, cost per 1,000, the log |
| `dashboard/index.html` | the dashboard: flow, calibration, near misses, feeds, learnings, knowledge map, learner model, health (no log: the page may be published) |
| `~/Library/Logs/triage/triage.log` | the run log (`tail -f`); `launchd.*.log`, `panel.err.log` next to it |
| `data/panel.log` | why the control room could not start, if ever |
| healthchecks.io | a check with period 30 minutes and grace 30 minutes, its ping URL in `.env` as `HEALTHCHECK_URL`: an hour of silence (laptop off, network down, launchd not firing) raises an alert; a failed run pings `/fail` with the error |
| phone | `tailscale serve --bg 8766`, then https://<your-mac>.<tailnet>.ts.net is the control room |


## 9. The knobs (config.yaml)

The file is read at the start of every run; edit it with any editor, no restart needed (the
control room picks it up on its next refresh). A misspelt key, a value out of range or an
unknown timezone is refused with the key named, by every command. Current values in brackets.

**Top level**

| Key | Means |
| --- | --- |
| `timezone` [Asia/Kolkata] | the day boundary for the budget and the counts |
| `data_dir`, `profile_dir`, `dashboard_dir` [./data, ./profile, ./dashboard] | where the database, your profile and the dashboard live |
| `obsidian_note` [empty] | a path for the weekly note, e.g. `~/Obsidian/Main/Triage/Weekly.md` |

**models**

| Key | Means |
| --- | --- |
| `teacher` [claude-sonnet-5-5] | reads every article; the judgment |
| `second_look` [claude-opus-5-5] | re-reads close calls with the full text; can only save an article |
| `learner` [claude-opus-5-5] | rewrites `profile/learner.md` on Sundays |
| `batch` [true] | judgments through the Batches API: half price, within an hour or two. `false` = every call at once, full price |
| `batch_discount` [0.5] | what the API charges for batches, for the spend figures |
| `cache_ttl` [1h] | how long the teacher's cached prompt prefix stays warm between runs (`5m` or `1h`) |
| `daily_budget_usd` [8.0] | the day's cap: scoring pauses when passed; nothing is cut because of it. A normal day is under a dollar |
| `est_usd_per_article` [0.001] | the budget check before a batch is submitted |
| `prices_per_mtok` | input, output, cache-read price per million tokens for each model; update when prices change. A model with no price is charged as the most expensive known one, with a warning |

**scoring**

| Key | Means |
| --- | --- |
| `prompt_version` [teacher.v2] | which prompt in `src/triage/prompts/` (a `./prompts/` folder next to config.yaml overrides); changing it re-judges everything unread |
| `weights` [0.30 quality, 0.30 learning, 0.15 fit, 0.15 breadth, 0.10 relevance] | how the five sub-scores make the score |
| `expansion_bonus` [10] | points for an article on one of this week's expansion topics |
| `batch_size` [20] | articles per teacher call |
| `workers` [4] | model calls in flight at once in synchronous runs (`--sync`, `--workers` overrides) |
| `max_per_run` [400] | articles judged per synchronous run |
| `batch_max_per_run` [1200] | articles submitted per batch run |
| `max_age_days` [14] | unread articles older than this are never judged; `bankruptcy` is for them; `blast --all-ages` includes them once |
| `rejudge_min_score` [50] | the teacher re-judges what a stand-in (Haiku) rated at least this; `null` turns it off |
| `opening_chars` [1200] | the text the teacher sees on the first pass |
| `second_look_window` [10] | a floor candidate within this many points of the floor gets the second look |
| `second_look_chars` [6000] | the text the second look sees |
| `second_look_max_per_run` [50] | synchronous runs: second looks per run at most |

**cut**

| Key | Means |
| --- | --- |
| `enforce` [false] | the automatic floor on or off (`triage enforce on/off`) |
| `target_unread` [40] | what `triage prune` keeps by default |
| `default_floor` [35] | the floor for folders without their own |
| `max_cuts_per_run` [500] | the most one run may cut; a broken prompt costs one undo |
| `concept_cap` [0.25] | in a prune, no concept may hold more than this share of the survivors |
| `exploration_share` [0.20] | share of what the floor leaves kept for new ground (good quality, concept unseen in 90 days) |
| `must_read_stream` [Must read] | the BazQux smart stream whose members are never cut |
| `folders` | per folder: `auto_cut_below: N` (its own floor), `protected: true` (never cut), `score: false` (never judged). Folder names must match BazQux exactly; `triage doctor --live` warns about ones that do not |

**tags** [score-90 … score-0, cut, wrong, prune, great, good, meh, bad, basic, heavy, more, less]:
the tag names written to and read from BazQux; change them only before the first run.

**breakers**

| Key | Means |
| --- | --- |
| `max_cut_share` [0.95], `min_new_for_breaker` [50] | a run that would cut more than 95% of at least 50 newly judged articles stops before writing: the prompt or model has probably broken. The floor stays off until `triage safety reset` |
| `rescue_rate_week` [0.05], `lowered_floor` [25] | rescues above 5% of a week's cuts lower the floor to 25 until they fall |
| `bulk_dismiss_threshold` [30] | this many kept articles going read in one run counts as mark-all-as-read, not as 30 reads. More than 80% of the pool going read at once is believed only when the next run sees the same (an empty answer from BazQux looks like that) |

**feedback**: `concept_window_days` [90], how far back stars, reads and rescues count as what the
student knows.

**.env** (never committed, `chmod 600`): `BAZQUX_EMAIL`, `BAZQUX_PASSWORD`, `ANTHROPIC_API_KEY`,
`HEALTHCHECK_URL` (optional). `triage doctor` says which are set without showing them.

**profile/**: `seed.md` is yours; `learner.md` is Opus's, rewritten on Sundays, versioned in
`profile/`'s own git history and in the database (`triage profile revert`).


## 10. When something looks wrong

| Symptom | Do |
| --- | --- |
| anything | `triage doctor` (configuration, prompts, credentials present, database; exit code 1 when something is wrong), then `triage doctor --live` (logs in to BazQux, one call to each model, a few cents) |
| the run notes say the breaker tripped, or `triage status` says BREAKER TRIPPED | look at a few newly judged articles (`triage why ID`); if the scores are sane, `triage safety reset`; if not, the prompt or the model changed: check `models.teacher`, the prompt file, `triage doctor --live` |
| `triage safety` says articles were given up on | the model answered three times with nothing for them (a prompt that does not fit the model, usually); fix that, then `triage safety reset` |
| the control room does not answer | `triage status` shows "control room: not answering · last: …"; `tail -5 data/panel.log`; `./install-launchd.sh` reinstalls the job and waits for it; `triage panel` runs it in the foreground with the error on screen |
| "already running" when you run something | something holds the lock: `triage status` names it; `triage stop` ends it |
| a launchd job shows exit code 126 or "Operation not permitted" | the checkout is under Documents, Desktop or Downloads, or in a path with spaces; keep it at `~/triage` (`./install-launchd.sh` refuses the others) |
| scores stopped appearing | `triage progress`: is the budget reached (spend near 8.00)? are batches in flight for more than an hour (the API is slow; they are collected on the next run, given up after 26 h and resubmitted)? `tail -50 ~/Library/Logs/triage/triage.log` |
| the floor cut something good | tag it `wrong` (it comes back); `triage undo` for the whole last cut; lower the folder's floor in `cut.folders` or `cut.default_floor` |
| the teacher's taste drifted | `triage profile show`; `triage profile revert`; edit `profile/seed.md` |
| a model id was rejected ("model call rejected") | `models.*` in config.yaml, or the key in `.env`; `triage doctor --live` |
| after `git pull` or any code change | `./install.sh` (also runs doctor), then `./install-launchd.sh`; `.venv/bin/pytest -q` runs the tests |


## 11. What it costs

Sonnet 5.5 through batches with the cached prefix: about $0.65 per 1,000 judged, so under a
dollar for a 1,300-article day; `triage progress` shows the measured figure and the cache hit
share. A full blast of 30,000 articles: $35 to $40 with Sonnet synchronous (full price), about $2.50
with the Haiku stand-in. `models.daily_budget_usd` caps a bad day; nothing is ever cut because the budget ran out.
