#!/usr/bin/env bash
# Everything for this laptop in one go: install, credentials from FeedBrain, a live check,
# launchd agents in shadow mode, and a first run. Nothing is cut until config.yaml says enforce: true.
set -euo pipefail
cd "$(dirname "$0")"

echo "== 1/4 install"
./install.sh

echo
echo "== 2/4 live check (BazQux login, folders, one call to each model)"
.venv/bin/viveka doctor --live

echo
echo "== 3/4 first run in shadow mode (scores and tags up to 400 articles, cuts nothing)"
.venv/bin/viveka run --shadow || true
.venv/bin/viveka status

echo
echo "== 4/4 launchd agents (every 30 min while logged in; Sunday weekly; monthly report; control room on http://127.0.0.1:8766)"
./install-launchd.sh

cat <<EOF

done. Scores are appearing in BazQux as tags score-90 … score-0 (open a tag with the unread filter on).
Next, when you are ready:
  .venv/bin/viveka bankruptcy --older-than 14          clear the old pile (reversible with: viveka undo)
  .venv/bin/viveka prune --dry-run                     see what a prune to 40 would cut
  .venv/bin/viveka replay --archive ../feedfilter/data/feedfilter.db --golden ../evals/golden.jsonl
  edit config.yaml: cut.enforce: true, cut.default_floor: 25   after two or three shadow days
control room: http://127.0.0.1:8766 (status, run, stop, prune, undo, blast, the dashboard underneath); whole picture in the terminal: .venv/bin/viveka status
EOF
