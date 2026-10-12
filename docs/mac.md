# bodh for Mac

The engine in a menu bar app: download, drag into Applications, open; no terminal, no Python.
Everything the app shows comes from the engine inside it, the same package `pipx install bodh`
gives; the command line stays one click away.

## Install

From the release page: open `bodh-<version>-arm64.dmg` (Apple silicon) or `-x86_64.dmg`
(Intel), drag bodh into Applications, open it. Or, with Homebrew, `brew install --cask
abhigyan-j-singh/tap/bodh` once the tap exists.

The first launch asks for your BazQux sign-in (checked live), a model provider and its key
(checked with one small call, the cost stated), five questions that become the student profile,
your folders (which to judge, which never to cut), and whether it may cut; start in scoring only
and turn cutting on in Settings after a few days. Then the first run judges what arrived in the
last two weeks. From then on the icon sits in the menu bar: quiet when idle, a filled book while
a run judges, a badge when something needs you.

## What lives where

- The project: `~/Library/Application Support/bodh/` (`config.yaml`, `data/`, `profile/`,
  `dashboard/`). Time Machine includes it. `BODH_HOME` set before launching the app points it
  elsewhere.
- The keys: your Keychain, under the service `bodh`, written and read by the engine. The app
  never sees a key after you paste it.
- The app's own log: `~/Library/Application Support/bodh/app.log`; the engine's log in `data/`.

## Settings

Every setting of `config.yaml` is in the window (`CONFIG.md` lists them with the same words):
General (start at login, the menu bar), Account (the BazQux sign-in, the must-read stream),
Folders, Models, Providers and keys (keys, the daily budget, prices), Judging, Cutting,
Learning, Schedule, Notifications, Connections, Privacy and data, Advanced. A change applies
when the field loses focus or Return is pressed, is confirmed by the engine, and shows the
engine's reason when refused. The search field finds any setting. A hand edit of config.yaml
shows in the window within a minute.

## The command line

Settings › Advanced › Install command line tool makes `/usr/local/bin/bodh` point at the
engine inside the app (an administrator prompt). In your shell profile:
`export BODH_HOME="$HOME/Library/Application Support/bodh"`. The CLI and the app share the lock,
so they never run at once; `bodh status` in Terminal agrees with the menu bar.

## Moving from the terminal install

A Mac that already runs bodh from `~/bodh` with the launchd jobs is offered a migration on the
first launch: move the folder into Application Support, import `.env` into the Keychain and
delete it, remove the launchd jobs, start at login. Each line is a checkbox. Settings ›
Advanced › Undo migration moves it back and prints the command that restores the jobs.

## Uninstall

Drag the app to the Bin. What stays, if you want it gone: `~/Library/Application Support/bodh`
(the project), the Keychain items under `bodh` (Keychain Access, search "bodh"), the login item
(System Settings › General › Login Items), `/usr/local/bin/bodh` if the tool was installed. The
Homebrew cask's `zap` removes the same.

## The Homebrew cask

```ruby
cask "bodh" do
  arch arm: "arm64", intel: "x86_64"
  version "1.0.0"
  sha256 arm: "…", intel: "…"
  url "https://github.com/abhigyan-j-singh/bodh/releases/download/v#{version}/bodh-#{version}-#{arch}.dmg"
  name "bodh"
  desc "Keeps your BazQux unread list worth reading"
  homepage "https://github.com/abhigyan-j-singh/bodh"
  auto_updates true
  depends_on macos: ">= :sonoma"
  app "Bodh.app"
  zap trash: ["~/Library/Application Support/bodh", "~/Library/Preferences/in.antriksh.bodh.plist"],
      delete: "/usr/local/bin/bodh"
end
```
