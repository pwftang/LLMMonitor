"""Poll one device: macmon /json + omlx admin API + ComfyUI + llama-server → metrics + latest state.

Metric namespaces written to the store:
  sys.*   — macmon system metrics (kind="system")
  llm.*   — omlx /admin/api/stats (kind="llm")
  llm.model.<slug>.* — per-model series from /admin/api/models (kind="models")
  comfyui.* — ComfyUI /queue + /system_stats (kind="comfyui"; opt-in per device)
  llamacpp.* — llama-server Prometheus /metrics (kind="llamacpp"; opt-in per device)
"""

from __future__ import annotations

import asyncio
import logging
import re
import socket
import time
from typing import Any

import httpx

from .config import Config, Device
from .db import Store

log = logging.getLogger("hub.poll")


async def _resolve_ipv4(name: str) -> str | None:
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(name, None, family=socket.AF_INET)
    except OSError:
        return None
    return infos[0][4][0] if infos else None


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _gb(bytes_val: float) -> float:
    return bytes_val / (1024**3)


def extract_system(p: dict) -> dict[str, float]:
    """macmon /json payload → sys.* series."""
    out: dict[str, float] = {}

    def add(key: str, v: Any) -> None:
        if isinstance(v, (int, float)):
            out[key] = float(v)

    add("sys.cpu_util", p.get("cpu_active_ratio"))
    add("sys.gpu_util", p.get("gpu_active_ratio"))
    add("sys.ecpu_freq_mhz", p.get("ecpu_freq_mhz"))
    add("sys.pcpu_freq_mhz", p.get("pcpu_freq_mhz"))
    add("sys.gpu_freq_mhz", p.get("gpu_freq_mhz"))
    for w in ("cpu_power", "gpu_power", "ane_power", "all_power", "sys_power", "ram_power"):
        add(f"sys.{w.replace('_power', '')}_power_w", p.get(w))
    temp = p.get("temp") or {}
    add("sys.cpu_temp_c", temp.get("cpu_temp_avg"))
    add("sys.gpu_temp_c", temp.get("gpu_temp_avg"))
    mem = p.get("memory") or {}
    ram_total, ram_used = mem.get("ram_total"), mem.get("ram_usage")
    if isinstance(ram_total, (int, float)) and ram_total:
        out["sys.ram_total_gb"] = _gb(ram_total)
        if isinstance(ram_used, (int, float)):
            out["sys.ram_used_gb"] = _gb(ram_used)
            out["sys.ram_used_pct"] = ram_used / ram_total
    if isinstance(mem.get("swap_usage"), (int, float)):
        out["sys.swap_used_gb"] = _gb(mem["swap_usage"])
    fans = p.get("fans") or []
    rpms = [f.get("rpm") for f in fans if isinstance(f.get("rpm"), (int, float))]
    if rpms:
        out["sys.fan_rpm_max"] = float(max(rpms))
        out["sys.fan_count"] = float(len(rpms))
    return out


# seconds between omlx version fetches; version changes only on omlx restart/upgrade
_VERSION_INTERVAL = 300.0


def extract_omlx_stats(p: dict) -> dict[str, float]:
    """omlx /admin/api/stats payload → llm.* series."""
    out: dict[str, float] = {}

    def add(key: str, v: Any) -> None:
        if isinstance(v, (int, float)):
            out[key] = float(v)

    add("llm.prefill_tps", p.get("avg_prefill_tps"))
    add("llm.gen_tps", p.get("avg_generation_tps"))
    add("llm.cached_tokens", p.get("total_cached_tokens"))
    add("llm.cache_eff", p.get("cache_efficiency"))
    add("llm.prompt_tokens", p.get("total_prompt_tokens"))
    mem = p.get("model_memory_used")
    if isinstance(mem, (int, float)):
        out["llm.model_mem_gb"] = _gb(mem)
    add("llm.active_reqs", p.get("total_active_requests"))
    add("llm.waiting_reqs", p.get("total_waiting_requests"))
    return out


# omlx exposes live memory as actual_size (0 when the model is not loaded);
# estimated_size is the download size for unloaded models, not live usage.
_MODEL_MEM_KEYS = ("actual_size", "memory_used", "memory_bytes", "memory", "ram_bytes", "size_bytes")
_MODEL_REQ_KEYS = ("active_requests", "total_requests", "request_count", "requests")
_MODEL_ID_KEYS = ("id", "model_id", "model", "name")


