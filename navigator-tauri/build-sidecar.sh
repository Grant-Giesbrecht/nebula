#!/usr/bin/env bash
# Freeze the Python bridge into a standalone executable and install it where
# Tauri expects an `externalBin`: src-tauri/binaries/nebula-bridge-<triple>.
#
# Tauri requires the target-triple suffix so a bundle can carry per-platform
# binaries; it strips the suffix when copying into the app. Run this before
# `npm run build` (and before `npm run dev` if you want to exercise the
# frozen bridge in development).
#
# Override the interpreter that gets frozen with NEBULA_PYTHON -- whatever
# Python builds the sidecar is the Python the app will ship.
set -euo pipefail

cd "$(dirname "$0")"

PYTHON="${NEBULA_PYTHON:-python3}"
NAME="nebula-bridge"

# Match the triple rustc is building for, not the host's uname -- they differ
# when cross-compiling or targeting x86_64 under Rosetta.
if [ -n "${TAURI_TARGET_TRIPLE:-}" ]; then
    TRIPLE="$TAURI_TARGET_TRIPLE"
else
    TRIPLE="$(rustc -vV | awk '/^host:/ {print $2}')"
fi
if [ -z "$TRIPLE" ]; then
    echo "error: could not determine the target triple (is rustc on PATH?)" >&2
    exit 1
fi

echo "==> freezing $NAME for $TRIPLE using $PYTHON"
"$PYTHON" -c "import PyInstaller" 2>/dev/null || {
    echo "error: PyInstaller not available for $PYTHON. Install it with:" >&2
    echo "         $PYTHON -m pip install pyinstaller" >&2
    exit 1
}

# --paths ../src: import `nebula` from the repo checkout rather than relying
#   on it being pip-installed for this interpreter.
# --hidden-import yaml: nebula's only runtime dependency; it is imported
#   lazily in places PyInstaller's static analysis does not follow.
"$PYTHON" -m PyInstaller \
    --onefile \
    --console \
    --clean \
    --noconfirm \
    --name "$NAME" \
    --paths ../src \
    --hidden-import yaml \
    --distpath build/sidecar/dist \
    --workpath build/sidecar/work \
    --specpath build/sidecar \
    sidecar/bridge.py

mkdir -p src-tauri/binaries
cp "build/sidecar/dist/$NAME" "src-tauri/binaries/$NAME-$TRIPLE"
chmod +x "src-tauri/binaries/$NAME-$TRIPLE"

echo "==> smoke test"
echo '{"id":1,"op":"ping","args":null}' | "src-tauri/binaries/$NAME-$TRIPLE"

echo "==> installed src-tauri/binaries/$NAME-$TRIPLE"
