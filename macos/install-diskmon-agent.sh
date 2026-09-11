#!/bin/bash
# Install/refresh the diskmon LaunchAgent on a Mac.
#
# diskmon is a tiny stdlib-only Python agent that answers GET /json with
# {"timestamp","path","total_bytes","used_bytes","free_bytes"} for the root
# volume, plus "mem_available_pct" (kern.memorystatus_level) when the sysctl
# is readable. This installer is self-contained: it writes the agent to
# ~/Library/diskmon/diskmon.py, then bootstraps a LaunchAgent that waits up to
# 5 minutes for Tailscale to assign a 100.* address before starting it
# (reboot-safe), with KeepAlive restarting it if it ever exits. Re-running is
# safe and also kills any strays first.
#
# Port: 9091 (next to macmon's 9090). Run once per Mac:
#   bash install-diskmon-agent.sh
set -euo pipefail

AGENT_DIR="$HOME/Library/diskmon"
AGENT_PY="$AGENT_DIR/diskmon.py"
PLIST="$HOME/Library/LaunchAgents/com.diskmon.plist"

pkill -f "diskmon.py" 2>/dev/null && echo "killed running diskmon instance" || true

mkdir -p "$AGENT_DIR"
cat > "$AGENT_PY" <<'EOF'
#!/usr/bin/env python3
"""diskmon - tiny disk + memory-pressure agent for LLMMonitor. Stdlib only."""
import argparse
import json
import shutil
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


# macOS reports pressure inversely: kern.memorystatus_level is the % of
# memory still available (what `memory_pressure -Q` prints as the
# "system-wide memory free percentage").
def memory_level_pct():
    try:
        out = subprocess.check_output(
            ["/usr/sbin/sysctl", "-n", "kern.memorystatus_level"], timeout=2
        )
        return float(out.strip())
    except (OSError, subprocess.SubprocessError, ValueError):
        return None  # not macOS / sysctl failed → omit the key


def agent_payload(path: str) -> dict:
    usage = shutil.disk_usage(path)
    payload = {
        "timestamp": time.time(),
        "path": path,
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
    }
    level = memory_level_pct()
    if level is not None:
        payload["mem_available_pct"] = level
    return payload


class Handler(BaseHTTPRequestHandler):
    watch_path = "/"
    server_version = "diskmon/1.0"

    def do_GET(self):
        if self.path not in ("/", "/json"):
            self.send_error(404)
            return
        body = json.dumps(agent_payload(self.watch_path)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep stdout quiet; launchd captures it
        pass


def main():
    ap = argparse.ArgumentParser(description="disk usage agent")
    ap.add_argument("--host", default="127.0.0.1", help="bind address")
    ap.add_argument("-p", "--port", type=int, default=9091)
    ap.add_argument("--path", default="/", help="filesystem to report on")
    args = ap.parse_args()
    Handler.watch_path = args.path
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
EOF
chmod +x "$AGENT_PY"

cat > "$PLIST" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.diskmon</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string><string>-c</string>
    <string>TS=$( (command -v tailscale; ls /Applications/Tailscale.app/Contents/MacOS/Tailscale /opt/homebrew/bin/tailscale /usr/local/bin/tailscale 2>/dev/null; echo tailscaled-missing) | head -1 ); IP=""; for i in $(seq 1 150); do IP=$($TS ip -4 2>/dev/null | head -1); case "$IP" in 100.*) break;; esac; IP=""; sleep 2; done; [ -n "$IP" ] || { echo "diskmon: no tailnet IP after 300s; exiting for launchd retry"; exit 1; }; exec /usr/bin/env python3 "$HOME/Library/diskmon/diskmon.py" --host "$IP" -p 9091</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>/tmp/diskmon.log</string>
  <key>StandardErrorPath</key><string>/tmp/diskmon.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/com.diskmon" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

sleep 2
tail -n 3 /tmp/diskmon.log || true
echo "diskmon listening on this machine's tailnet IP, port 9091"
