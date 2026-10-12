# Every setting

Generated from the configuration models by `scripts/gen_config_md.py`; do not edit by hand.
`config.yaml` holds these keys (the example file shows every default with a comment); the Mac
app's Settings window shows the same ones, pane by pane, and `bodh settings` reads and writes
them from the terminal: `bodh settings` lists every value with its source, `bodh settings set
autocut.cutoff 40` changes one with the file's comments kept, `bodh settings schema --json`
prints what the app draws. A key that is not here is a typo and is refused.

## Account

The reader you sign in to, and the stream that protects articles.

| Key | Default | Range | What it does |
| --- | --- | --- | --- |
| `protect.must_read_stream` | `"Must read"` |  | The BazQux smart stream whose unread members are never cut (README: how to create it). |

## Folders

Your BazQux folders: which are judged, which are never cut, and each one's cut-off.

| Key | Default | Range | What it does |
| --- | --- | --- | --- |
| `folders` | none |  | Your BazQux folders by their exact names, each with its own policy; a folder not listed is judged and cut at the default cut-off. |
| `folders.<name>.protected` | `false` |  | Never cut: articles in this folder stay unread whatever their score. |
| `folders.<name>.cutoff` | empty | 0 to 100, or empty | This folder's own cut-off; empty means the default (autocut.cutoff). |
| `folders.<name>.judge` | `true` |  | Sent to the judge at all. Off for folders whose articles are never reading material (comics, say): never judged, never cut. |

## Models

Which model fills each role, and how much each run gives it.

| Key | Default | Range | What it does |
| --- | --- | --- | --- |
| `models.judge` | `"claude-sonnet-5-5"` |  | Reads every article once: the judgment. Changing it re-judges every unread article another model rated catch_up.rejudge_at or more; `bodh models compare` tries a model first. |
| `models.reviewer` | `"claude-opus-5-5"` |  | Re-reads close calls with the full text; it can only save an article, never cut one. |
| `models.profiler` | `"claude-opus-5-5"` |  | Rewrites the learned profile on Sundays. |
| `models.batch` | `true` |  | Message Batches API where the provider has one: half price, judgments arrive on the next run. Off: every call at once, at full price. |
| `models.batch_discount` | `0.5` | 0 to 1 | What the API charges for a batch, as a share of the full price; for the spend figures. (Advanced.) |
| `models.cache_ttl` | `"1h"` | `5m` or `1h` | How long the judge's cached prompt prefix stays warm between runs. An hour costs more to write and less to read: a batch may take the hour, and the next run reads the same prefix. (Advanced.) |
| `judge.articles_per_call` | `20` | 1 to 100 | Articles in one judge call. (Advanced.) Unit: articles. |
| `judge.text_chars` | `1200` | 100 or more | The opening of each article the judge sees on the first pass. (Advanced.) Unit: characters. |
| `review.chars` | `6000` | 100 or more | The text the reviewer sees; a review happens only when the article has more than the opening. (Advanced.) Unit: characters. |
| `review.max_per_run` | `50` | 0 or more | Synchronous runs: at most this many reviews per run, so a backlog of close calls cannot eat the run's fifteen minutes. (Advanced.) Unit: reviews. |
| `catch_up.model` | `"claude-haiku-5-5"` |  | The quick judge `bodh catch-up` uses unless told otherwise (provider/model-id). |
| `catch_up.rejudge_at` | `50.0` | 0 to 100, or empty | What the quick judge rated at least this, the judge reads again; empty: never. What it rated below keeps the quick judge's verdict: a catch-up's junk is final. Unit: points. |
| `limits.per_run_sync` | `400` | 0 or more | Synchronous runs: articles judged per run. (Advanced.) Unit: articles. |
| `limits.per_run_batch` | `1200` | 0 or more | Batch runs: articles submitted per run. (Advanced.) Unit: articles. |
| `limits.parallel_calls` | `4` | 1 to 64 | Synchronous runs: judge calls and reviews running at once. (Advanced.) Unit: calls. |

## Providers and keys

The daily budget and the price of every model.

| Key | Default | Range | What it does |
| --- | --- | --- | --- |
| `models.daily_budget_usd` | `8.0` | 0 or more | Judging pauses when a day's spend passes this; nothing is cut because of it. Unit: USD a day. |
| `models.est_usd_per_article` | `0.001` | 0 or more | The budget check before a batch is submitted, before its usage is known. (Advanced.) Unit: USD. |
| `models.prices_per_mtok` | `{claude-haiku-4-5: [1.0, 5.0, 0.1], claude-haiku-5-5: [0.1, 0.5, 0.01], claude-opus-5-5: [4.0, 20.0, 0.2], claude-sonnet-5-5: [2.0, 10.0, 0.1]}` |  | USD per million tokens: input, output and cache read, per model (a bare id for Anthropic, provider/model-id for the rest). A model not listed is charged as the dearest one here, with a warning. |

## Judging

How a judgment becomes a score.

