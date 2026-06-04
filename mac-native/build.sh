#!/bin/bash
# Build the SwiftUI executable and wrap it into a runnable .app bundle.
# No full Xcode required — uses the Command Line Tools swift toolchain.
set -e
cd "$(dirname "$0")"

APP="Stock Mac.app"
SDK="$(xcrun --show-sdk-path)"

echo "▶︎ swiftc compile…"
swiftc -sdk "$SDK" -target arm64-apple-macos26.0 -O \
  Sources/StockMac/*.swift Sources/StockMac/Views/*.swift \
  -o .build_StockMac

BIN=".build_StockMac"
[ -f "$BIN" ] || { echo "build failed: $BIN missing"; exit 1; }

echo "▶︎ bundling $APP…"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/StockMac"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Stock Mac</string>
  <key>CFBundleDisplayName</key><string>Stock Mac</string>
  <key>CFBundleIdentifier</key><string>com.stockautomation.mac</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>CFBundleShortVersionString</key><string>0.1</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>StockMac</string>
  <key>LSMinimumSystemVersion</key><string>26.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSPrincipalClass</key><string>NSApplication</string>
  <key>NSAppTransportSecurity</key>
  <dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict>
</plist>
PLIST

# Ad-hoc sign so the local network / process-spawn entitlements work without a dev account.
codesign --force --deep --sign - "$APP" 2>/dev/null || true

echo "✅ built $APP"
