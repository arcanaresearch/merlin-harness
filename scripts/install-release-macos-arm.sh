#!/bin/sh
set -eu

repository=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
version=$($repository/.venv/bin/python -c \
  'from importlib.metadata import version; print(version("merlin-tui"))')
release_name="merlin-${version}-darwin-arm64"
archive="$repository/target/releases/$release_name.tar.gz"
install_root="${MERLIN_INSTALL_ROOT:-$HOME/.local/share/merlin}"
bin_root="${MERLIN_BIN_ROOT:-$HOME/.local/bin}"

test -f "$archive"
mkdir -p -- "$install_root" "$bin_root"

# Never replace a runtime bundle while its daemon may still have that bundle
# mapped.  In addition to making the active version ambiguous, repeated local
# installs can strand old one-file/one-dir runtime processes and exhaust the
# operator's descriptor limit.  A failed stop is a failed install.
if [ -x "$bin_root/merlin" ]; then
  "$bin_root/merlin" stop >/dev/null
fi

stage=$(mktemp -d "$install_root/.install.XXXXXX")
trap 'rm -rf -- "$stage"' EXIT HUP INT TERM
tar -C "$stage" -xzf "$archive"
rm -rf -- "$install_root/$release_name"
mv "$stage/$release_name" "$install_root/$release_name"
ln -sfn "$install_root/$release_name/merlin" "$bin_root/merlin"
"$bin_root/merlin" --version
echo "installed: $bin_root/merlin"
