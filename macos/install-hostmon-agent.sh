#!/bin/bash
# Install/refresh the hostmon LaunchAgent on a Mac.
#
# hostmon is a tiny stdlib-only Python agent that answers GET /json with
# host telemetry that macmon doesn't cover: currently
# {"timestamp","path","total_bytes","used_bytes","free_bytes"} for the root
# volume, plus "mem_available_pct" (kern.memorystatus_level), "uptime_s"
# (kern.boottime), "net_rx_Bps"/"net_tx_Bps" (network throughput from
# cumulative netstat counters; rate across polls, so absent on the first
# request) and "top_rss_procs" ([[name, MB]] top-5 process memory, from ps)
# — each when the underlying sysctl/command is readable. This installer is
# self-contained: it writes the agent to
# ~/Library/hostmon/hostmon.py, then bootstraps a LaunchAgent that waits up to
# 5 minutes for Tailscale to assign a 100.* address before starting it
# (reboot-safe), with KeepAlive restarting it if it ever exits. Re-running is
# safe and also kills any strays first — including migrating/removing a
# previous "diskmon" (com.diskmon, ~/Library/diskmon) install.
#
# Port: 9091 (next to macmon's 9090). Run once per Mac:
#   bash install-hostmon-agent.sh
set -euo pipefail

AGENT_DIR="$HOME/Library/hostmon"
AGENT_PY="$AGENT_DIR/hostmon.py"
PLIST="$HOME/Library/LaunchAgents/com.hostmon.plist"

# Migrate a previous diskmon install: stop it, drop its LaunchAgent and dir.
pkill -f "diskmon.py" 2>/dev/null && echo "killed running diskmon instance" || true
if launchctl bootout "gui/$(id -u)/com.diskmon" 2>/dev/null; then
  echo "removed old com.diskmon LaunchAgent"
fi
rm -f "$HOME/Library/LaunchAgents/com.diskmon.plist"
rm -rf "$HOME/Library/diskmon"

pkill -f "hostmon.py" 2>/dev/null && echo "killed running hostmon instance" || true

mkdir -p "$AGENT_DIR"
cat > "$AGENT_PY" <<'EOF'
#!/usr/bin/env python3
"""hostmon - tiny host telemetry agent for LLMMonitor. Stdlib only."""
import argparse
import json
import re
import shutil
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _run(cmd: list[str]) -> str | None:
    try:
        return subprocess.check_output(cmd, timeout=2, stderr=subprocess.DEVNULL).decode()
    except (OSError, subprocess.SubprocessError):
        return None  # command missing / failed → omit the key


# macOS reports pressure inversely: kern.memorystatus_level is the % of
# memory still available (what `memory_pressure -Q` prints as the
# "system-wide memory free percentage").
def memory_level_pct():
    out = _run(["/usr/sbin/sysctl", "-n", "kern.memorystatus_level"])
    if out is None:
        return None
    try:
        return float(out.strip())
    except ValueError:
        return None


# kern.boottime prints "{ sec = 1747…, usec = … } <date>"; uptime = now - sec.
def uptime_s():
    out = _run(["/usr/sbin/sysctl", "-n", "kern.boottime"])
    if out is None:
        return None
    m = re.search(r"sec\s*=\s*(\d+)", out)
    return round(time.time() - int(m.group(1))) if m else None


_net_last = None  # (t, rx_bytes, tx_bytes) cumulative across interfaces


def net_rates_bps():
    """(rx_Bps, tx_Bps) summed over non-loopback interfaces, from the delta
    between this and the previous `netstat -ibn` sample. First call returns
    (None, None): a single cumulative snapshot has no rate."""
    global _net_last
    out = _run(["/usr/sbin/netstat", "-ibn"])
    if out is None:
        return None, None
    rx = tx = 0
    for line in out.splitlines():
        c = line.split()
        # Name Mtu Network Address Ipkts Ierrs Ibytes Opkts Obytes Coll —
        # link# Address rows carry the per-interface cumulative counters.
        if len(c) >= 10 and c[3].startswith("link#") and c[0] != "lo0":
            try:
                rx += int(c[6])
                tx += int(c[9])
            except ValueError:
                continue
    now = time.time()
    rates = (None, None)
    if _net_last is not None and now > _net_last[0]:
        drx, dtx = rx - _net_last[1], tx - _net_last[2]
        if drx >= 0 and dtx >= 0:  # counters reset when an interface bounces
            rates = (round(drx / (now - _net_last[0]), 1), round(dtx / (now - _net_last[0]), 1))
    _net_last = (now, rx, tx)
    return rates


def top_rss_procs(n: int = 5):
    """Top-n memory consumers as [[name, MB], ...], totals aggregated by
    command name so app + helper processes count together."""
    out = _run(["/bin/ps", "-A", "-o", "rss=", "-o", "comm="])
    if out is None:
        return None
    totals = {}
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        try:
            kb = int(parts[0])
        except ValueError:
            continue
        name = parts[1].rsplit("/", 1)[-1]
        totals[name] = totals.get(name, 0) + kb
    return [[name, round(kb / 1024, 1)] for name, kb in
            sorted(totals.items(), key=lambda kv: -kv[1])[:n]]


def agent_payload(path: str) -> dict:
    usage = shutil.disk_usage(path)
    payload = {
        "timestamp": time.time(),
        "path": path,
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
    }
    for key, val in (
        ("mem_available_pct", memory_level_pct()),
        ("uptime_s", uptime_s()),
        ("top_rss_procs", top_rss_procs()),
    ):
        if val is not None:
            payload[key] = val
    rx, tx = net_rates_bps()
    if rx is not None:
        payload["net_rx_Bps"] = rx
        payload["net_tx_Bps"] = tx
    return payload


class Handler(BaseHTTPRequestHandler):
    watch_path = "/"
    server_version = "hostmon/1.2"

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
    ap = argparse.ArgumentParser(description="host telemetry agent")
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
  <key>Label</key><string>com.hostmon</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string><string>-c</string>
    <string>TS=$( (command -v tailscale; ls /Applications/Tailscale.app/Contents/MacOS/Tailscale /opt/homebrew/bin/tailscale /usr/local/bin/tailscale 2>/dev/null; echo tailscaled-missing) | head -1 ); IP=""; for i in $(seq 1 150); do IP=$($TS ip -4 2>/dev/null | head -1); case "$IP" in 100.*) break;; esac; IP=""; sleep 2; done; [ -n "$IP" ] || { echo "hostmon: no tailnet IP after 300s; exiting for launchd retry"; exit 1; }; exec /usr/bin/env python3 "$HOME/Library/hostmon/hostmon.py" --host "$IP" -p 9091</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>/tmp/hostmon.log</string>
  <key>StandardErrorPath</key><string>/tmp/hostmon.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/com.hostmon" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

sleep 2
tail -n 3 /tmp/hostmon.log || true
echo "hostmon listening on this machine's tailnet IP, port 9091"
