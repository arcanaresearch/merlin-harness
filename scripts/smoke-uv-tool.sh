#!/bin/sh
set -eu

repository=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
wheelhouse=${MERLIN_WHEELHOUSE:-"$repository/dist/wheelhouse"}
smoke_root=$(mktemp -d "${TMPDIR:-/tmp}/merlin-tool-smoke.XXXXXX")
trap 'rm -rf -- "$smoke_root"' EXIT HUP INT TERM

export UV_TOOL_DIR="$smoke_root/tools"
export UV_TOOL_BIN_DIR="$smoke_root/bin"
set -- "$wheelhouse"/merlin_tui-*.whl
if [ "$#" -ne 1 ] || [ ! -f "$1" ]; then
  echo "expected one merlin-tui wheel in $wheelhouse; run build-wheelhouse-macos-arm.sh" >&2
  exit 1
fi
tui_wheel=$1
uv tool install \
  --offline \
  --no-index \
  --force \
  --python "$repository/.venv/bin/python" \
  --find-links "$wheelhouse" \
  "$tui_wheel"
"$UV_TOOL_BIN_DIR/merlin" --version
