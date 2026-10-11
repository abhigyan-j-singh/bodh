# Changelog

Every change a user could notice, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/) from 1.0.0 on.

## [Unreleased]

### Changed

- The project is **bodh** (बोध: understanding, insight); it was `triage`. The package, the command, the pip name,
  the launchd labels (`bodh.run`, `bodh.weekly`, `bodh.report`, `bodh.console`), the root
  variable (`BODH_HOME`), the database (`data/bodh.db`) and the log folder
  (`~/Library/Logs/bodh`) carry the new name. For one release the old names keep working: the
  `triage` command runs with a note, `TRIAGE_HOME` is honoured with a warning, an existing
  `data/triage.db` is kept as it is, and `./install-launchd.sh` boots out the old
  `com.antriksh.triage*` jobs when it finds them. The log moves to `~/Library/Logs/bodh/`; the
  old folder can be deleted. All of this goes away in the release after 1.0.0. To upgrade an
  install from before the rename: `triage halt`, update the checkout, `./install.sh &&
  ./install-launchd.sh`.
- `config.yaml` is no longer tracked by git: it is yours, like `.env` and `profile/`, and
  `config.example.yaml` is the template. An install that loses its `config.yaml` while it has a
  database is refused by `install.sh` until the file is restored, so a fresh copy of the template
  never silently changes folders, floors or timezone.
- The two scoring rules that were one person's are configuration. `scoring.home_angle` (a short
  phrase; empty turns it off) and `scoring.home_bonus` (10) replace the fixed India angle; the
  `funding` flag now means a funding round outside the student's own field, as the seed describes
  it. Old flag names in the database are read as the new ones.
- `timezone` defaults to the machine's own zone (`$TZ`, else `/etc/localtime`, else UTC); it is
  no longer `Asia/Kolkata`. An empty `timezone:` means the same; `bodh doctor` says which zone is
  in effect and whether it came from `config.yaml` or the computer.
- With no `BODH_HOME` and no `config.yaml` above the working directory, `~/bodh` is tried last,
  so a checkout at the default place works from any folder.
- `cut.enforce` defaults to false: a new install scores and tags but cuts nothing until the
  owner turns the floor on. Every command but `version` refuses to run without a `config.yaml`
  (exit code 2).
- `config.yaml` is validated strictly: a misspelt key, a value out of range, an unknown timezone
  or two tags with one name are refused with the key named.
- The templates speak to a stranger: `profile.example/seed.md` is a worked example for an
  invented student (the shape explained in a comment at the top), `config.example.yaml` has a
  comment on every key and generic folder names, `.env.example` says where each value comes from.

### Added

- `bodh safety [status|reset]`: the breaker, which now persists between runs (the floor and the
  tag-requested prune wait, `bodh prune` refuses, until a person resets it), and the articles
  the teacher gave up on after three empty answers.
- `bodh prune --yes` for scripts and the control room; the interactive prune shows the cut,
  asks, and cuts only what it showed.
- Database migrations (`kv.schema_version`), so a database from an earlier version opens and
  runs under the new code.
- Rescued articles (`wrong`, or marked unread again after a cut) are never cut again by any path;
  an undone cut is exempt from the automatic floor.
- A log redactor: no secret or auth token reaches the log, a traceback or the dashboard.
- `bodh doctor` exits 1 when a check fails, and reports a seed that is still the worked example;
  `bodh run` and `bodh blast` refuse such a seed before touching the network (exit code 2).
- `contrib/replay_archive.py`: judge a sample of another filter's archive offline before anything
  goes live (the `replay` command it replaces needed one particular archive).
- `CHANGELOG.md` (this file), `LICENSE` (MIT); `pyproject.toml` carries the author, the licence, the
  repository URLs and the classifiers, and reads the version from the package.

### Removed

- `laptop-install.sh`; its steps are the README's install section.
- The `replay` command (see `contrib/replay_archive.py`).
- The `teacher.v1` prompt: it carried one person's rules and the current parser could not read
  its answers; `scoring.prompt_version` is `teacher.v2`.
- `install.sh` no longer reads a sibling project's `.env`.
