#!/usr/bin/env bash
# A plain DMG: the app and an Applications link, named bodh-<version>-<arch>.dmg, with its
# SHA-256 beside it. create-dmg (brew install create-dmg) gives the nicer window when present;
# hdiutil does the job without it.
set -euo pipefail
app="${1:?the app bundle}"; version="${2:?the version}"; arch="${3:?arm64 or x86_64}"; out_dir="${4:-$(dirname "$app")}"
name="bodh-$version-$arch.dmg"
out="$out_dir/$name"
rm -f "$out"
if command -v create-dmg >/dev/null 2>&1; then
  create-dmg --volname "bodh" --window-size 540 360 --icon-size 128 --icon "Bodh.app" 140 160 --app-drop-link 400 160 --no-internet-enable "$out" "$app"
else
  stage="$(mktemp -d)"
  cp -R "$app" "$stage/"
  ln -s /Applications "$stage/Applications"
  hdiutil create -volname "bodh" -srcfolder "$stage" -ov -format UDZO "$out" >/dev/null
  rm -rf "$stage"
fi
(cd "$out_dir" && shasum -a 256 "$name" > "$name.sha256")
echo "wrote $out and $out.sha256"