def _first_key(d: dict, keys: tuple[str, ...]) -> Any:
    for k in keys:
        if k in d:
            return d[k]
    return None


def extract_model_series(models: list | dict) -> dict[str, float]:
    """omlx /admin/api/models payload → per-model llm.model.<slug>.* series.

    The exact response schema varies between omlx versions, so this is
    deliberately best-effort: whatever numeric memory/request fields exist
    get charted; the raw payload is kept in device state for the UI.
    """
    out: dict[str, float] = {}
    entries = models if isinstance(models, list) else models.get("models", [])
    if not isinstance(entries, list):
        return out
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        ident = _first_key(entry, _MODEL_ID_KEYS)
        if not isinstance(ident, str) or not ident:
            continue
        slug = _slug(ident)
        if not slug:
            continue
        mem = _first_key(entry, _MODEL_MEM_KEYS)
        if isinstance(mem, (int, float)) and mem:
            out[f"llm.model.{slug}.mem_gb"] = _gb(mem)
        reqs = _first_key(entry, _MODEL_REQ_KEYS)
        if isinstance(reqs, (int, float)):
            out[f"llm.model.{slug}.reqs"] = float(reqs)
    return out


def extract_comfyui(queue: dict, stats: dict) -> dict[str, float]:
    """ComfyUI /queue + /system_stats payloads → comfyui.* series.

    Queue counts come from /queue; model memory is ComfyUI's torch allocator
    delta on its compute device (on Mac, ComfyUI vram is unified RAM).
    """
    out: dict[str, float] = {}

    def _count(key: str, v: Any) -> None:
        out[key] = float(len(v)) if isinstance(v, list) else 0.0

    _count("comfyui.queue_running", queue.get("queue_running"))
    _count("comfyui.queue_pending", queue.get("queue_pending"))
    devices = (stats or {}).get("devices")
    if isinstance(devices, list) and devices and isinstance(devices[0], dict):
        total = devices[0].get("torch_vram_total")
        free = devices[0].get("torch_vram_free")
        if isinstance(total, (int, float)) and isinstance(free, (int, float)):
            out["comfyui.model_mem_gb"] = max(0.0, _gb(total - free))
    return out


_PROM_SAMPLE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{[^}]*\})?\s+(\S+)")


def parse_prometheus(text: str) -> dict[str, float]:
    """Minimal Prometheus text-exposition parser: metric name → value.

    Only covers what llama-server's /metrics emits: plain samples, no
    timestamps. Labelled series collapse to their last sample — llama.cpp's
    core metrics are unlabelled, so that isn't a limitation in practice.
    NaN/Inf samples are dropped.
    """
    out: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        m = _PROM_SAMPLE.match(line)
        if not m:
            continue
        try:
            value = float(m.group(2))
        except ValueError:
            continue
        if value == value and value not in (float("inf"), float("-inf")):
            out[m.group(1)] = value
    return out


def extract_llamacpp(
    counters: dict[str, float],
    prev: tuple[float, dict[str, float]] | None,
    now: float,
) -> dict[str, float]:
    """llama-server /metrics → llamacpp.* series.

    Throughput is derived from cumulative counters: tokens processed per
    second of *active work* (Δ tokens_total / Δ seconds_total), falling back
    to wall-clock rate when a server build doesn't expose the seconds
    counters. First poll after (re)start has no previous sample, so tps
    series appear from the second poll onwards.
    """
    out: dict[str, float] = {}

    def add(key: str, v: Any) -> None:
        if isinstance(v, (int, float)):
            out[key] = float(v)

    add("llamacpp.prompt_tokens", counters.get("llamacpp:prompt_tokens_total"))
    add("llamacpp.gen_tokens", counters.get("llamacpp:tokens_predicted_total"))
    add("llamacpp.active_reqs", counters.get("llamacpp:requests_processing"))
    add("llamacpp.waiting_reqs", counters.get("llamacpp:requests_deferred"))
    add("llamacpp.kv_used_tokens", counters.get("llamacpp:kv_cache_tokens"))
    add("llamacpp.kv_used_pct", counters.get("llamacpp:kv_cache_usage_ratio"))

    if prev is None:
        return out
    prev_ts, prev_counters = prev

    def rate(tokens_key: str, seconds_key: str) -> float | None:
        tokens = counters.get(tokens_key)
        prev_tokens = prev_counters.get(tokens_key)
        if tokens is None or prev_tokens is None:
            return None
        d_tok = tokens - prev_tokens
        if d_tok < 0:  # counter reset — llama-server restarted between polls
            return None
        d_sec = None
        sec, prev_sec = counters.get(seconds_key), prev_counters.get(seconds_key)
        if sec is not None and prev_sec is not None and sec - prev_sec >= 0:
            d_sec = sec - prev_sec
        else:
            d_sec = now - prev_ts
        return d_tok / d_sec if d_sec > 0 else None

    add("llamacpp.prefill_tps", rate("llamacpp:prompt_tokens_total", "llamacpp:prompt_seconds_total"))
    add(
        "llamacpp.gen_tps",
        rate("llamacpp:tokens_predicted_total", "llamacpp:tokens_predicted_seconds_total"),
    )
    return out


