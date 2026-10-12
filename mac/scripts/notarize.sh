#!/usr/bin/env bash
# Notarize a signed app or DMG and staple the ticket. The credentials live in a Keychain
# profile made once with:
#   xcrun notarytool store-credentials bodh --apple-id YOU@EXAMPLE.COM --team-id TEAMID --password APP-SPECIFIC-PASSWORD
# Usage: NOTARY_PROFILE=bodh scripts/notarize.sh Bodh.app   (or the .dmg)
set -euo pipefail
target="${1:?the app or the dmg}"
profile="${NOTARY_PROFILE:-bodh}"
work="$(mktemp -d)"
if [ -d "$target" ]; then
  ditto -c -k --keepParent "$target" "$work/upload.zip"
  upload="$work/upload.zip"
else
  upload="$target"
fi
echo "submitting $target to the notary service (profile $profile)"
xcrun notarytool submit "$upload" --keychain-profile "$profile" --wait
xcrun stapler staple "$target"
xcrun stapler validate "$target"
rm -rf "$work"
echo "notarized and stapled: $target"
