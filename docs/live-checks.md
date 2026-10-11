# Live checks: what the owner runs after an update

The suite runs offline; these are the few things only a person with the real account can
check, each with what to expect. Run them after `./install.sh && ./install-launchd.sh`, in the
checkout, with the virtualenv's `bodh` (or the `bodh` alias). Each costs cents at most.

| Check | Expected |
| --- | --- |
| `bodh doctor` | one line per check, `backups: N, newest bodh-<date>.db` (or "the run makes one a day"), and `ok` last; exit code 0 |
| `bodh doctor --values` | every setting with `config.yaml` or `default` beside it; the ones you changed say `config.yaml` |
| `bodh doctor --live` | `bazqux login: ok`, the unread count, the feeds and folders, no e-mail address anywhere, one line per model ending `ok`, the spend of today, then `ok`; exit code 0. A folder in `config.yaml` that BazQux does not have is a problem line and exit code 1 |
| `bodh run --sync --limit 20` | one summary line: `unread N · new … · judged 20 …`, no traceback; `bodh progress` then shows 20 more judged and the band tags appear in BazQux |
| `bodh catch-up --model <catch_up.model> --max-seconds 60 --yes` | the preview line (`… articles pending; about $… with …`), a progress line that moves, then `judged …` and `time limit` in the report if a minute was not enough; Ctrl-C and run again to continue |
| `bodh backup` | `backup written: data/backups/bodh-<today>.db (N kept)`; the file opens with `sqlite3` |
| `bodh rebuild --dry-run` | one line of counts: the band-tagged, cut-tagged and starred articles BazQux holds, how many are unknown here (a few at most on a live database), `would write 0 scores` or thereabouts |
| `bodh eval --cap 0.50` | `60 articles; about $0.0x with <judge>`, a progress count, then the agreement: exact and within one band, the per-band table, the largest disagreements; put the two figures and the date in README's Development section |
| `bodh keys` | one line per provider, `set` or `not set`, no value and no prefix anywhere |
| `bodh models` | the four roles with their prices and capabilities (`batch, schema, cache` for Anthropic), the providers with keys, the price table, the budget line |
| `bodh models compare --with <provider/model> --sample 5 --yes` (after `bodh keys set <provider>` and a price in `config.yaml`) | a progress count to 10, then `compared 5 of 5 articles`, the agreement lines, the cost per 1,000 and seconds per article for both, and `nothing stored changed`; a few cents |
| `bodh status` | the console up, the four `bodh.*` jobs, the last run's summary |
| the console, once each: run, stop, schedule pause and resume, autocut off and on, prune (dry run), undo, clear-old (dry run), catch-up (stop it), weekly, report, doctor, dashboard | each button streams its output and ends with the command's exit code; `autocut off` shows in `bodh doctor --values` at once |
| `tail -f ~/Library/Logs/bodh/bodh.log` during a run | one line per phase, the summary line at the end, no secret anywhere |

The first time after the rename, the switch-day sequence comes first: `.venv/bin/triage halt`,
then `./install.sh && ./install-launchd.sh`, then `launchctl print gui/$(id -u)/com.antriksh.triage`
must fail (the old jobs are gone) and `triage version` prints the note and `bodh 1.0.0.dev0`.
