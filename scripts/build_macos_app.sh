#!/bin/bash
# Build "Playlist Downloader.app" — a menu bar app with the Python backend
# frozen inside it. Run from anywhere; output lands in dist/.
#
#   scripts/build_macos_app.sh              # bundled backend, ready to ship
#   scripts/build_macos_app.sh --dev        # no backend; uses the repo .venv
#
# Requires: Xcode command line tools (swiftc) and, for a bundled backend,
# a Python with the requirements + PyInstaller installed (the repo .venv).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

DEV=0
[[ "${1:-}" == "--dev" ]] && DEV=1

VERSION="$(python3 - <<'PY'
import re, pathlib
text = pathlib.Path("app/__init__.py").read_text()
print(re.search(r'__version__\s*=\s*"([^"]+)"', text).group(1))
PY
)"

BUILD="$ROOT/build/macos"
APP="$ROOT/dist/Playlist Downloader.app"
CONTENTS="$APP/Contents"
MACOS="$CONTENTS/MacOS"
RES="$CONTENTS/Resources"

echo "==> Playlist Downloader $VERSION"
rm -rf "$BUILD" "$APP"
mkdir -p "$MACOS" "$RES"

echo "==> Compiling menu bar controller"
swiftc -O \
  -target arm64-apple-macos11 \
  "$ROOT/launcher/macos/MenuBarApp.swift" \
  -o "$MACOS/PlaylistDownloader" \
  -framework Cocoa

if [[ $DEV -eq 1 ]]; then
  echo "==> Dev build: no bundled backend (repo .venv is used at runtime)"
  rsync -a --exclude '__pycache__' --exclude '*.pyc' "$ROOT/app/" "$RES/app/"
  rsync -a --exclude '__pycache__' --exclude '*.pyc' "$ROOT/web/" "$RES/web/"
  cp "$ROOT/cloud_config.json" "$RES/cloud_config.json"
else
  if [[ ! -x "$ROOT/build/backend/PlaylistDownloaderBackend/PlaylistDownloaderBackend" ]]; then
    echo "==> Freezing backend with PyInstaller (first run only)"
    PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
    [[ -x "$PYTHON" ]] || PYTHON=python3
    "$PYTHON" "$ROOT/scripts/build_backend.py"
  fi
  echo "==> Embedding backend"
  cp -R "$ROOT/build/backend/PlaylistDownloaderBackend" "$RES/backend"
fi

cp "$ROOT/AppIcon.icns" "$RES/AppIcon.icns"

cat > "$CONTENTS/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>Playlist Downloader</string>
    <key>CFBundleDisplayName</key>
    <string>Playlist Downloader</string>
    <key>CFBundleIdentifier</key>
    <string>com.brianyang.playlistdownloader</string>
    <key>CFBundleExecutable</key>
    <string>PlaylistDownloader</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>$VERSION</string>
    <key>CFBundleVersion</key>
    <string>$VERSION</string>
    <key>CFBundleInfoDictionaryVersion</key>
    <string>6.0</string>
    <key>LSMinimumSystemVersion</key>
    <string>11.0</string>
    <key>LSUIElement</key>
    <true/>
    <key>LSApplicationCategoryType</key>
    <string>public.app-category.music</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>NSAppTransportSecurity</key>
    <dict>
        <key>NSAllowsArbitraryLoads</key>
        <true/>
    </dict>
</dict>
</plist>
PLIST
printf 'APPL????' > "$CONTENTS/PkgInfo"

# Ad-hoc signature keeps macOS happy about the freshly built bundle. Shipping to
# other Macs needs a Developer ID certificate + notarization (see DEVELOPER.md).
echo "==> Signing (ad-hoc)"
codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || true
xattr -cr "$APP" 2>/dev/null || true

if [[ $DEV -eq 0 ]]; then
  echo "==> Packaging dist/PlaylistDownloader-macOS.zip"
  (cd "$ROOT/dist" && rm -f PlaylistDownloader-macOS.zip && \
     ditto -c -k --keepParent "Playlist Downloader.app" PlaylistDownloader-macOS.zip)
fi

echo "Done: $APP"
