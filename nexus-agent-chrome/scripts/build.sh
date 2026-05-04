#!/usr/bin/env bash
#
# Build script for the Nexus Agent Chrome extension.
#
# Produces dist/nexus-agent-vX.Y.Z.zip suitable for manual "Load unpacked"
# or upload to GitHub Release. No npm install required — we use nothing
# but zip.
#
# Usage:
#   bash scripts/build.sh          # from extensions/chrome/
#   npm run build                  # same, via package.json

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHROME_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$CHROME_DIR"

# Wave 4: regenerate manifest.json from Nexus's default_config.yaml so
# the distributed extension uses the operator's chosen externally_
# connectable.matches allowlist, not the source-controlled dev defaults.
# If PyYAML is missing, gen_manifest.py prints a warning and exits 0 —
# the build falls back to the source manifest (pre-Wave-4 behavior).
if command -v python3 >/dev/null 2>&1; then
  python3 "$SCRIPT_DIR/gen_manifest.py"
else
  echo "python3 not found; building with source manifest as-is." >&2
fi

# Extract version from manifest.json (simple grep, no jq dependency).
VERSION=$(grep -oE '"version"\s*:\s*"[^"]+"' manifest.json | head -1 | sed -E 's/.*"([^"]+)"$/\1/')
if [ -z "$VERSION" ]; then
  echo "Failed to parse version from manifest.json" >&2
  exit 1
fi

OUT_DIR="dist"
OUT_NAME="nexus-agent-v${VERSION}"
OUT_ZIP="${OUT_DIR}/${OUT_NAME}.zip"

rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR/$OUT_NAME"

# Copy only what the extension needs at runtime.
FILES=(
  manifest.json
  background
  content
  options
  popup
  icons
  shared
)
for f in "${FILES[@]}"; do
  if [ -e "$f" ]; then
    cp -R "$f" "$OUT_DIR/$OUT_NAME/"
  fi
done

# Sanity check — refuse to ship without the core files.
for required in manifest.json background/service_worker.js content/content.js icons/icon-16.png; do
  if [ ! -e "$OUT_DIR/$OUT_NAME/$required" ]; then
    echo "Missing required file: $required" >&2
    exit 2
  fi
done

# Zip it.
(cd "$OUT_DIR/$OUT_NAME" && zip -rq "../${OUT_NAME}.zip" .)

# Clean up expanded copy — keep only the zip and the unpacked dir for dev load.
echo "Built: $OUT_ZIP"
ls -la "$OUT_DIR"
