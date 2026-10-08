#!/usr/bin/env bash
# Install or update triage in place. Safe to run again and again; this is also the update command.
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
.venv/bin/python -m pip install -q -e ".[dev]"

[ -f config.yaml ] || { cp config.example.yaml config.yaml; echo "created config.yaml (shadow mode on; edit folders, target, floor)"; }
if [ ! -f .env ]; then
  if [ -f ../.env ] && grep -q '^BAZQUX_EMAIL=' ../.env; then
    # reuse FeedBrain's credentials, nothing else
    { grep -E '^(BAZQUX_EMAIL|BAZQUX_PASSWORD|ANTHROPIC_API_KEY)=' ../.env; echo 'HEALTHCHECK_URL='; } > .env
    chmod 600 .env
    echo "created .env from FeedBrain's credentials (HEALTHCHECK_URL left empty; add one from healthchecks.io when ready)"
  else
    cp .env.example .env; chmod 600 .env
    echo "created .env: fill in BAZQUX_EMAIL, BAZQUX_PASSWORD, ANTHROPIC_API_KEY, HEALTHCHECK_URL"
  fi
fi
mkdir -p data dashboard
mkdir -p "$HOME/Library/Logs/triage" 2>/dev/null || true

if [ ! -d profile ]; then
  cp -R profile.example profile
  echo "created profile/ from profile.example: write profile/seed.md, fifteen lines about the student"
fi
if [ -d profile ] && [ ! -d profile/.git ] && command -v git >/dev/null 2>&1; then
  (cd profile && git init -q && git add -A && git commit -q -m "seed and initial learner model") || true
fi

echo
.venv/bin/triage doctor
