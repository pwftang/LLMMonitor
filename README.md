# LLMMonitor

One-page monitoring for a small fleet of Apple Silicon Macs serving LLMs with
[omlx](https://github.com/jundot/omlx). At-a-glance cards for every device with
click-through drill-down: loaded models, per-model and total model memory,
token throughput, KV cache stats — plus macmon-style CPU/GPU/RAM, temperature,
fan and power metrics, with history (24 h full-resolution, 30 d rolled up).

## How it works

```
┌──────────────┐   polls every 2–5 s over Tailscale    ┌─────────────────┐
│  hub (this)  │ ────────────────────────────────────▶ │ Mac: omlx :8000 │
│  FastAPI +   │   GET /admin/api/stats, /models        │     macmon:9090 │
│  SQLite + JS │   GET http://<mac>:9090/json           └─────────────────┘
└──────────────┘
```

The Macs run **nothing new except `macmon serve`** (Rust, no sudo, negligible
footprint). The hub is a single Python process with one SQLite file, so moving
it to a VM/container later is a copy of the repo + data dir.

## Set up each Mac (once)

Install macmon, then make it bind to the tailnet only — the `/json` endpoint
has no auth, so it must not listen on LAN/Wi-Fi (especially on the MacBook):

```sh
brew install macmon          # or: cargo install macmon
tailscale ip -4              # note the 100.x.y.z address
which tailscale              # sanity: CLI must exist at a known path
```

Each Mac has its own plist under `macos/` — its tailscale IP is baked in
(stable on a tailnet) and it execs `/opt/homebrew/bin/macmon` directly, so
there is no shell or `$PATH` to go wrong (launchd gives jobs a minimal PATH;
a shell one-liner in a plist that calls `brew` or bare `tailscale` will fail):
`macos/com.macmon.mac-studio-m2-ultra.plist` → `100.74.115.38`
`macos/com.macmon.patricks-mac-studio.plist`   → `100.117.172.29`
`macos/com.macmon.pats-macbook-pro.plist`      → `100.91.150.110`

On an Intel Mac edit line 9 to `/usr/local/bin/macmon`. For new machines,
`com.macmon.plist` at the repo root is a template that locates the tailscale
CLI and tailnet IP itself at start — prefer a hardcoded-IP copy once known.

Copy onto the Mac and load:

```sh
# filename on the Mac must be com.macmon.plist
mkdir -p ~/Library/LaunchAgents
launchctl load ~/Library/LaunchAgents/com.macmon.plist
launchctl list | grep macmon          # column 1 = PID means running
curl -s http://$(tailscale ip -4 | head -1):9090/json | head -c 200
```

omlx needs nothing further — just note its admin port and API key. Note that
the MacBook will go to sleep; the hub marks it offline and backfills when it
returns (that's expected, not an error). Don't bother enabling omlx auth for
the hub's sake — the hub works fine without an API key if omlx has none.

> **Trust boundary:** the omlx admin port is key-protected by you; macmon and
> this hub are unauthenticated read-only endpoints. All three rely on the
> tailnet as the boundary — lock a Tailscale ACL to your own devices if you
> want belt and braces.

## Run the hub

```sh
python3 -m venv .venv && .venv/bin/pip install -e ./server
cp hub.example.toml hub.toml   # fill in tailnet hostnames + omlx keys
cd server && ../.venv/bin/python -m hub
# → http://127.0.0.1:8400
```

Set `bind` in `hub.toml` to this box's tailnet IP if you want to browse the
dashboard from your other devices. `HUB_MOCK=1 ../.venv/bin/python -m hub`
runs against synthetic telemetry (no Macs needed) — handy for UI tweaks.

### Container / VM migration

```sh
podman build -t llmmonitor -f Containerfile .
podman run -d -p 8400:8400 -v llmmonitor-data:/data llmmonitor
```

Put your filled-in `hub.toml` on the `/data` volume. The SQLite database lives
beside it by default (`data_dir`), so the volume is a complete backup.

## Data & retention

- `samples` — raw 2 s metrics, kept for `full_res_hours` (default 24 h)
- `rollups` — 5-min min/avg/max per metric, kept for `rollup_days` (default 30 d)
- A hourly background job folds samples into rollups and prunes both tables.
  History requests automatically use rollups for ranges beyond the full-res window.

## Tests

```sh
.venv/bin/pip install -e "./server[dev]"
cd server && ../.venv/bin/python -m pytest -q
```

## API (for tinkering)

- `GET /api/devices` — every configured device with latest metrics
- `GET /api/devices/{id}/latest`
- `GET /api/devices/{id}/history?metrics=llm.gen_tps,sys.cpu_util&range=6h`
  (ranges: `1h 3h 6h 24h 7d 30d`, or explicit `from_ts`/`to_ts`)
