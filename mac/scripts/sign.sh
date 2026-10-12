#!/usr/bin/env bash
# Sign the app for distribution: every Mach-O inside the engine first (the interpreter, the
# shared library, every .so), each with the hardened runtime and the engine's entitlements,
# then the app with its own; timestamps on; then verify the way Gatekeeper will.
#   IDENTITY="Developer ID Application: Name (TEAMID)" scripts/sign.sh Bodh.app [strict]
# Without IDENTITY the app is signed ad hoc (runs on this Mac only; fine for development).
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
app="${1:?the app bundle}"
flavour="${2:-default}"
identity="${IDENTITY:--}"
engine_entitlements="$here/Resources/engine.entitlements"
[ "$flavour" = "strict" ] && engine_entitlements="$here/Resources/engine-strict.entitlements"
flags=(--force --options runtime --timestamp)
[ "$identity" = "-" ] && flags=(--force)
engine="$app/Contents/Frameworks/engine"

echo "signing the engine's Mach-O files with $identity"
count=0
while IFS= read -r -d '' file; do
  if file "$file" | grep -q 'Mach-O'; then
    codesign "${flags[@]}" --sign "$identity" --entitlements "$engine_entitlements" "$file"
    count=$((count + 1))
  fi
done < <(find "$engine" -type f \( -name '*.so' -o -name '*.dylib' -o -perm -u+x \) -print0)
echo "signed $count Mach-O files in the engine"

echo "signing the app"
codesign "${flags[@]}" --sign "$identity" --entitlements "$here/Resources/bodh.entitlements" "$app"
codesign --verify --deep --strict --verbose=2 "$app"
if [ "$identity" != "-" ]; then
  spctl --assess --type execute --verbose=2 "$app" || echo "spctl: not accepted yet (expected before notarization)"
fi
echo "signed $app"
