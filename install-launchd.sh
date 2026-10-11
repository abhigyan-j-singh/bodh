#!/usr/bin/env bash
# Install the four launchd jobs for this checkout: the run (every 30 minutes), the Sunday learner
# rewrite, the monthly report, and the control room (http://127.0.0.1:8766, kept alive).
#   ./install-launchd.sh            laptop: LaunchAgents in ~/Library/LaunchAgents, no sudo, run while logged in
#   ./install-launchd.sh --daemon   always-on Mac mini: LaunchDaemons in /Library/LaunchDaemons (asks for sudo)
# Run again after moving the folder, updating the code or changing the plists; it replaces what is there.
set -euo pipefail
cd "$(dirname "$0")"
TRIAGE_DIR="$(pwd)"
MODE="${1:-agent}"
PANEL_PORT=8766
case "$MODE" in
  agent|--daemon) ;;
  *) echo "usage: ./install-launchd.sh [--daemon]"; exit 2;;
esac
[ -x .venv/bin/triage ] || { echo "run ./install.sh first"; exit 1; }
mkdir -p "$HOME/Library/Logs/triage"

# launchd jobs cannot read ~/Documents, ~/Desktop or ~/Downloads (macOS privacy protection denies
# them silently: "Operation not permitted", exit 126), and a path with spaces makes pip wrap the
# entry point in /bin/sh, which is denied first. Keep the checkout somewhere plain, ~/triage say.
case "$TRIAGE_DIR" in
  "$HOME/Documents"*|"$HOME/Desktop"*|"$HOME/Downloads"*|*" "*)
    echo "this checkout is at: $TRIAGE_DIR"
    echo "launchd cannot run jobs from Documents, Desktop or Downloads, or from a path with spaces."
    echo "move it first, for example:  mv \"$TRIAGE_DIR\" ~/triage && cd ~/triage && rm -rf .venv && ./install.sh && ./install-launchd.sh"
    exit 1;;
esac

render() {  # $1 = template, $2 = destination
  if [ "$MODE" = "--daemon" ]; then
    USERLINE="    <key>UserName</key><string>$(id -un)</string>"
  else
    USERLINE=""
  fi
  sed -e "s|__TRIAGE_DIR__|$TRIAGE_DIR|g" -e "s|__HOME__|$HOME|g" -e "s|__USERNAME__|$USERLINE|g" "$1" > "$2"
}

if [ "$MODE" = "--daemon" ]; then DOMAIN="system"; SUDO="sudo"; else DOMAIN="gui/$(id -u)"; SUDO=""; fi

for job in com.antriksh.triage com.antriksh.triage.weekly com.antriksh.triage.report com.antriksh.triage.panel; do
  # unload whatever is there under this label first (an older checkout's job, say), then load the new plist
  $SUDO launchctl bootout "$DOMAIN/$job" 2>/dev/null || true
  sleep 1
  if [ "$MODE" = "--daemon" ]; then
    tmp="$(mktemp)"; render "launchd/$job.plist" "$tmp"
    sudo cp "$tmp" "/Library/LaunchDaemons/$job.plist"
    sudo chown root:wheel "/Library/LaunchDaemons/$job.plist"; sudo chmod 644 "/Library/LaunchDaemons/$job.plist"
    rm -f "$tmp"
    if ! sudo launchctl bootstrap system "/Library/LaunchDaemons/$job.plist"; then
      echo "could not load $job as a daemon. See: sudo launchctl print system/$job; the plist is /Library/LaunchDaemons/$job.plist"
      exit 1
    fi
  else
    mkdir -p "$HOME/Library/LaunchAgents"
    render "launchd/$job.plist" "$HOME/Library/LaunchAgents/$job.plist"
    if ! launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/$job.plist"; then
      echo "could not load $job. Usual causes: the checkout moved (run ./install.sh again), or the job is still"
      echo "loaded under an older path (launchctl bootout gui/$(id -u)/$job, then run this again)."
      echo "See: launchctl print gui/$(id -u)/$job; the plist is ~/Library/LaunchAgents/$job.plist"
      exit 1
    fi
  fi
  echo "installed $job"
done

# the control room should answer within a few seconds; if not, say why
echo -n "control room: "
up=""
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
  if curl -s -m 2 "http://127.0.0.1:$PANEL_PORT/api/status" >/dev/null 2>&1; then up=1; break; fi
  sleep 1
done
if [ -n "$up" ]; then
  echo "http://127.0.0.1:$PANEL_PORT"
else
  echo "not answering on port $PANEL_PORT after 15 s"
  printed="$($SUDO launchctl print "$DOMAIN/com.antriksh.triage.panel" 2>/dev/null || true)"
  if [ -n "$printed" ]; then
    echo "  job state: $(echo "$printed" | grep -E 'state =|last exit code|pid =' | tr -s ' ' | tr '\n' ';')"
  else
    echo "  job state: not loaded"
  fi
  for f in "$HOME/Library/Logs/triage/panel.err.log" "$TRIAGE_DIR/data/panel.log"; do
    [ -s "$f" ] && { echo "  $f:"; tail -5 "$f" | sed 's/^/    /'; }
  done
  echo "  try it in the foreground to see the error: .venv/bin/triage panel"
fi

if [ "$MODE" = "--daemon" ]; then
  echo "jobs run as daemons; see them with: sudo launchctl print system/com.antriksh.triage"
else
  echo "jobs run as agents while you are logged in; see them with: launchctl print gui/$(id -u)/com.antriksh.triage"
fi
echo "logs: ~/Library/Logs/triage/   status: .venv/bin/triage status   stop everything: .venv/bin/triage halt"
