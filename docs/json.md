# The JSON bodh prints

Every command with `--json` prints one object on stdout and nothing else; errors still go to
stderr as one line with the exit code. The first field is always `schema`, an integer that
changes only when a field is removed or its meaning changes (fields are added freely). This is
schema **1**. The same shapes come back from the Python API (`bodh.api`), from the console's
`/api/status`, from `GET /api/dashboard` and from `dashboard/progress.json` and `data.json`, so a
script reads one contract.

Conventions: times are ISO 8601 strings in UTC (`2026-10-11T20:32:44+00:00`) unless the key
says otherwise; money is USD as a number; an article id is the long BazQux form
(`tag:google.com,2005:reader/item/00000000a1b2c3d4`); `null` means not known or not
applicable, never zero; a band is the tag name (`score-70`), a status is `unread`, `read` or
`cut`.

## `bodh version --json`

```json
{"schema": 1, "version": "1.0.0", "newest": null, "channel": "pipx"}
```

`newest` is the newest release on PyPI with `--check`, else null; `channel` is how this bodh
was installed (`pipx`, `uv`, `brew`, `binary`, `checkout`, `pip`).

## `bodh keys --json`

```json
{"schema": 1, "keys": [
  {"provider": "anthropic", "variable": "ANTHROPIC_API_KEY", "set": true, "optional": []},
  {"provider": "openai-compatible", "variable": "OPENAI_COMPATIBLE_BASE_URL", "set": false,
   "optional": ["OPENAI_COMPATIBLE_API_KEY"]}
]}
```

Never a value, never a prefix.

## `bodh run --json`

```json
{"schema": 1, "run": {
  "run_id": 212, "ok": true, "unread_upstream": 1180, "new": 41, "scored": 38, "dupes": 1,
  "pending": 2, "submitted": 0, "inflight": 0, "reviews": 3, "saved": 1, "held": 0,
  "new_ground": 2, "cut_autocut": 12, "cut_prune": 0, "boundary": null,
  "feedback": {"reads": 9, "stars": 1, "rescues": 0, "verdicts": 2},
  "spend_usd": 0.07, "error": "", "notes": [], "batch_mode": true,
  "summary": "unread 1180 · new 41 · judged 38 · ..."
}}
```