| Key | Default | Range | What it does |
| --- | --- | --- | --- |
| `judge.prompt_version` | `"teacher.v2"` |  | The judge's prompt in the package's prompts folder (a prompts/ folder next to config.yaml overrides it). Changing it re-judges everything unread. (Advanced.) |
| `judge.weights` | `{breadth: 0.15, fit: 0.15, learning: 0.3, quality: 0.3, relevance: 0.1}` |  | How the five sub-scores become the score; they must sum to 1. |
| `judge.expansion_bonus` | `10` | 0 to 100 | Points for an article on one of this week's expansion topics (from the learned profile). Unit: points. |
| `judge.home_angle` | empty |  | Your home ground ("India", "Brazil", "the Pacific Northwest"): an article with that angle gets the home bonus. Empty turns it off. |
| `judge.home_bonus` | `10` | 0 to 100 | The points a home-angle article gets. Unit: points. |

## Cutting

Scoring-only or cutting, the cut-off, the prune, and the safety limits.

| Key | Default | Range | What it does |
| --- | --- | --- | --- |
| `review.window` | `10` | 0 to 100 | An autocut candidate within this many points of the cut-off is reviewed; 0 turns the review off. Unit: points. |
| `limits.max_age_days` | `14` | 0 or more, or empty | Unread articles older than this are never judged; `bodh clear-old` is for them. Empty: no age limit. Unit: days. |
| `autocut.enabled` | `false` |  | Off is scoring-only mode: judge and tag, cut nothing. On: every run marks read what scores under its folder's cut-off, within the safety limits. |
| `autocut.cutoff` | `35` | 0 to 100 | The cut-off for folders without their own. Unit: points. |
| `autocut.max_per_run` | `500` | 0 or more | Most cuts in one run: a broken prompt costs one undo, not a library. Unit: articles. |
| `autocut.new_ground_share` | `0.2` | 0 to 1 | The share of what autocut leaves that is kept for new ground (a concept you have not read). |
| `prune.keep` | `40` | 0 or more | `bodh prune` with no arguments keeps this many. Unit: articles. |
| `prune.max_share_per_concept` | `0.25` | 0 to 1 | No concept may hold more than this share of the survivors. |
| `safety.max_cut_share` | `0.95` | 0 to 1 | A run that would cut more than this share of at least min_judged newly judged articles stops before writing, and autocut stays off until `bodh safety reset`. |
| `safety.min_judged` | `50` | 1 or more | The fewest newly judged articles the share above is measured on. Unit: articles. |
| `safety.rescue_rate_week` | `0.05` | 0 to 1 | Rescues above this share of a week's cuts (20 cuts at least) lower the cut-off to the lowered value until they fall. |
| `safety.lowered_cutoff` | `25` | 0 to 100 | The cut-off while the rescue rate is high. Unit: points. |
| `safety.mark_all_read_threshold` | `30` | 1 or more | This many kept articles going read in one run is mark-all-as-read, not that many reads. (Advanced.) Unit: articles. |

## Learning

How far back your reading counts, and the tags bodh writes and reads.

| Key | Default | Range | What it does |
| --- | --- | --- | --- |
| `tags.bands` | `{'0': score-0, '30': score-30, '50': score-50, '70': score-70, '90': score-90}` |  | Score band to tag name: the tag the judge writes for a score at or above each threshold. |
| `tags.cut` | `"cut"` |  | On every article bodh marked read; mark it unread anywhere to rescue it. |
| `tags.rescue` | `"rescue"` |  | On a cut article you wanted: back to unread, never cut again (the old name `wrong` is still read). |
| `tags.prune` | `"prune"` |  | On any article: the next run with autocut on prunes to prune.keep. |
| `tags.great` | `"great"` |  | The best kind: more like this; the Sunday rewrite may not lose it. |
| `tags.good` | `"good"` |  | Worth the time. |
| `tags.meh` | `"meh"` |  | Not worth it. |
| `tags.bad` | `"bad"` |  | Junk; the judge was wrong to keep it. |
| `tags.basic` | `"basic"` |  | Too elementary for you. |
| `tags.heavy` | `"heavy"` |  | Too dense or too specialist for you now. |
| `tags.more` | `"more"` |  | More of this article's concept, from the next run. |
| `tags.less` | `"less"` |  | Less of it. |
| `profile.concept_window_days` | `90` | 1 or more | How far back stars, reads and verdicts count as what you know. Unit: days. |

## Schedule

The clock the day runs by.

| Key | Default | Range | What it does |
| --- | --- | --- | --- |
| `timezone` | empty |  | IANA name (Europe/Lisbon, say): the day boundary for the budget and the daily counts. Empty: this computer's zone. |

## Connections

Where else the weekly note goes.

| Key | Default | Range | What it does |
| --- | --- | --- | --- |
| `obsidian_note` | empty |  | Also write the weekly note here (a Markdown file in an Obsidian vault, say: "~/Obsidian/Main/Bodh/Weekly.md"); empty writes it to the dashboard folder only. |

## Privacy and data

Where the data lives.

| Key | Default | Range | What it does |
| --- | --- | --- | --- |
| `data_dir` | `"data"` |  | The database, the run logs, progress.json and the lock; relative to config.yaml. (Advanced.) |
| `profile_dir` | `"profile"` |  | about.md (yours) and learned.md (rewritten weekly); its own git repository. (Advanced.) |
| `dashboard_dir` | `"dashboard"` |  | The exported dashboard page, the weekly note and the feed report. (Advanced.) |
