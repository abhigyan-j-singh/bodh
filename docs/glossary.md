# Glossary

One word per thing, the same word in the command line, the configuration, the console, the
code and these documents. Where a name changed in October 2026 the old one is given in
brackets; the old names keep working for one release and are listed in `CHANGELOG.md`.

| Term | Meaning | Where it appears |
| --- | --- | --- |
| bodh | The project: a teacher for your BazQux unread list. बोध, understanding (was `triage`) | the command, the package, the docs |
| the student | The person the articles are judged for: you | prompts, docs |
| the judge | The model that reads every article once and writes its judgment (was the teacher; "the teacher" stays in prose for what the judge does) | `models.judge`, `judge.*`, the console |
| the judgment | What the judge writes for one article: five sub-scores, a lesson, a concept, a kind, flags, a confidence | `bodh why`, the database |
| the score | The five sub-scores composed into one number from 0 to 100, with the caps and bonuses applied | everywhere a number is shown |
| the sub-scores | quality, learning, fit, breadth, relevance, each 0 to 100 | `judge.weights`, `bodh why` |
| the lesson | One line the judge writes: what the student takes from the article | the list, the dashboard |
| the concept | One to three words naming the field the article teaches; the unit of the student's record | the dashboard, the prune |
| a band | One of the five score ranges (90 and up, 70, 50, 30, 0), written to BazQux as a tag (was a bucket) | `tags.bands`, `score-90` … `score-0` |
| the review | A second reading of a close call by a stronger model, with the full text; it can only save an article (was the second look) | `models.reviewer`, `review.*`, the console |
| the reviewer | The model that gives the review | `models.reviewer` |
| the profiler | The model that rewrites the student profile every Sunday (was the learner) | `models.profiler` |
| the student profile | Two files in `profile/`: `about.md`, which the student writes (was `seed.md`), and `learned.md`, which the profiler writes (was `learner.md`, "the learner model") | `profile/`, `bodh profile` |
| about | `profile/about.md`: who the student is, in fifteen lines | `profile/about.md` |
| learned | `profile/learned.md`: what the profiler has learned about the student, with its version history | `profile/learned.md`, `bodh profile show` |
| a steer | A dated line the student adds to the learned profile: more or less of a concept (was a nudge) | `bodh steer`, the `more` and `less` tags |
| the cut-off | The score under which an article is cut by the automatic floor (was the floor) | `autocut.cutoff`, `folders.*.cutoff` |
| autocut | The automatic cut every run makes below the cut-off when it is enabled (was enforcing, the floor) | `autocut.enabled`, `bodh autocut on\|off` |
| scoring-only mode | Autocut off: every article is judged and tagged, nothing is cut (was shadow mode) | the console, `bodh run --shadow` |
| a cut | Marking an article read in BazQux and tagging it `cut`; every cut is a logged run and can be undone | `bodh undo`, the dashboard |
| a prune | The student's own cut: keep the best N, cut a share, or cut below a score, with a cap per concept | `bodh prune`, `prune.*`, the `prune` tag |
| clear-old | Marking everything older than N days read, stars and protections exempt (was bankruptcy) | `bodh clear-old --days N` |
| the catch-up | One deliberate pass over everything pending, as fast as the API allows, usually with the quick judge (was the blast) | `bodh catch-up`, `catch_up.*` |
| the quick judge | A faster, cheaper model used for a catch-up; the judge re-judges what it rates highly (was the stand-in) | `catch_up.model`, `catch_up.rejudge_at` |
| a run | One pass of the timer's job: feedback, new articles, judgments, tags, autocut, the dashboard, the heartbeat | `bodh run`, the console |
| with the model | Articles whose judgment is on its way through the Batches API (was in flight) | `bodh progress`, the console |
| waiting for review | Close calls held unread until their review lands on the next run (was held) | `bodh progress`, the console |
| pending | Unread articles no judge has read yet | `bodh progress` |
| unread | What is unread in BazQux now (was the pile) | the console, `bodh status` |
| a rescue | Marking a cut article unread again, or tagging it `rescue`; it is never cut again by any path (was `wrong`, which is still accepted) | the `rescue` tag, `bodh mark ID rescue` |
| a verdict | The student's opinion of an article as a tag: a grade (`great`, `good`, `meh`, `bad`), a level (`basic`, `heavy`) or a steer (`more`, `less`) | the tags, `bodh mark`, the dashboard |
| protected | Never cut: starred, in the must-read stream, in a protected folder, rescued | `protect.must_read_stream`, `folders.*.protected` |
| the must-read stream | A BazQux smart stream whose unread members are never cut | `protect.must_read_stream` |
| new ground | An article of decent quality on a concept the student has not read; a share of what autocut leaves is kept for it (was exploration) | `autocut.new_ground_share` |
| safety | The limits that stop a broken judge from cutting: the share limit, the rescue rate, the mark-all-read limit (was breakers) | `safety.*`, `bodh safety` |
| the safety limit | A run whose autocut would cut nearly everything newly judged stops before any write and cuts nothing until a person resets it (was the breaker) | `safety.max_cut_share`, `bodh safety reset` |
| the console | The local page with the levers and the dashboard, kept alive by the schedule (was the control room, `bodh panel`) | `bodh console`, `bodh.console` |
| the dashboard | The page the console shows and `bodh run` exports: today, the list, the verdicts, the learning, the feeds, the money | `dashboard/index.html` |
| the schedule | The operating system's jobs that run bodh: launchd on a Mac (systemd, Task Scheduler and `bodh serve` elsewhere, from Phase 9) | `bodh schedule`, `bodh.run` |
| the heartbeat | A ping to healthchecks.io after every run, so silence raises an alert | `HEALTHCHECK_URL` |
| the budget | The day's spending cap on model calls; judging pauses when it is reached, nothing is cut because of it | `models.daily_budget_usd` |
| the home angle | A phrase from the configuration; an article with an angle on it gets a bonus | `judge.home_angle`, `judge.home_bonus` |
| the reader | The feed service the articles come from and the cuts go to: BazQux today | `readers/bazqux.py` |