def llamacpp_model_name(props: dict) -> str | None:
    """Identify the loaded model from llama-server /props."""
    alias = props.get("model_alias")
    if isinstance(alias, str) and alias:
        return alias
    dgs = props.get("default_generation_settings")
    if isinstance(dgs, dict):
        model = dgs.get("model")
        if isinstance(model, str) and model:
            return model
    return None


class DeviceState:
    """Latest known state for one device; served by the REST API."""

    def __init__(self, device: Device):
        self.device = device
        self.online = False
        self.last_error: str | None = None
        self.last_seen: float | None = None
        self.pressure_level: str | None = None
        self.omlx_version: str | None = None
        self.tailscale_ip: str | None = None
        self.local_ip: str | None = None
        # Per-source health: None until the first macmon attempt, so partially
        # failing sources (macmon down, omlx up) can be surfaced in the UI.
        self.macmon_ok: bool | None = None
        self.macmon_last_ok: float | None = None
        self.comfy_ok: bool | None = None
        self.llamacpp_ok: bool | None = None
        self.system: dict[str, float] = {}
        self.llm: dict[str, float] = {}
        self.raw: dict[str, Any] = {}

    def mark_offline(self, err: str) -> None:
        self.online = False
        self.last_error = err

    def mark_ok(self) -> None:
        self.online = True
        self.last_error = None
        self.last_seen = time.time()

    def public(self) -> dict:
        base = self.device.omlx_base
        return {
            "id": self.device.id,
            "name": self.device.name,
            "host": self.device.host,
            "has_omlx": base is not None,
            "has_macmon": self.device.macmon_url is not None,
            "has_comfyui": self.device.comfyui_base is not None,
            "has_llamacpp": self.device.llamacpp_base is not None,
            "comfy_ok": self.comfy_ok,
            "llamacpp_ok": self.llamacpp_ok,
            "omlx_admin_url": f"{base}/admin" if base else None,
            "tailscale_ip": self.tailscale_ip,
            "local_ip": self.local_ip,
            "online": self.online,
            "last_error": self.last_error,
            "last_seen": self.last_seen,
            "macmon_ok": self.macmon_ok,
            "macmon_last_ok": self.macmon_last_ok,
            "pressure_level": self.pressure_level,
            "omlx_version": self.omlx_version,
            "system": self.system,
            "llm": self.llm,
            "raw": self.raw,
        }


# seconds between device IP re-resolutions; local DNS is cheap and this
# self-heals when a device's LAN address changes under DHCP
_IP_TTL = 60.0


