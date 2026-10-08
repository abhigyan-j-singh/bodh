#!/usr/bin/env bash
# Install the three launchd jobs for this checkout.
#   ./install-launchd.sh            laptop: LaunchAgents in ~/Library/LaunchAgents, no sudo, run while logged in
#   ./install-launchd.sh --daemon   always-on Mac mini: LaunchDaemons in /Library/LaunchDaemons (asks for sudo)
# Run again after moving the folder or changing the plists; it replaces what is there.
set -euo pipefail
cd "$(dirname "$0")"
TRIAGE_DIR="$(pwd)"
MODE="${1:-agent}"
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

for job in com.antriksh.triage com.antriksh.triage.weekly com.antriksh.triage.report; do
  if [ "$MODE" = "--daemon" ]; then
    tmp="$(mktemp)"; render "launchd/$job.plist" "$tmp"
    sudo launchctl bootout "system/$job" 2>/dev/null || true
    sudo cp "$tmp" "/Library/LaunchDaemons/$job.plist"
    sudo chown root:wheel "/Library/LaunchDaemons/$job.plist"; sudo chmod 644 "/Library/LaunchDaemons/$job.plist"
    sudo launchctl bootstrap system "/Library/LaunchDaemons/$job.plist"
    rm -f "$tmp"
  else
    mkdir -p "$HOME/Library/LaunchAgents"
    launchctl bootout "gui/$(id -u)/$job" 2>/dev/null || true
    render "launchd/$job.plist" "$HOME/Library/LaunchAgents/$job.plist"
    launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/$job.plist"
  fi
  echo "installed $job"
done

if [ "$MODE" = "--daemon" ]; then
  echo "jobs run as daemons; see them with: sudo launchctl print system/com.antriksh.triage"
else
  echo "jobs run as agents while you are logged in; see them with: launchctl print gui/$(id -u)/com.antriksh.triage"
fi
echo "logs: ~/Library/Logs/triage/   stop: $( [ "$MODE" = "--daemon" ] && echo sudo ) launchctl bootout $( [ "$MODE" = "--daemon" ] && echo system || echo gui/$(id -u) )/com.antriksh.triage"
