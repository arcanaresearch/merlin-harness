#!/bin/sh
set -eu

repository=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
version=$($repository/.venv/bin/python -c \
  'import sys, tomllib; print(tomllib.load(open(sys.argv[1], "rb"))["project"]["version"])' \
  "$repository/pyproject.toml")
release_name="merlin-${version}-darwin-arm64"
release_channel=${MERLIN_RELEASE_CHANNEL:-stable}
target="$repository/target"
build="$target/release-build"
release_root="$target/releases/$release_name"
archive="$target/releases/$release_name.tar.gz"
assets="$build/playwright"
postgres_version="18.1.0"
postgres_source="$HOME/.pg0/installation/$postgres_version"

if [ "$(uname -s)" != "Darwin" ] || [ "$(uname -m)" != "arm64" ]; then
  echo "release build requires macOS ARM" >&2
  exit 1
fi
if [ "$(sw_vers -productVersion | cut -d. -f1)" != "14" ]; then
  echo "release build currently targets macOS 14" >&2
  exit 1
fi

# PyInstaller copies distribution metadata from the workspace environment.
# Keep it aligned with the manifests so a version bump cannot freeze stale metadata.
uv sync --all-packages
"$repository/scripts/build-wheelhouse-macos-arm.sh"
rm -rf -- "$build" "$release_root"
mkdir -p -- "$build" "$release_root/payload" "$target/releases"

# Reuse a provisioned browser cache for offline release qualification.
# Playwright still checks that the cache contains its required browser revision.
if [ -n "${MERLIN_RELEASE_BROWSER_SOURCE:-}" ]; then
  mkdir -p -- "$assets"
  cp -R "$MERLIN_RELEASE_BROWSER_SOURCE/." "$assets/"
fi
PLAYWRIGHT_BROWSERS_PATH="$assets" \
  "$repository/.venv/bin/playwright" install chromium

if [ ! -x "$postgres_source/bin/postgres" ]; then
  echo "pg0 PostgreSQL $postgres_version is not installed at $postgres_source" >&2
  echo "start Merlin once to provision it before building a release" >&2
  exit 1
fi

cd "$repository"
uv run --with 'pyinstaller>=6.16,<7' pyinstaller \
  --clean --noconfirm \
  --distpath "$build/dist" \
  --workpath "$build/work" \
  packaging/merlin_macos.spec
"$repository/.venv/bin/python" "$repository/scripts/verify_encrypted_packs.py" \
  --collector-root "$build/dist/merlin-runtime"

cp -R "$build/dist/merlin-runtime" "$release_root/payload/merlin-runtime"
cp -R "$assets" "$release_root/payload/playwright"
mkdir -p "$release_root/payload/postgresql"
cp -R "$postgres_source" "$release_root/payload/postgresql/$postgres_version"
cp "$repository/dist/wheelhouse-manifest.json" "$release_root/manifest.json"
cp "$repository/LICENSE" "$release_root/LICENSE"
mkdir -p "$release_root/notices"
cp "$repository/docs/THIRD-PARTY.md" "$release_root/notices/THIRD-PARTY.md"
uv run --with 'pyinstaller>=6.16,<7' python "$repository/scripts/collect-release-notices.py" \
  --wheelhouse "$repository/dist/wheelhouse" --output "$release_root/notices" \
  --browser-cache "$assets"
printf '%s\n' "$release_channel" > "$release_root/release-channel"
cp "$repository/packaging/merlin-launcher" "$release_root/merlin"
chmod 0755 "$release_root/merlin"
"$repository/.venv/bin/python" "$repository/scripts/release_contract.py" stamp \
  --manifest "$release_root/manifest.json" --version "$version" \
  --name "$release_name" --platform darwin-arm64 --channel "$release_channel"
"$repository/.venv/bin/python" "$repository/scripts/release_contract.py" verify \
  --manifest "$release_root/manifest.json" --version "$version" \
  --name "$release_name" --platform darwin-arm64 --channel "$release_channel" \
  --binary "$release_root/merlin"

if [ -n "${MERLIN_CODESIGN_IDENTITY:-}" ]; then
  codesign --force --deep --options runtime --timestamp \
    --sign "$MERLIN_CODESIGN_IDENTITY" "$release_root/payload/merlin-runtime/merlin-runtime"
fi

rm -f -- "$archive" "$archive.sha256"
archive_stage=$(mktemp -d "${TMPDIR:-/tmp}/merlin-release-archive.XXXXXX")
trap 'rm -rf -- "$archive_stage"' EXIT HUP INT TERM
cp -R "$release_root" "$archive_stage/$release_name"
find "$archive_stage/$release_name" -name .DS_Store -delete
COPYFILE_DISABLE=1 tar -C "$archive_stage" -czf "$archive" "$release_name"
rm -rf -- "$archive_stage"
trap - EXIT HUP INT TERM
"$repository/.venv/bin/python" "$repository/scripts/release_contract.py" verify \
  --manifest "$release_root/manifest.json" --version "$version" \
  --name "$release_name" --platform darwin-arm64 --channel "$release_channel" \
  --binary "$release_root/merlin" --archive "$archive"
shasum -a 256 "$archive" > "$archive.sha256"
echo "$archive"
