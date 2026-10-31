#!/bin/bash
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$PROJECT_DIR/build_app"
APP="$BUILD/Playlist Downloader.app"
CONTENTS="$APP/Contents"
MACOS="$CONTENTS/MacOS"
RES="$CONTENTS/Resources"

rm -rf "$BUILD"
mkdir -p "$MACOS" "$RES"

rsync -a --exclude '__pycache__' --exclude '*.pyc' "$PROJECT_DIR/app/" "$RES/app/"
rsync -a --exclude '__pycache__' --exclude '*.pyc' "$PROJECT_DIR/web/" "$RES/web/"

if [ -f "$PROJECT_DIR/cloud_config.json" ]; then
  cp "$PROJECT_DIR/cloud_config.json" "$RES/cloud_config.json"
fi

cat > "$MACOS/PlaylistDownloader" << 'LAUNCHER'
#!/bin/bash
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
DIR="$(cd "$(dirname "$0")/.." && pwd)"
RES="$DIR/Resources"

PYTHONPATH="$RES" PYTHONUNBUFFERED=1 cd "$RES" && python3 -u -m app.server --port 8765 &
SERVER_PID=$!
sleep 3
open "http://127.0.0.1:8765/"
wait $SERVER_PID
LAUNCHER
chmod +x "$MACOS/PlaylistDownloader"

cat > "$CONTENTS/Info.plist" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>PlaylistDownloader</string>
    <key>CFBundleIdentifier</key>
    <string>com.brianyang.playlistdownloader</string>
    <key>CFBundleName</key>
    <string>Playlist Downloader</string>
    <key>CFBundleDisplayName</key>
    <string>Playlist Downloader</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>LSUIElement</key>
    <true/>
</dict>
</plist>
PLIST

printf 'APPL????' > "$CONTENTS/PkgInfo"
xattr -cr "$APP" 2>/dev/null || true
echo "Done: $APP"
