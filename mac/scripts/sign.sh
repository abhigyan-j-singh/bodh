#!/usr/bin/env bash
# Sign the app for distribution: every Mach-O inside the engine first (the interpreter, the
# shared library, every .so), each with the hardened runtime and the engine's entitlements,
# then the app with its own; timestamps on; then verify the way Gatekeeper will. The engine
# sits under Contents/Resources, which the app's seal covers as data, so its Mach-O files are
# signed and verified here one by one: the notary service checks each of them.
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
engine="$app/Contents/Resources/engine"
[ -d "$engine" ] || { echo "no engine at $engine (scripts/make_app.sh puts it there)" >&2; exit 1; }

machos=()
while IFS= read -r -d '' file; do
  file "$file" | grep -q 'Mach-O' && machos+=("$file")
done < <(find "$engine" -type f \( -name '*.so' -o -name '*.dylib' -o -perm -u+x \) -print0)
[ "${#machos[@]}" -gt 0 ] || { echo "no Mach-O files in $engine: not an engine built for a Mac" >&2; exit 1; }

echo "signing the engine's ${#machos[@]} Mach-O files with $identity"
for file in "${machos[@]}"; do
  codesign "${flags[@]}" --sign "$identity" --entitlements "$engine_entitlements" "$file"
done

echo "signing the app"
codesign "${flags[@]}" --sign "$identity" --entitlements "$here/Resources/bodh.entitlements" "$app"
codesign --verify --deep --strict --verbose=2 "$app"
for file in "${machos[@]}"; do
  codesign --verify --strict "$file" || { echo "the engine's $file does not verify" >&2; exit 1; }
done
echo "the engine's ${#machos[@]} Mach-O files verify"
if [ "$identity" != "-" ]; then
  spctl --assess --type execute --verbose=2 "$app" || echo "spctl: not accepted yet (expected before notarization)"
fi
echo "signed $app"
