# LLMMonitor

One-page monitoring for a small fleet of Apple Silicon Macs serving LLMs with
[omlx](https://github.com/jundot/omlx). At-a-glance cards for every device with
click-through drill-down: loaded models, per-model and total model memory,
token throughput, KV cache stats — plus macmon-style CPU/GPU/RAM, temperature,
fan and power metrics, with history (24 h full-resolution, 30 d rolled up).

## How it works

```
┌──────────────┐   polls every 2–5 s over Tailscale    ┌──────────────────────┐
│  hub (this)  │ ────────────────────────────────────▶ │ Mac: omlx :8000      │
│  FastAPI +   │   GET /admin/api/stats, /models        │      macmon :9090    │
│  SQLite + JS │   GET http://<mac>:9090/json           │      ComfyUI :8188 * │
└──────────────┘   GET http://<mac>:8188/queue, /system_stats (opt-in per device)
                   GET http://<mac>:8080/metrics, /props (opt-in per device)
                                                           └ llama-server :8080 *
```

\* ComfyUI and llama-server polling are opt-in per device (`comfyui_port` /
`llamacpp_port` in `hub.toml`).

The Macs run **nothing new except `macmon serve`** (Rust, no sudo, negligible
footprint). The hub is a single Python process with one SQLite file, so moving
it to a VM/container later is a copy of the repo + data dir.

## Set up each Mac (once)

Install macmon, then make it bind to the tailnet only — the `/json` endpoint
has no auth, so it must not listen on LAN/Wi-Fi (especially on the MacBook):

```sh
brew install macmon          # or: cargo install macmon
```

Then run the bootstrap script from this repo on the Mac:

```sh
./macos/install-macmon-agent.sh
```

The script writes `~/Library/LaunchAgents/com.macmon.plist` and bootstraps it
with launchd. The plist execs `/bin/sh` so it can *wait for* the tailnet IP
(up to 5 minutes, retried via `KeepAlive`): macmon previously raced Tailscale
at boot and either crashed or bound to the wrong interface. This also means no
per-machine plists and no hardcoded IPs — every Mac runs the identical file.
`com.macmon.plist` at the repo root is the same plist for manual installs.

Verify:

```sh
launchctl list | grep macmon          # column 1 = PID means running
curl -s http://$(tailscale ip -4 | head -1):9090/json | head -c 200
```

omlx needs nothing further — just note its admin port and API key. Note that
the MacBook will go to sleep; the hub marks it offline and backfills when it
returns (that's expected, not an error). Don't bother enabling omlx auth for
the hub's sake — the hub works fine without an API key if omlx has none.

### ComfyUI (optional, per device)

If a Mac also runs ComfyUI, the hub can show its queue depth and model memory
alongside the omlx stats. By default ComfyUI binds to 127.0.0.1, which the hub
can't reach over the tailnet — restart ComfyUI bound to the Mac's tailscale IP:

```sh
python main.py --listen $(tailscale ip -4 | head -1) --port 8188
```

(ComfyUI's `/queue` and `/system_stats` endpoints are unauthenticated, so only
bind the tailnet IP — never `0.0.0.0`, especially on the MacBook.) Then add
`comfyui_port = 8188` to that device's block in `hub.toml`. Nothing new runs on
the Mac — the hub just polls the REST API ComfyUI already serves.

### llama-server / llama.cpp (optional, per device)

If a Mac also runs llama.cpp's llama-server (e.g. Hermes), the hub can show
its generation/prefill throughput, request counts, KV cache usage and model
name alongside everything else. llama-server must be started with `--metrics`
(the Prometheus `/metrics` endpoint is off by default) and bound to the
tailnet IP:

```sh
llama-server -m model.gguf --port 8080 --metrics \
  --host $(tailscale ip -4 | head -1)
```

(`/metrics` and `/props` are unauthenticated, so again: tailnet IP only.) Then
add `llamacpp_port = 8080` to that device's block in `hub.toml`. Sampling two
polls apart is how the hub derives t/s from llama-server's cumulative token
counters, so throughput appears from the second poll after start/restart.

A device can run omlx and llama-server side by side; they report under
separate `llm.*` / `llamacpp.*` metric namespaces and separate card sections.

> **Trust boundary:** the omlx admin port is key-protected by you; macmon,
> ComfyUI, llama-server and this hub are unauthenticated read-only endpoints.
> All of them rely on the tailnet as the boundary — lock a Tailscale ACL to
> your own devices if you want belt and braces.

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
