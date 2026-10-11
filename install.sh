#!/usr/bin/env bash
# Install or update bodh in place. Safe to run again and again; this is also the update command.
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
# the package was called triage until 1.0.0: its stale distribution would own .venv/bin/triage (now bodh's alias)
.venv/bin/python -m pip uninstall -y -q triage >/dev/null 2>&1 || true
.venv/bin/python -m pip install -q -e ".[dev]"

if [ ! -f config.yaml ]; then
  if [ -f data/bodh.db ] || [ -f data/triage.db ]; then
    echo "config.yaml is missing but data/ holds a database: this is an existing install, not a new one." >&2
    echo "Restore your config.yaml (a backup, or \`git show <an old commit>:config.yaml > config.yaml\`) and run again;" >&2
    echo "a fresh copy of config.example.yaml would silently change your folders, cut-offs and timezone." >&2
    exit 1
  fi
  cp config.example.yaml config.yaml; echo "created config.yaml (scoring-only mode; edit folders, prune.keep, autocut.cutoff)"
fi
if [ ! -f .env ]; then
  cp .env.example .env; chmod 600 .env
  echo "created .env: fill in BAZQUX_EMAIL, BAZQUX_PASSWORD, ANTHROPIC_API_KEY and, when you have one, HEALTHCHECK_URL"
fi
mkdir -p data dashboard
mkdir -p "$HOME/Library/Logs/bodh" 2>/dev/null || true

if [ ! -d profile ]; then
  cp -R profile.example profile
  echo "created profile/ from profile.example: replace the worked example in profile/about.md with fifteen lines about you"
fi
if [ -d profile ] && [ ! -d profile/.git ] && command -v git >/dev/null 2>&1; then
  (cd profile && git init -q && git add -A \
     && git -c user.name=bodh -c user.email=bodh@localhost commit -q -m "about.md and the initial learned profile") || true
fi
# the profile's files were seed.md and learner.md until October 2026: rename them in place, keeping
# their history when profile/ is a git repository (bodh reads the old names until this has run)
rename_profile_file() {  # $1 = old name, $2 = new name
  if [ -f "profile/$1" ] && [ ! -e "profile/$2" ]; then
    if [ -d profile/.git ] && command -v git >/dev/null 2>&1 && (cd profile && git ls-files --error-unmatch "$1" >/dev/null 2>&1); then
      (cd profile && git mv "$1" "$2") || mv "profile/$1" "profile/$2"
      (cd profile && git -c user.name=bodh -c user.email=bodh@localhost commit -q -m "$1 is $2 since October 2026") || true
    else
      mv "profile/$1" "profile/$2"
    fi
    echo "renamed profile/$1 to profile/$2 (its name since October 2026)"
  fi
}
rename_profile_file seed.md about.md
rename_profile_file learner.md learned.md

echo
if .venv/bin/bodh doctor; then
  echo "next: ./install-launchd.sh schedules it (scoring-only mode: nothing is cut until \`bodh autocut on\`)"
else
  echo
  echo "not ready yet: fix what doctor lists above (.env, profile/about.md), then run .venv/bin/bodh doctor again" >&2
  exit 1
fi
