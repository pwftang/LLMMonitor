"""Poll one device: macmon /json + omlx admin API → metric namespace + latest state.

Metric namespaces written to the store:
  sys.*   — macmon system metrics (kind="system")
  llm.*   — omlx /admin/api/stats (kind="llm")
  llm.model.<slug>.* — per-model series from /admin/api/models (kind="models")
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from .config import Config, Device
from .db import Store

log = logging.getLogger("hub.poll")


def _slug(text: str) -> str:
    import re

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


class DeviceState:
    """Latest known state for one device; served by the REST API."""

    def __init__(self, device: Device):
        self.device = device
        self.online = False
        self.last_error: str | None = None
        self.last_seen: float | None = None
        self.pressure_level: str | None = None
        self.omlx_version: str | None = None
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
            "omlx_admin_url": f"{base}/admin" if base else None,
            "online": self.online,
            "last_error": self.last_error,
            "last_seen": self.last_seen,
            "pressure_level": self.pressure_level,
            "omlx_version": self.omlx_version,
            "system": self.system,
            "llm": self.llm,
            "raw": self.raw,
        }


class DevicePoller:
    def __init__(self, device: Device, cfg: Config, store: Store, state: DeviceState):
        self.device, self.cfg, self.store, self.state = device, cfg, store, state
        self._backoff_until = 0.0
        self._version_fetched_at = 0.0
        self._client: httpx.AsyncClient | None = None

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

    async def _tick_fast(self) -> bool:
        ok = False
        if self.device.macmon_url:
            ok |= await self._tick_macmon()
        if self.device.omlx_base:
            ok |= await self._tick_omlx_stats()
        if ok:
            self.state.mark_ok()
        else:
            self.state.mark_offline("unreachable")
            log.warning("%s unreachable (macmon=%s omlx=%s)",
                        self.device.id, self.device.macmon_url, self.device.omlx_base)
        return ok

    async def _tick_macmon(self) -> bool:
        assert self._client is not None and self.device.macmon_url is not None
        try:
            r = await self._client.get(self.device.macmon_url)
            r.raise_for_status()
            payload = r.json()
            if not isinstance(payload, dict):
                raise ValueError("macmon /json returned non-object payload")
        except Exception as e:  # noqa: BLE001
            log.debug("%s macmon failed: %s", self.device.id, e)
            return False
        series = extract_system(payload)
        self.state.system = series
        self.state.raw["macmon"] = payload
        self.store.insert(self.device.id, time.time(), "system", series)
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
