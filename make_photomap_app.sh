#!/bin/bash
# Rebuilds "~/Desktop/Photo Map (Local).app", an AppleScript applet that runs
# this repo's site_launcher.sh.
#
# Why an applet and not a bundle that executes the shell script directly: a
# script-executable app runs as /bin/bash, a platform binary, and tccd
# silently denies its Documents access instead of prompting — the app can then
# never cold-start the server. The compiled applet stub is a real Mach-O that
# owns its TCC identity, so the one-time Allow prompt works.
#
# Rebuilding re-signs the bundle, which changes its cdhash and invalidates any
# existing TCC grant. A stale grant is worse than none (it can silently deny),
# so the old record is reset here — expect ONE fresh "access Documents" prompt
# on the next launch: click Allow.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$HOME/Desktop/Photo Map (Local).app"
BUNDLE_ID="local.ryan.photomaplocal"

SRC="$(mktemp -d)/applet.applescript"
cat > "$SRC" <<EOF
try
	do shell script "/bin/bash " & quoted form of "$REPO/site_launcher.sh"
on error errMsg
	display notification errMsg with title "Photo Map (local)"
end try
EOF

rm -rf "$APP"
osacompile -o "$APP" "$SRC"
plutil -replace CFBundleIdentifier -string "$BUNDLE_ID" "$APP/Contents/Info.plist"
plutil -replace CFBundleName -string "Photo Map (Local)" "$APP/Contents/Info.plist"
plutil -replace LSUIElement -bool true "$APP/Contents/Info.plist"
codesign --force --deep -s - "$APP"
tccutil reset SystemPolicyDocumentsFolder "$BUNDLE_ID" >/dev/null 2>&1 || true
echo "Built $APP — first launch will ask for Documents access, click Allow."
