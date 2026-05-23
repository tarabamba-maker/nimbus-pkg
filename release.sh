#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# release.sh — bump version, build signed Tauri app, publish to GitHub releases.
#
# Usage: ./release.sh <new-version>   e.g. ./release.sh 2.0.1
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

NEW_VERSION="${1:-}"
if [ -z "$NEW_VERSION" ]; then
  echo "Usage: $0 <new-version>   (e.g. 2.0.1)"
  exit 1
fi

REPO="tarabamba-maker/nimbus-pkg"
SIGNING_KEY="$HOME/.tauri/nimbus-pkg.key"
PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
TAURI_DIR="$PROJECT_ROOT/ui-tauri"
CONF="$TAURI_DIR/src-tauri/tauri.conf.json"
CARGO_TOML="$TAURI_DIR/src-tauri/Cargo.toml"

if [ ! -f "$SIGNING_KEY" ]; then
  echo "ERROR: signing key not found at $SIGNING_KEY"
  exit 1
fi

# ── 1. Bump version in tauri.conf.json + Cargo.toml ─────────────────────────
echo "→ Bumping version to $NEW_VERSION"
python3 -c "
import json, pathlib
p = pathlib.Path('$CONF')
d = json.loads(p.read_text())
d['version'] = '$NEW_VERSION'
p.write_text(json.dumps(d, indent=2) + '\n')
"
sed -i.bak "s/^version = .*/version = \"$NEW_VERSION\"/" "$CARGO_TOML" && rm "$CARGO_TOML.bak"

# ── 2. Build with signing ───────────────────────────────────────────────────
echo "→ Building Tauri app (signed)"
cd "$TAURI_DIR"
export TAURI_SIGNING_PRIVATE_KEY="$(cat "$SIGNING_KEY")"
export TAURI_SIGNING_PRIVATE_KEY_PASSWORD=""
PATH="$HOME/.cargo/bin:$PATH" npm run tauri build

# ── 3. Find produced artifacts ──────────────────────────────────────────────
BUNDLE_DIR="$TAURI_DIR/src-tauri/target/release/bundle/macos"
TAR_GZ=$(ls "$BUNDLE_DIR"/*.app.tar.gz 2>/dev/null | head -1 || true)
SIG=$(ls "$BUNDLE_DIR"/*.app.tar.gz.sig 2>/dev/null | head -1 || true)
DMG_DIR="$TAURI_DIR/src-tauri/target/release/bundle/dmg"
DMG=$(ls "$DMG_DIR"/*.dmg 2>/dev/null | head -1 || true)

if [ -z "$TAR_GZ" ] || [ -z "$SIG" ]; then
  echo "ERROR: missing .app.tar.gz or .sig in $BUNDLE_DIR"
  exit 1
fi

# ── 4. Compose latest.json manifest ─────────────────────────────────────────
SIG_CONTENT=$(cat "$SIG")
ARCH=$(uname -m)   # arm64 → darwin-aarch64; x86_64 → darwin-x86_64
PLATFORM_KEY="darwin-aarch64"
[ "$ARCH" = "x86_64" ] && PLATFORM_KEY="darwin-x86_64"
PUB_DATE=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
TAR_NAME=$(basename "$TAR_GZ")
# GitHub replaces spaces with dots in release asset URLs
GH_TAR_NAME="${TAR_NAME// /.}"
DOWNLOAD_URL="https://github.com/$REPO/releases/download/v$NEW_VERSION/$GH_TAR_NAME"

LATEST_JSON="$PROJECT_ROOT/latest.json"
cat > "$LATEST_JSON" <<EOF
{
  "version": "$NEW_VERSION",
  "notes": "Release v$NEW_VERSION",
  "pub_date": "$PUB_DATE",
  "platforms": {
    "$PLATFORM_KEY": {
      "signature": "$SIG_CONTENT",
      "url": "$DOWNLOAD_URL"
    }
  }
}
EOF
echo "→ latest.json written"

# ── 5. Create GH release + upload assets ────────────────────────────────────
echo "→ Creating GitHub release v$NEW_VERSION on $REPO"
gh release create "v$NEW_VERSION" \
  --repo "$REPO" \
  --title "v$NEW_VERSION" \
  --notes "Release v$NEW_VERSION" \
  "$TAR_GZ" "$SIG" "$LATEST_JSON" ${DMG:+"$DMG"}

echo ""
echo "✅ Release v$NEW_VERSION published."
echo "   Manifest: https://github.com/$REPO/releases/latest/download/latest.json"
echo "   Users on previous version will see the update banner within 6 hours."
