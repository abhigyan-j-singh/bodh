#!/usr/bin/env bash
# Build the bundled engine the Mac app ships: a python-build-standalone interpreter with bodh
# and keyring installed into it, trimmed, with a `bodh` launcher beside it. Reproducible: the
# interpreter release and its SHA-256 are pinned here, the wheel is built from this checkout (or
# given), the dependencies come from a constraints file.
#
#   scripts/build_engine.sh                         # this machine's architecture, into mac/build/engine-<arch>
#   scripts/build_engine.sh --arch x86_64           # the Intel engine (runs under Rosetta on Apple silicon)
#   scripts/build_engine.sh --arch arm64 --out DIR  # where to put it
#   scripts/build_engine.sh --wheel dist/bodh-1.0.0-py3-none-any.whl   # a released wheel instead of the checkout
#   scripts/build_engine.sh --platform linux        # the Linux build, to test the script where there is no Mac
#
# The result: DIR/python (the interpreter and site-packages), DIR/bodh (a launcher), DIR/engine.json
# (what went in). Every Mach-O inside is signed later by mac/scripts/sign.sh; this script signs nothing.
set -euo pipefail

PBS_TAG="20261009"            # python-build-standalone release (astral-sh/python-build-standalone)
PY_VERSION="3.12.15"          # the CPython it carries
FLAVOUR="install_only_stripped"  # the smallest build: no debug symbols, no static library
sha_for() {                   # the release's SHA256SUMS, pinned; a mismatch stops the build
  case "$1" in
    aarch64-apple-darwin)      echo "233f8e15255b3e1d1fd47ea5dd5230dfed803f371138e896e54d98c60ae19f6f" ;;
    x86_64-apple-darwin)       echo "fb3187d95334813b88940db0f1ca57b84cedfd552739c3a5771c3ea0e49988c9" ;;
    x86_64-unknown-linux-gnu)  echo "ffa8f85f1b56e88b687f08d743d817015524b86a9365fd921243132b80f1a36d" ;;
    *) echo ""; ;;
  esac
}

here="$(cd "$(dirname "$0")/.." && pwd)"
arch=""; platform=""; out=""; wheel=""; constraints="$here/mac/engine/constraints.txt"; cache="${BODH_ENGINE_CACHE:-$HOME/.cache/bodh-engine}"
while [ $# -gt 0 ]; do
  case "$1" in
    --arch) arch="$2"; shift 2 ;;
    --platform) platform="$2"; shift 2 ;;
    --out) out="$2"; shift 2 ;;
    --wheel) wheel="$2"; shift 2 ;;
    --constraints) constraints="$2"; shift 2 ;;
    --cache) cache="$2"; shift 2 ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done

host_os="$(uname -s)"; host_arch="$(uname -m)"
[ -n "$platform" ] || platform=$([ "$host_os" = "Darwin" ] && echo darwin || echo linux)
[ -n "$arch" ] || arch="$host_arch"
case "$arch" in arm64|aarch64) arch_name="arm64"; triple_arch="aarch64" ;; x86_64|amd64) arch_name="x86_64"; triple_arch="x86_64" ;; *) echo "unknown arch $arch (arm64 or x86_64)" >&2; exit 2 ;; esac
case "$platform" in darwin) triple="$triple_arch-apple-darwin" ;; linux) triple="$triple_arch-unknown-linux-gnu" ;; *) echo "unknown platform $platform (darwin or linux)" >&2; exit 2 ;; esac
sha="$(sha_for "$triple")"
[ -n "$sha" ] || { echo "no pinned interpreter for $triple" >&2; exit 2; }
[ -n "$out" ] || out="$here/mac/build/engine-$arch_name"

say() { printf '%s\n' "$*"; }
checksum() { if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi; }

# 1. the interpreter, cached and verified
name="cpython-${PY_VERSION}+${PBS_TAG}-${triple}-${FLAVOUR}.tar.gz"
url="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}/${name}"
mkdir -p "$cache"
tarball="$cache/$name"
if [ ! -f "$tarball" ] || [ "$(checksum "$tarball")" != "$sha" ]; then
  say "downloading $name"
  curl -fsSL --retry 3 -o "$tarball.part" "$url"
  mv "$tarball.part" "$tarball"
fi
got="$(checksum "$tarball")"
[ "$got" = "$sha" ] || { echo "checksum mismatch for $name: $got, expected $sha" >&2; rm -f "$tarball"; exit 1; }

