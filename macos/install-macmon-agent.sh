#!/bin/bash
# Install/refresh the macmon LaunchAgent on a Mac.
#
# The agent waits up to 5 minutes for Tailscale to assign a 100.* address
# before starting macmon (reboot-safe: macmon no longer races Tailscale),
# and KeepAlive relaunches it if it ever exits. Re-running is safe and also
# kills any stray manually-started macmon first.
set -euo pipefail

PLIST="$HOME/Library/LaunchAgents/com.macmon.plist"

pkill -f "macmon serve" 2>/dev/null && echo "killed running macmon instance" || true

cat > "$PLIST" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.macmon</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string><string>-c</string>
    <string>TS=$( (command -v tailscale; ls /Applications/Tailscale.app/Contents/MacOS/Tailscale /opt/homebrew/bin/tailscale /usr/local/bin/tailscale 2>/dev/null; echo tailscaled-missing) | head -1 ); IP=""; for i in $(seq 1 150); do IP=$($TS ip -4 2>/dev/null | head -1); case "$IP" in 100.*) break;; esac; IP=""; sleep 2; done; [ -n "$IP" ] || { echo "macmon: no tailnet IP after 300s; exiting for launchd retry"; exit 1; }; exec /opt/homebrew/bin/macmon serve --host "$IP" -p 9090 -i 1000</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>/tmp/macmon.log</string>
  <key>StandardErrorPath</key><string>/tmp/macmon.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/com.macmon" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

sleep 3
tail -n 3 /tmp/macmon.log