The fields of `RunResult` (`bodh/run.py`) plus `summary`, the one line the text output
prints. `notes` carries what the run wants you to know ("daily budget reached: nothing cut this
run", the circuit breaker's line).

## `bodh status --json`

```json
{"schema": 1, "status": {
  "now": "2026-10-11T20:32:44+00:00",
  "console": {"up": false, "url": "http://127.0.0.1:8766", "pid": null, "note": ""},
  "mode": "scoring-only",
  "safety_tripped": null,
  "batch_mode": true,
  "running": null,
  "jobs": {"run": {"label": "bodh.run", "installed": false, "loaded": false, "state": "not installed",
                   "last_exit": null, "pid": null, "mode": "", "plist": null, "interval": null},
           "weekly": {}, "report": {}, "console": {}},
  "launchctl": false,
  "next_run_in": null,
  "config": {"judge": "claude-sonnet-5-5", "reviewer": "claude-opus-5-5", "profiler": "claude-opus-5-5",
             "budget": 8.0, "keep": 40, "cutoff": 35, "max_age_days": 14, "folders": {},
             "root": "/Users/me/bodh", "heartbeat": false},
  "pile": {"unread": 0, "judged": 0, "pending": 0, "inflight": 0, "too_old": 0,
           "stand_in": 0, "rejudge": 0, "low_conf": 0},
  "cuts_pending": {"untagged": 0, "unmarked": 0},
  "spend": {"today": 0.0, "week": 0.0, "all": 0.0, "per_1000": null, "cache_hit": null},
  "today": {"arrived": 0, "judged": 0, "dupes": 0, "second_looks": 0, "cut": 0, "spend": 0.0},
  "rate_per_min": 0.0, "eta_min": null,
  "runs": [], "last_ok": null, "last_error": null, "batches": [], "log": []
}}
```

`mode` is `autocut` or `scoring-only`; `safety_tripped` is the run id that tripped the safety
limit, or null; `running` is null or `{"pid", "kind", "started", "elapsed", "phase"}` for the
run holding the lock; `jobs.*.state` is `running`, `waiting`, `not loaded`, `not installed` or
`unknown` (launchd's answer; on Linux and Windows the jobs read `not installed` and `bodh
serve` or the systemd timers do the work); `next_run_in` is seconds or null; `runs` is the
last fifteen run rows (`id`, `kind`, `started_at`, `finished_at`, `ok`, `unread_upstream`,
`new`, `scored`, `second_looks`, `cut_autocut`, `cut_prune`, `rescues`, `reads`, `spend_usd`,
`boundary`, `error`, `summary`); `last_ok` and `last_error` are rows of the same shape or null;
`batches` are the Batches API batches in flight (`id`, `model`, `status`, `submitted_at`,
`n`); `log` is the last ten log lines (local only: the console never serves them off the
machine). The console's `/api/status` serves the same `status` object (with the console's own
`child` field for the command it is running).

## `bodh progress --json` and `dashboard/progress.json`

```json
{"schema": 1, "progress": {
  "at": "2026-10-11T20:32:51+00:00", "now": "2026-10-11T20:32:51+00:00",
  "batch_mode": true, "max_age_days": 14, "skip_folders": [], "judge": "claude-sonnet-5-5",
  "budget": 8.0, "estimated_models": [],
  "unread": 0, "judged_unread": 0, "pending": 0, "inflight": 0, "inflight_second": 0,
  "too_old": 0, "skipped": 0, "low_conf": 0, "held": 0, "given_up": 0, "stand_in": 0, "rejudge": 0,
  "today": {"arrived": 0, "judged": 0, "dupes": 0, "second_looks": 0, "cut": 0, "spend": 0.0},
  "week": {}, "month": {},
  "all_time": {"judged": 0, "dupes": 0, "cut": 0, "spend": 0.0},
  "cache_hit": null, "per_1000": null, "output_tokens_week": 0,
  "folders": [{"folder": "Essays", "unread": 12, "judged": 12, "good": 4, "cut": 30, "pending": 0,
               "inflight": 0, "cutoff": 35, "scored": true}],
  "batches": [], "runs": [],
  "rate_per_min": 0.0, "judged_2min": 0, "judged_10min": 0,
  "series": [0, 0, 0], "series_bin_minutes": 5, "series_start": "2026-10-11T17:32:51+00:00",
  "running": false, "run_started": null, "phase": "", "eta_min": null,
  "log": []
}}
```

`judge` is the configured judge; `estimated_models` names the models whose spend is an
estimate (their service reports no token counts); `stand_in` counts unread judgments by a
model other than the judge and `rejudge` how many of them the judge will read again;
`folders[].cutoff` is that folder's cut-off (null: never cut) and `scored` whether the folder
is judged at all; `series` is judged-per-bin for the last three hours, newest last; `running`,
`run_started`, `phase`, `rate_per_min` and `eta_min` describe the run in progress. The file
`dashboard/progress.json` is the same object at the top level (`schema` beside the fields, no
`progress` wrapper, no `log`), rewritten by every run as it goes.

## `bodh why ID|URL --json`

```json
{"schema": 1, "article": {
  "id": "tag:google.com,2005:reader/item/00000000a1b2c3d4", "title": "...", "url": "https://...",
  "feed": "Feed A", "folder": "Essays", "judged": true,
  "score": 80.0, "base_score": 80.0, "review_score": null,
  "quality": 80, "learning": 80, "fit": 80, "breadth": 80, "relevance": 80,
  "confidence": "high", "kind": "reporting", "flags": [],
  "lesson": "...", "concept": "reefs", "judged_by": "claude-sonnet-5-5", "prompt_version": "teacher.v2",
  "band": "score-70", "status": "unread", "cut_mode": null, "cut_at": null, "cut_run_id": null,
  "starred": false, "protected": false, "new_ground": false,
  "record": [{"kind": "great", "at": "2026-10-11T19:02:11+00:00"}]
}}
```

When `judged` is false the judgment fields are null. `cut_mode` is `autocut`, `prune` or
`clear-old` once cut; `record` is your marks on the article, oldest first.

## `bodh find "words" --json`

```json
{"schema": 1, "query": "liquid cooling", "unread": false, "hits": [
  {"id": "...", "title": "...", "url": "...", "feed": "Feed A", "folder": "Essays",
   "score": 80.0, "band": "score-70", "status": "unread", "lesson": "..."}
]}
```

Every word must appear in the title or the lesson; best score first, unjudged last; at most
`--limit` (50).

## `bodh list --json`

```json
{"schema": 1, "band": 70.0, "folder": null, "articles": [
  {"id": "...", "title": "...", "url": "...", "feed": "Feed A", "folder": "Essays",
   "score": 88.0, "band": "score-70", "lesson": "...", "concept": "...", "judged_at": "2026-10-11T18:40:02+00:00"}
]}
```

Unread articles at `band` and above, best first, at most `--limit` (50).

## `bodh tags --json`

```json
{"schema": 1, "tags": [
  {"tag": "score-90", "group": "band", "writer": "bodh", "meaning": "the judge's score is 90 or more"},
  {"tag": "great", "group": "grade", "writer": "you", "meaning": "the best kind: more like this; the eval gate protects it"}
]}
```

`group` is `band`, `cut`, `command`, `grade`, `level`, `steer` or `stream`; `writer` says who
puts the tag on the article.

## `GET /api/dashboard` and `dashboard/data.json`

The dashboard document: every number the page shows and nothing it does not, one object the
console serves live, the static export embeds in its page (`<script type="application/json"
id="bodh-document">`) and writes beside it as `data.json`, and the Mac app reads. Numbers
arrive as numbers, times as ISO strings (`as_of`) or as text already in the reader's zone
(`as_of_text`, `age_text`, `started_text`), and every sentence the page prints (the state line,
the leads, the attention, the calibration reading, the empty states) is composed here, so no
renderer invents words. The full schema is `docs/dashboard.schema.json`, generated from the
pydantic model by `scripts/gen_schema.py` (the suite fails when it is stale). Condensed:

```json
{"schema": 1, "as_of": "2026-10-11T15:56:00+00:00", "as_of_text": "16:56", "timezone": "Europe/Lisbon",
 "state": {"running": false, "kind": null, "phase": "", "sentence": "Idle · next run in 22 min",
           "progress_done": null, "progress_total": null, "rate_per_min": null, "eta_min": null,
           "mode": "scoring-only", "mode_label": "Scoring only", "next_run_in_s": 1320.0,
           "attention": null, "levers": ["run", "pause"]},
 "today": {"unread": 130, "target": 40, "hero_text": "130 unread · target 40",
           "lead": "38 judged, 12 cut, 7 read, $0.41 spent.",
           "tiles": [{"key": "judged", "label": "Judged today", "value": 38.0, "text": "38", "delta": 12.0,
                      "delta_text": "+12 vs yesterday", "direction": "up-good",
                      "series": [20.0, 31.0, 28.0, 40.0, 22.0, 26.0, 38.0], "series_label": "judged, last seven days"}]},
 "list": {"lead": "41 unread at 70 and above, best first.", "band": 70, "bands": [90, 70, 50],
          "folders": ["Essays", "News"], "total": 41, "pending": 34,
          "articles": [{"id": "tag:google.com,2005:reader/item/00000000a1b2c3d4", "title": "...",
                        "url": "https://...", "reader_url": "https://bazqux.com/i/00000000a1b2c3d4",
                        "score": 92.3, "band": 90, "lesson": "...", "concept": "...", "feed": "...",
                        "folder": "Essays", "age_text": "2 h", "arrived_at": "2026-10-11T13:40:00+00:00",
                        "judged_at": "2026-10-11T14:02:11+00:00"}],
          "empty_text": "Nothing at 70 or above right now. 34 articles are waiting for the judge; the next run is in 22 min.",
          "verdicts": ["great", "good", "meh", "bad", "basic", "heavy", "more", "less"]},
 "verdicts": {"lead": "...", "grid": [{"band": 90, "label": "90 and up", "counts": {"great": 4, "good": 2, "meh": 0, "bad": 0}}],
              "grid_sentence": "...", "disagreements": [{"id": "...", "title": "...", "url": "...", "grade": "bad", "score": 85.0, "band": 70, "concept": "..."}],
              "disagreements_empty": "...", "calibration": [{"band": 90, "label": "90 and up", "n": 120, "read_share": 0.41, "star_share": 0.05}],
              "calibration_sentence": "...", "calibration_empty": "...",
              "near_misses": [{"id": "...", "title": "...", "url": "...", "score": 34.0, "band": 30, "lesson": "...", "concept": "...", "cut_mode": "autocut"}],
              "near_misses_empty": "..."},
 "learning": {"lead": "...", "concepts": [{"concept": "...", "count": 7, "items": [{"title": "...", "url": "...", "lesson": "..."}], "more": 2}],
              "concepts_more": 0, "concepts_empty": "...", "cloud": [{"concept": "...", "count": 9, "frontier": false}], "cloud_empty": "...",
              "profile": {"version": 12, "date_text": "Sun 5 Oct", "text": "...", "diff": [{"kind": "add", "text": "..."}], "diff_empty": "..."}},
 "feeds": {"lead": "...", "days": 30, "empty_text": "...",
           "rows": [{"feed": "...", "folder": "News", "articles": 212, "judged": 210, "mean_score": 41.2, "mean_band": 30,
                     "cut_share": 0.61, "read_share": 0.04, "star_share": 0.0, "proposal": "unsubscribe: ..."}]},
 "money": {"lead": "...", "days": [{"day": "2026-10-11", "label": "Sat 11 Oct", "usd": 0.41}], "budget_usd": 8.0,
           "chart_title": "Spend is under the budget every day this month.", "per_1000": 0.98, "per_1000_text": "$0.98",
           "cache_hit": 0.71, "cache_hit_text": "71%", "month_usd": 28.62, "month_text": "$28.62", "empty_text": "..."},
 "machinery": {"lead": "...", "batches": [{"id": "msgbatch_...", "purpose": "judge", "articles": 400, "age_text": "12 min"}], "batches_empty": "...",
               "runs": [{"id": 2880, "kind": "run", "started_text": "16:30", "started_at": "2026-10-11T15:30:00+00:00", "ok": true, "summary": "judged 30"}],
               "runs_empty": "...", "log": ["..."]},
 "footer": {"version": "1.0.0", "data_dir": "/Users/you/bodh/data", "docs_url": "https://...", "repo_url": "https://...", "exported_text": null}}
```

`state.levers` names the levers the page may show (`run`, `pause`, `stop`); `state.attention`
is the one banner (`key`, `level` of `warning`, `critical` or `good`, `word`, `text`, an optional
link, `dismissable`) or null. `list.verdicts`, `machinery.log` and `footer.data_dir` are empty
or null in the export (`?mode=export`), which also lists only the bands its rows cover. A share
is a fraction (`0.41`), a band the lower bound of its range (`90`), a tile's `direction` one of
`up-good`, `down-good`, `up-bad`, `down-bad` or empty.

The console's live routes beside it: `GET /api/tick` is one tick, `{"schema": 1, "generation": N,
"as_of": ..., "state": {...}}`, with `today` while a run is in progress and `child` (`running`,
`args`, `code`, `elapsed`, `output`) while the console runs a command of its own; `generation`
moves whenever the page should refetch. `GET /api/events` streams the same object as server-sent
`tick` events, every two seconds while a run is in progress and at least every fifteen. `GET
/list?band=70&offset=50` returns `{"rows": "<li>…", "total": N, "offset": 50, "count": 50}`, the
reading list's next page rendered as the page renders rows. `GET /api/plan?action=prune&keep=40`
returns `{"ok": true, "sentence": "Cut 50 of 130 judged unread articles, keeping the 40 best? …",
"button": "Cut 50"}` for `prune`, `undo`, `clear_old`, `catch_up` and `autocut_on`. `POST
/api/mark` with `{"id": ..., "kind": "great"}` (and the `X-Bodh: console` header, like
`/api/action`) records a verdict in process and answers `{"ok": true, "message": "recorded great
for: …"}`.

## Reading it from Python

```python
from bodh.api import App

app = App()                                  # BODH_HOME, else the config.yaml above the working directory
print(app.status()["pile"])
for row in app.reading_list(band=70):
    print(row.score, row.title, row.lesson)
view = app.why("https://example.org/post")   # an ArticleView dataclass, or None
app.close()
```

The API returns the dataclasses behind these objects (`Hit`, `ListRow`, `ArticleView`,
`TagInfo`, `PrunePlan`, `CatchUpPlan`), each with `to_dict()`; `docs/api.md` (Phase 8)
documents every method.
