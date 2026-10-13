#!/usr/bin/env bash
# Assemble Bodh.app from the SwiftPM build, the engine and the resources:
#   Contents/MacOS/Bodh            the executable (swift build -c release)
#   Contents/Resources/engine/     the engine (scripts/build_engine.sh) with its launcher,
#                                  where py2app and Briefcase put an interpreter and its files:
#                                  codesign takes everything under Frameworks or MacOS for code
#                                  (an executable script wants a signature it cannot carry, a
#                                  folder with a dot in its name, lib/python3.12, is taken for
#                                  a bundle), while Resources are sealed as data and the Mach-O
#                                  files inside are signed one by one (scripts/sign.sh)
#   Contents/Resources/AppIcon.icns, Info.plist with the version and the bundle id filled in
# Usage: scripts/make_app.sh --binary PATH --engine DIR --out Bodh.app --version 1.0.0 --build 12 --bundle-id in.antriksh.bodh
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
binary=""; engine=""; out="$here/build/Bodh.app"; version="0.0.0"; build="1"; bundle_id="in.antriksh.bodh"
while [ $# -gt 0 ]; do
  case "$1" in
    --binary) binary="$2"; shift 2 ;;
    --engine) engine="$2"; shift 2 ;;
    --out) out="$2"; shift 2 ;;
    --version) version="$2"; shift 2 ;;
    --build) build="$2"; shift 2 ;;
    --bundle-id) bundle_id="$2"; shift 2 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done
[ -x "$binary" ] || { echo "no executable at $binary (swift build first)" >&2; exit 1; }
[ -x "$engine/python/bin/python3" ] || { echo "no engine at $engine (make engine first)" >&2; exit 1; }

rm -rf "$out"
mkdir -p "$out/Contents/MacOS" "$out/Contents/Resources"
cp "$binary" "$out/Contents/MacOS/Bodh"
sed -e "s/__BUNDLE_ID__/$bundle_id/" -e "s/__VERSION__/$version/" -e "s/__BUILD__/$build/" "$here/Resources/Info.plist" > "$out/Contents/Info.plist"
echo "APPL????" > "$out/Contents/PkgInfo"
if [ -f "$here/build/AppIcon.icns" ]; then
  cp "$here/build/AppIcon.icns" "$out/Contents/Resources/AppIcon.icns"
else
  echo "note: no build/AppIcon.icns (make icon); the app will show the generic icon" >&2
fi
# the engine, copied whole; symlinks inside python-build-standalone are kept as symlinks
cp -R "$engine" "$out/Contents/Resources/engine"
rm -rf "$out/Contents/Resources/engine/wheel"
# every file the app serves itself must be readable; nothing in a bundle should be writable by the group
chmod -R go-w "$out"
size="$(du -sh "$out" | cut -f1)"
echo "assembled $out ($size): version $version build $build, $bundle_id"
