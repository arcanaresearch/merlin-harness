#!/bin/sh
set -eu

repository=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
artifact_root="$repository/dist"
wheelhouse="$artifact_root/wheelhouse"
requirements="$artifact_root/requirements-macos-arm.lock.txt"
manifest="$artifact_root/wheelhouse-manifest.json"

if [ "$(uname -s)" != "Darwin" ] || [ "$(uname -m)" != "arm64" ]; then
  echo "wheelhouse build requires macOS ARM" >&2
  exit 1
fi
macos_major=$(sw_vers -productVersion | cut -d. -f1)
if [ "$macos_major" != "14" ]; then
  echo "wheelhouse build targets macOS 14; current host is macOS $macos_major" >&2
  exit 1
fi
python_version=$($repository/.venv/bin/python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
if [ "$python_version" != "3.13" ]; then
  echo "wheelhouse build requires workspace Python 3.13" >&2
  exit 1
fi

rm -rf -- "$wheelhouse"
mkdir -p -- "$wheelhouse"
uv export \
  --format requirements.txt \
  --all-packages \
  --no-dev \
  --no-emit-workspace \
  --output-file "$requirements"
"$repository/.venv/bin/python" "$repository/scripts/encrypt-builtin-packs.py"
uv build --all-packages --wheel --out-dir "$wheelhouse"
uv run --with pip python -m pip download \
  --require-hashes \
  --only-binary=:all: \
  --dest "$wheelhouse" \
  --requirement "$requirements"
pack_wheels=$(find "$wheelhouse" -maxdepth 1 -type f -name 'merlin_packs-*.whl')
pack_wheel_count=$(printf '%s\n' "$pack_wheels" | sed '/^$/d' | wc -l | tr -d ' ')
if [ "$pack_wheel_count" -ne 1 ]; then
  echo "expected exactly one merlin-packs wheel, found $pack_wheel_count" >&2
  exit 1
fi
"$repository/.venv/bin/python" "$repository/scripts/verify_encrypted_packs.py" \
  --wheel "$pack_wheels"
"$repository/.venv/bin/python" "$repository/scripts/hash-wheelhouse.py" \
  "$wheelhouse" "$manifest"

echo "wheelhouse: $wheelhouse"
echo "manifest: $manifest"
