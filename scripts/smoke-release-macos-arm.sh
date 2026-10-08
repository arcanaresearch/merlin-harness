#!/bin/sh
set -eu

repository=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
version=$($repository/.venv/bin/python -c \
  'from importlib.metadata import version; print(version("merlin-tui"))')
release_name="merlin-${version}-darwin-arm64"
archive="$repository/target/releases/$release_name.tar.gz"
smoke_root=$(mktemp -d "${TMPDIR:-/tmp}/merlin-release-smoke.XXXXXX")
trap 'rm -rf -- "$smoke_root"' EXIT HUP INT TERM

test -f "$archive"
tar -C "$smoke_root" -xzf "$archive"
binary="$smoke_root/$release_name/merlin"
test -x "$binary"
export HOME="$smoke_root/home"
export MERLIN_PROVIDER_BASE_URL="http://127.0.0.1:9/v1"
export MERLIN_PROVIDER_MODEL="release-smoke"
"$binary" --version
MERLIN_INSTALL_NO_LAUNCH=1 "$binary"
installed="$HOME/.local/bin/merlin"
test -x "$installed"
"$binary" update
"$installed" start
"$installed" status | grep '^running$'
"$installed" restart
"$installed" stop
mkdir -p "$HOME/.merlin/probe"
"$installed" uninstall --keep-data
test ! -e "$installed"
test -d "$HOME/.merlin/probe"
MERLIN_INSTALL_NO_LAUNCH=1 "$binary"
installed="$HOME/.local/bin/merlin"
"$installed" uninstall --purge
test ! -e "$installed"
test ! -e "$HOME/.merlin"
echo "release smoke passed: $archive"
