#!/bin/bash
# Creates "Video Browser.app" on the Desktop that runs launcher.sh on double-click.
set -e
APP="$HOME/Desktop/Video Browser.app"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$APP/Contents/MacOS"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Video Browser</string>
  <key>CFBundleDisplayName</key><string>Video Browser</string>
  <key>CFBundleExecutable</key><string>launcher</string>
  <key>CFBundleIdentifier</key><string>local.ryan.videobrowser</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSUIElement</key><true/>
</dict></plist>
PLIST
cat > "$APP/Contents/MacOS/launcher" <<SH
#!/bin/bash
exec "$REPO/video_browse/launcher.sh"
SH
chmod +x "$APP/Contents/MacOS/launcher"
echo "Installed: $APP"