class DevicePoller:
    def __init__(self, device: Device, cfg: Config, store: Store, state: DeviceState):
        self.device, self.cfg, self.store, self.state = device, cfg, store, state
        self._backoff_until = 0.0
        self._version_fetched_at = 0.0
        self._ips_resolved_at = 0.0
        self._client: httpx.AsyncClient | None = None
        # Previous /metrics sample for llama.cpp rate derivation (counters
        # are cumulative, so throughput needs a before/after).
        self._llamacpp_prev: tuple[float, dict[str, float]] | None = None

    async def run(self) -> None:
        log.info("poller started for %s", self.device.id)
        async with httpx.AsyncClient(
            timeout=self.cfg.request_timeout, follow_redirects=True
        ) as client:
            self._client = client
            tasks = [asyncio.create_task(self._fast_loop())]
            if self.device.omlx_base:
                tasks.append(asyncio.create_task(self._models_loop()))
            await asyncio.gather(*tasks)

    async def _fast_loop(self) -> None:
        while True:
            started = time.monotonic()
            ok = await self._tick_fast()
            if not ok:
                self._backoff_until = time.monotonic() + self.cfg.offline_backoff
            wait = self.cfg.poll_interval - (time.monotonic() - started)
            if time.monotonic() < self._backoff_until:
                wait = self._backoff_until - time.monotonic()
            await asyncio.sleep(max(0.1, wait))

    async def _models_loop(self) -> None:
        while True:
            started = time.monotonic()
            if time.monotonic() >= self._backoff_until:
                await self._tick_models()
            wait = self.cfg.models_interval - (time.monotonic() - started)
            await asyncio.sleep(max(0.5, wait))

    async def _tick_models(self) -> None:
        if time.monotonic() - self._version_fetched_at > _VERSION_INTERVAL:
            await self._tick_version()
        try:
            data = await self._omlx_get("/admin/api/models")
            # omlx wraps the list as {"models": [...]}; the frontend consumes
            # raw.omlx_models as a bare array, so unwrap here.
            entries = data.get("models", []) if isinstance(data, dict) else data
            self.state.raw["omlx_models"] = entries if isinstance(entries, list) else []
            self.store.insert(
                self.device.id, time.time(), "models", extract_model_series(data)
            )
        except Exception as e:  # noqa: BLE001 — devices drop off the tailnet routinely
            log.warning("%s models poll failed: %s", self.device.id, e)

    async def _tick_version(self) -> None:
        # omlx has no version endpoint; its FastAPI/OpenAPI metadata carries it.
        try:
            data = await self._omlx_get("/openapi.json")
            info = data.get("info", {}) if isinstance(data, dict) else {}
            version = info.get("version") if isinstance(info, dict) else None
            if isinstance(version, str) and version:
                self.state.omlx_version = version
        except Exception as e:  # noqa: BLE001
            log.debug("%s omlx version fetch failed: %s", self.device.id, e)
        self._version_fetched_at = time.monotonic()

    async def _resolve_ips(self) -> None:
        if time.monotonic() - self._ips_resolved_at < _IP_TTL:
            return
        # The TTL applies to failures too — a device that is off the tailnet
        # fails DNS slowly, and we don't want that blocking every tick.
        self._ips_resolved_at = time.monotonic()
        self.state.tailscale_ip = await _resolve_ipv4(self.device.host)
        # Best-effort mDNS: "box.tailnet.ts.net" → "box.local". Stays None on
        # LANs without mDNS, and the UI simply omits the line.
        short = self.device.host.split(".")[0]
        self.state.local_ip = await _resolve_ipv4(f"{short}.local")

    async def _tick_fast(self) -> bool:
        await self._resolve_ips()
        ok = False
        if self.device.macmon_url:
            ok |= await self._tick_macmon()
        if self.device.omlx_base:
            ok |= await self._tick_omlx_stats()
        if self.device.comfyui_base:
            ok |= await self._tick_comfyui()
        if self.device.llamacpp_base:
            ok |= await self._tick_llamacpp()
        if ok:
            self.state.mark_ok()
        else:
            self.state.mark_offline("unreachable")
            log.warning("%s unreachable (macmon=%s omlx=%s comfyui=%s llamacpp=%s)",
                        self.device.id, self.device.macmon_url, self.device.omlx_base,
                        self.device.comfyui_base, self.device.llamacpp_base)
        return ok

    async def _tick_macmon(self) -> bool:
        assert self._client is not None and self.device.macmon_url is not None
        was_ok = self.state.macmon_ok
        try:
            r = await self._client.get(self.device.macmon_url)
            r.raise_for_status()
            payload = r.json()
            if not isinstance(payload, dict):
                raise ValueError("macmon /json returned non-object payload")
        except Exception as e:  # noqa: BLE001
            # Warn on transition only — logging every tick would spam the log
            # for the whole duration of an outage.
            if was_ok is not False:
                log.warning("%s macmon unreachable: %s", self.device.id, e)
            self.state.macmon_ok = False
            return False
        if was_ok is False:
            log.info("%s macmon recovered", self.device.id)
        self.state.macmon_ok = True
        self.state.macmon_last_ok = time.time()
        series = extract_system(payload)
        self.state.system = series
        self.state.raw["macmon"] = payload
        self.store.insert(self.device.id, time.time(), "system", series)
        return True

    async def _tick_comfyui(self) -> bool:
        assert self._client is not None and self.device.comfyui_base is not None
        was_ok = self.state.comfy_ok
        try:
            queue_r, stats_r = await asyncio.gather(
                self._client.get(self.device.comfyui_base + "/queue"),
                self._client.get(self.device.comfyui_base + "/system_stats"),
            )
            queue_r.raise_for_status()
            stats_r.raise_for_status()
            queue, stats = queue_r.json(), stats_r.json()
            if not isinstance(queue, dict) or not isinstance(stats, dict):
                raise ValueError("comfyui returned non-object payload")
        except Exception as e:  # noqa: BLE001
            if was_ok is not False:
                log.warning("%s comfyui unreachable: %s", self.device.id, e)
            self.state.comfy_ok = False
            self.state.raw.pop("comfyui", None)
            return False
        if was_ok is False:
            log.info("%s comfyui recovered", self.device.id)
        self.state.comfy_ok = True
        series = extract_comfyui(queue, stats)
        devices = stats.get("devices") or [{}]
        self.state.raw["comfyui"] = {
            "version": (stats.get("system") or {}).get("comfyui_version"),
            "device_type": devices[0].get("type") if isinstance(devices[0], dict) else None,
            "queue_running": series["comfyui.queue_running"],
            "queue_pending": series["comfyui.queue_pending"],
            "mem_gb": series.get("comfyui.model_mem_gb"),
        }
        self.store.insert(self.device.id, time.time(), "comfyui", series)
        return True

    async def _tick_llamacpp(self) -> bool:
        assert self._client is not None and self.device.llamacpp_base is not None
        was_ok = self.state.llamacpp_ok
        try:
            metrics_r, props_r = await asyncio.gather(
                self._client.get(self.device.llamacpp_base + "/metrics"),
                self._client.get(self.device.llamacpp_base + "/props"),
            )
            metrics_r.raise_for_status()
            props_r.raise_for_status()
            counters = parse_prometheus(metrics_r.text)
            props = props_r.json()
            if not counters:
                raise ValueError("llamacpp /metrics returned no samples")
            if not isinstance(props, dict):
                raise ValueError("llamacpp /props returned non-object payload")
        except Exception as e:  # noqa: BLE001
            if was_ok is not False:
                log.warning("%s llamacpp unreachable: %s", self.device.id, e)
            self.state.llamacpp_ok = False
            self.state.raw.pop("llamacpp", None)
            # Forget the baseline so a restarted server (reset counters)
            # doesn't produce a bogus rate on recovery.
            self._llamacpp_prev = None
            return False
        if was_ok is False:
            log.info("%s llamacpp recovered", self.device.id)
        self.state.llamacpp_ok = True
        now = time.time()
        series = extract_llamacpp(counters, self._llamacpp_prev, now)
        self._llamacpp_prev = (now, counters)
        self.state.raw["llamacpp"] = {
            "model": llamacpp_model_name(props),
            "gen_tps": series.get("llamacpp.gen_tps"),
            "prefill_tps": series.get("llamacpp.prefill_tps"),
            "kv_used_pct": series.get("llamacpp.kv_used_pct"),
            "kv_used_tokens": series.get("llamacpp.kv_used_tokens"),
            "prompt_tokens": series.get("llamacpp.prompt_tokens"),
            "gen_tokens": series.get("llamacpp.gen_tokens"),
            "active_reqs": series.get("llamacpp.active_reqs"),
            "waiting_reqs": series.get("llamacpp.waiting_reqs"),
        }
        self.store.insert(self.device.id, now, "llamacpp", series)
        return True

    async def _tick_omlx_stats(self) -> bool:
        try:
            payload = await self._omlx_get("/admin/api/stats")
            if not isinstance(payload, dict):
                raise ValueError("omlx /admin/api/stats returned non-object payload")
        except Exception as e:  # noqa: BLE001
            log.debug("%s omlx stats failed: %s", self.device.id, e)
            return False
        series = extract_omlx_stats(payload)
        self.state.llm = series
        self.state.pressure_level = (
            payload.get("pressure_level") if isinstance(payload, dict) else None
        )
        self.state.raw["omlx_stats"] = payload
        self.store.insert(self.device.id, time.time(), "llm", series)
        return True

    async def _omlx_get(self, path: str) -> Any:
        assert self._client is not None and self.device.omlx_base is not None
        url = self.device.omlx_base + path
        r = await self._client.get(url)
        if r.status_code == 401 and self.device.api_key:
            login = await self._client.post(
                self.device.omlx_base + "/admin/api/login",
                json={"api_key": self.device.api_key},
            )
            login.raise_for_status()
            r = await self._client.get(url)
        r.raise_for_status()
        return r.json()