# 2. unpack
rm -rf "$out"; mkdir -p "$out"
tar -xzf "$tarball" -C "$out"
python="$out/python/bin/python3"
[ -x "$python" ] || { echo "no interpreter at $python after unpacking" >&2; exit 1; }

# 3. the wheel: from this checkout unless one was given
if [ -z "$wheel" ]; then
  host_python="${BODH_HOST_PYTHON:-python3}"
  mkdir -p "$out/wheel"
  say "building the wheel from $here"
  "$host_python" -m pip wheel "$here" --no-deps -w "$out/wheel" -q
  wheel="$(ls "$out"/wheel/bodh-*.whl | head -1)"
fi
[ -f "$wheel" ] || { echo "no wheel at $wheel" >&2; exit 1; }

# 4. install into the bundled interpreter (it must run here: the same architecture, or Rosetta)
if ! "$python" -c 'import sys; print(sys.version)' >/dev/null 2>&1; then
  echo "the $triple interpreter does not run on this $host_os $host_arch machine; build it on a Mac of that architecture, or with Rosetta installed" >&2
  exit 1
fi
constraint_args=()
[ -f "$constraints" ] && constraint_args=(-c "$constraints")
say "installing bodh and keyring into the engine"
"$python" -m pip install -q --no-cache-dir --no-compile --disable-pip-version-check --only-binary=:all: "${constraint_args[@]}" "$wheel" "keyring>=24"
"$python" -m pip uninstall -q -y pip setuptools wheel 2>/dev/null || true

# 5. trim what no run needs (tests, IDLE, Tk, headers, the static library), then compile the
#    bytecode once (unchecked hashes: no mtime checks, nothing written at run time; the app runs
#    the engine with PYTHONDONTWRITEBYTECODE=1 so a signed bundle is never modified)
site="$out/python/lib/python3.12"
rm -rf "$site/test" "$site/idlelib" "$site/tkinter" "$site/turtledemo" "$site/ensurepip" "$site/pydoc_data" \
       "$site/lib2to3" "$site/config-3.12-"* "$out/python/include" "$out/python/share" "$out/python/lib/pkgconfig" \
       "$out/python/lib/libpython3.12.a" "$site/lib-dynload/_tkinter"* "$out/python/lib/tcl"* "$out/python/lib/tk"* \
       "$out/python/lib/libtcl"* "$out/python/lib/libtk"* "$out/python/lib/itcl"* "$out/python/lib/Tix"* "$out/python/lib/thread"*
find "$out/python/lib/python3.12/site-packages" -type d -name "tests" -prune -exec rm -rf {} + 2>/dev/null || true
# bin/ keeps the interpreter and its python3 link alone: pip, idle, pydoc and python3.12-config
# (a shell script `python3*` used to let through) have no run to serve
find "$out/python/bin" -mindepth 1 ! -name 'python3' ! -name 'python3.12' -delete 2>/dev/null || true
find "$out/python" -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true
"$python" -m compileall -q -j 0 --invalidation-mode unchecked-hash "$site" >/dev/null

# 6. the launcher and the record. The app's Install command line tool makes /usr/local/bin/bodh
#    a symlink to the launcher, hence the readlink: the engine is beside the script itself.
cat > "$out/bodh" <<'EOF'
#!/bin/sh
# bodh's command line: the bundled interpreter, the bundled package.
self="$(readlink -f "$0" 2>/dev/null)" || self="$0"
engine="$(cd "$(dirname "$self")" && pwd)"
[ -x "$engine/python/bin/python3" ] || { echo "bodh: no engine beside $self" >&2; exit 1; }
exec "$engine/python/bin/python3" -m bodh.cli "$@"
EOF
chmod +x "$out/bodh"
bodh_version="$("$python" -c 'import bodh; print(bodh.__version__)')"
cat > "$out/engine.json" <<EOF
{"bodh": "$bodh_version", "python": "$PY_VERSION", "pbs": "$PBS_TAG", "triple": "$triple", "arch": "$arch_name", "platform": "$platform", "sha256": "$sha"}
EOF

# 7. the check and the figures (nothing may be written into the engine by running it)
PYTHONDONTWRITEBYTECODE=1 "$python" -m bodh.cli version >/dev/null
size="$(du -sh "$out" | cut -f1)"
machos=0
if command -v file >/dev/null 2>&1; then
  machos="$(find "$out/python" -type f \( -name '*.so' -o -name '*.dylib' -o -perm -u+x \) -exec file {} + 2>/dev/null | grep -c 'Mach-O' || true)"
fi
say "engine built: bodh $bodh_version on CPython $PY_VERSION ($triple) at $out, $size, $machos Mach-O files to sign"
