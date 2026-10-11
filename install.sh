#!/usr/bin/env bash
# Install or update viveka in place. Safe to run again and again; this is also the update command.
#   ./install.sh            create/refresh .venv, install the package, copy example files if missing, run doctor
set -euo pipefail
cd "$(dirname "$0")"

# Prefer a modern Python: Homebrew's first, then whatever python3 is on PATH (must be 3.10 or newer).
pick_python() {
  if [ -n "${PYTHON:-}" ]; then echo "$PYTHON"; return; fi
  for c in python3.13 python3.12 python3.11 python3.10 /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
    if command -v "$c" >/dev/null 2>&1; then
      if "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then echo "$c"; return; fi
    fi
  done
  echo "no Python 3.10+ found; on a Mac: brew install python" >&2; exit 1
}
PY="$(pick_python)"
echo "using $("$PY" --version 2>&1) at $(command -v "$PY")"

if [ ! -d .venv ]; then "$PY" -m venv .venv; fi
.venv/bin/python -m pip install -q --upgrade pip
# the package was called triage until 1.0.0: its stale distribution would own .venv/bin/triage (now viveka's alias)
.venv/bin/python -m pip uninstall -y -q triage >/dev/null 2>&1 || true
.venv/bin/python -m pip install -q -e ".[dev]"

if [ ! -f config.yaml ]; then
  if [ -f data/viveka.db ] || [ -f data/triage.db ]; then
    echo "config.yaml is missing but data/ holds a database: this is an existing install, not a new one." >&2
    echo "Restore your config.yaml (a backup, or \`git show <an old commit>:config.yaml > config.yaml\`) and run again;" >&2
    echo "a fresh copy of config.example.yaml would silently change your folders, floors and timezone." >&2
    exit 1
  fi
  cp config.example.yaml config.yaml; echo "created config.yaml (shadow mode on; edit folders, target, floor)"
fi
if [ ! -f .env ]; then
  cp .env.example .env; chmod 600 .env
  echo "created .env: fill in BAZQUX_EMAIL, BAZQUX_PASSWORD, ANTHROPIC_API_KEY and, when you have one, HEALTHCHECK_URL"
fi
mkdir -p data dashboard
mkdir -p "$HOME/Library/Logs/viveka" 2>/dev/null || true

if [ ! -d profile ]; then
  cp -R profile.example profile
  echo "created profile/ from profile.example: replace the worked example in profile/seed.md with fifteen lines about you"
fi
if [ -d profile ] && [ ! -d profile/.git ] && command -v git >/dev/null 2>&1; then
  (cd profile && git init -q && git add -A \
     && git -c user.name=viveka -c user.email=viveka@localhost commit -q -m "seed and initial learner model") || true
fi

echo
if .venv/bin/viveka doctor; then
  echo "next: ./install-launchd.sh schedules it (shadow mode: nothing is cut until cut.enforce is true)"
else
  echo
  echo "not ready yet: fix what doctor lists above (.env, profile/seed.md), then run .venv/bin/viveka doctor again" >&2
  exit 1
fi
