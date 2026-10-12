# bodh for Mac

A menu bar app around the bundled engine. The app adds no judging, cutting or learning of its
own: it starts `bodh serve --app` from inside its bundle, keeps it alive, and shows what the
engine says through the engine's own service (`docs/json.md`). Settings are drawn from the
engine's settings schema, so a knob added to the engine appears in the window without a
Swift change.

```
mac/
  Package.swift            SwiftPM: the Bodh app, the BodhEngine library, its tests
  Sources/BodhEngine/      Foundation only: the API client, the models, the event stream, the supervisor
  Sources/Bodh/            the app: menu bar, window, Settings, onboarding, notifications, login item
  Tests/BodhEngineTests/   the models against the engine's fixtures, the SSE parser, the supervisor
  Resources/               Info.plist, the entitlements, the icon
  scripts/                 make_app.sh, sign.sh, notarize.sh, make_dmg.sh, icon.py
  engine/constraints.txt   the dependency versions the bundled engine is built with
  Makefile                 engine, build, app, run, test, icon, sign, notarize, dmg, dist
```

## Build it

On a Mac with Xcode 15 or later (or the Command Line Tools) and Python 3.10+ on the PATH:

```
cd mac
make app          # the engine (downloads a pinned CPython once, ~40 MB), the Swift build, Bodh.app in build/
open build/Bodh.app
```

`make run` starts the app from the build folder with `BODH_ENGINE` pointing at the engine, which
is the quick loop while developing. `make test` runs the BodhEngine tests; `open Package.swift`
opens the package in Xcode, where the `Bodh` scheme runs the app (set `BODH_ENGINE` to
`mac/build/engine-arm64` in the scheme's environment, and `BODH_HOME` to a project folder of
your choosing if you do not want the app's own one under Application Support).

The app is signed ad hoc by `make app`, enough for this Mac. For a copy another Mac will open
without a warning: `make sign IDENTITY="Developer ID Application: Your Name (TEAMID)"`, then
`make notarize` and `make dmg` (`docs/release.md` has the one-time set-up: the certificate, the
notarytool profile).

## How it runs

- On launch the app writes a 32-byte token to `~/Library/Application Support/bodh/token`
  (mode 600), makes the project files there if there are none (`bodh init --skeleton`), and
  starts `python3 -m bodh.cli serve --app --port 0 --token-file … --home …` from
  `Contents/Frameworks/engine`. The engine prints its port on the first line; the app polls
  `/api/health` until it answers, then opens the event stream. A crash restarts the engine
  with backoff (1, 2, 4, 8, 30 s); ten in an hour and it gives up with a Retry button. Quit
  sends SIGTERM, waits five seconds, then SIGKILL; the engine also ends when its stdin closes,
  so a dead app never leaves an orphan.
- The menu bar icon says idle, running, paused or "needs you"; the menu has Run now, Stop,
  Pause, the window, BazQux, Settings, updates, Quit.
- The window shows the engine's own pages, one section at a time (`/?mode=app&section=…`),
  with the token in the request; the pages keep themselves live. The log is native.
- Settings: General, Account, Folders, Models, Providers and keys, Judging, Cutting, Learning,
  Schedule, Notifications, Connections, Privacy and data, Advanced. Every engine setting is one
  `PUT /api/settings`, confirmed by re-reading; a refusal shows under the field in the engine's
  words. Keys go to the Keychain through the engine (`PUT /api/keys/…`), so the same program
  that reads an item created it and the Keychain never asks.
- Onboarding on the first launch: BazQux sign-in (checked live), a provider and key (checked
  live, cents stated), five questions that become the profile, the folders, scoring-only or
  cutting, then the first run.
- A Mac that already runs bodh from the terminal is offered the migration: move `~/bodh`
  into Application Support (`bodh migrate-home`), import `.env` into the Keychain, remove the
  launchd jobs (`bodh schedule uninstall`), start at login. Undo is in Settings › Advanced.

## What is not here yet

Sparkle (Check for updates opens the releases page), the App Store, UI tests, the Appearance
and Keyboard panes, the Connections pane beyond the weekly note path (healthchecks, ntfy,
Pushover, Slack, hooks, MCP, Shortcuts come with the engine's Phase 8), Localizable strings.
`MAC_APP_PLAN.md` is the full plan; this is Stage B with the packaging of Stage C scripted.
