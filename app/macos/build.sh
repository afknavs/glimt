#!/bin/zsh
# Builds Glimt.app (menu bar app) with the Swift toolchain. No Xcode project needed.
#   ./build.sh            build into ./build/Glimt.app
#   ./build.sh --install  also copy to ~/Applications and launch it
# `glimt app install` runs this same script from the installed package.
set -euo pipefail
cd "$(dirname "$0")"
APP="${GLIMT_APP_BUILD:-build}/Glimt.app"
rm -rf "$APP"; mkdir -p "$APP/Contents/MacOS"
swiftc -swift-version 5 -O -parse-as-library -target "$(uname -m)-apple-macos14.0" \
  Sources/*.swift -o "$APP/Contents/MacOS/Glimt"
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Glimt</string>
  <key>CFBundleDisplayName</key><string>Glimt</string>
  <key>CFBundleIdentifier</key><string>dev.glimt.menu</string>
  <key>CFBundleExecutable</key><string>Glimt</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>LSUIElement</key><true/>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict></plist>
PLIST
codesign --force --sign - "$APP" >/dev/null 2>&1
echo "Built $APP"
if [[ "${1:-}" == "--install" ]]; then
  mkdir -p ~/Applications
  pkill -x Glimt 2>/dev/null || true
  rm -rf ~/Applications/Glimt.app
  cp -R "$APP" ~/Applications/
  # Open it at login.
  AGENT=~/Library/LaunchAgents/dev.glimt.menu.plist
  cat > "$AGENT" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>dev.glimt.menu</string>
  <key>LimitLoadToSessionType</key><string>Aqua</string>
  <key>ProgramArguments</key><array><string>/usr/bin/open</string><string>-gj</string><string>-a</string><string>$HOME/Applications/Glimt.app</string></array>
  <key>RunAtLoad</key><true/>
</dict></plist>
PLIST
  launchctl bootout "gui/$(id -u)/dev.glimt.menu" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$AGENT"
  echo "Installed to ~/Applications/Glimt.app; it opens at login (remove $AGENT to stop that)"
fi
