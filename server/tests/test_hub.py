import asyncio
import sqlite3
import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from hub.api import create_app
from hub.config import Config, Device, Retention, load
from hub.db import Store
from hub.poll import (
    DevicePoller,
    DeviceState,
    extract_comfyui,
    extract_model_series,
    extract_omlx_stats,
    extract_system,
)

# From the macmon README example (same shape as `macmon serve` /json).
MACMON_PAYLOAD = {
    "timestamp": "2025-02-24T20:38:15.427569+00:00",
    "temp": {"cpu_temp_avg": 43.73614, "gpu_temp_avg": 36.95167},
    "memory": {
        "ram_total": 25769803776,
        "ram_usage": 20985479168,
        "swap_total": 4294967296,
        "swap_usage": 2602434560,
    },
    "fans": [
        {"name": "fan0", "rpm": 999, "max_rpm": 4900},
        {"name": "fan1", "rpm": 1200, "max_rpm": 5200},
    ],
    "cpu_scaled_ratio": 0.036854,
    "cpu_active_ratio": 0.092,
    "ecpu_freq_mhz": 1100,
    "ecpu_scaled_ratio": 0.082656614,
    "ecpu_active_ratio": 0.18,
    "pcpu_freq_mhz": 1800,
    "pcpu_scaled_ratio": 0.015181795,
    "pcpu_active_ratio": 0.04,
    "gpu_freq_mhz": 461,
    "gpu_scaled_ratio": 0.021497859,
    "gpu_active_ratio": 0.09,
    "cpu_power": 0.20486385,
    "gpu_power": 0.017451683,
    "ane_power": 0.0,
    "all_power": 0.22231553,
    "sys_power": 5.876533,
    "ram_power": 0.11635789,
    "gpu_ram_power": 0.0009615385,
}

OMLX_STATS = {
    "avg_prefill_tps": 512.3,
    "avg_generation_tps": 41.7,
    "total_cached_tokens": 183400,
    "cache_efficiency": 0.62,
    "total_prompt_tokens": 295487,
    "model_memory_used": 19791209280,
    "pressure_level": "normal",
    "total_active_requests": 2,
    "total_waiting_requests": 0,
}

# Real ComfyUI /queue + /system_stats (port 8188), trimmed to the fields used.
COMFY_QUEUE = {
    "queue_running": [[1, "p1", {}, {}, [9]]],
    "queue_pending": [[2, "p2", {}, {}, [9]], [3, "p3", {}, {}, [9]]],
}
COMFY_SYS = {
    "system": {"comfyui_version": "0.3.59"},
    "devices": [
        {
            "name": "mps",
            "type": "mps",
            "index": 0,
            "vram_total": 2**33,
            "vram_free": 2**32,
            "torch_vram_total": 2**33,
            "torch_vram_free": 7 * 1024**3,
        }
    ],
}


class TestExtractSystem:
    def test_full_payload(self):
        m = extract_system(MACMON_PAYLOAD)
        assert m["sys.cpu_util"] == pytest.approx(0.092)
        assert m["sys.gpu_util"] == pytest.approx(0.09)
        assert m["sys.cpu_power_w"] == pytest.approx(0.20486385)
        assert m["sys.sys_power_w"] == pytest.approx(5.876533)
        assert m["sys.cpu_temp_c"] == pytest.approx(43.73614)
        assert m["sys.gpu_temp_c"] == pytest.approx(36.95167)
        assert m["sys.ram_used_gb"] == pytest.approx(20985479168 / 1024**3)
        assert m["sys.ram_used_pct"] == pytest.approx(20985479168 / 25769803776)
        assert m["sys.swap_used_gb"] == pytest.approx(2602434560 / 1024**3)
        assert m["sys.fan_rpm_max"] == 1200.0
        assert m["sys.ecpu_freq_mhz"] == 1100.0
        assert m["sys.gpu_freq_mhz"] == 461.0

    def test_missing_sections_are_tolerated(self):
        # headless/no-GPU chips, chips without fan reporting, etc.
        m = extract_system({"memory": {"ram_total": 100, "ram_usage": 50}})
        assert m["sys.ram_used_pct"] == pytest.approx(0.5)
        assert "sys.fan_rpm_max" not in m
        assert "sys.cpu_temp_c" not in m

    def test_empty_payload(self):
        assert extract_system({}) == {}


class TestExtractOmlxStats:
    def test_full_payload(self):
        m = extract_omlx_stats(OMLX_STATS)
        assert m["llm.prefill_tps"] == pytest.approx(512.3)
        assert m["llm.gen_tps"] == pytest.approx(41.7)
        assert m["llm.cached_tokens"] == 183400.0
        assert m["llm.cache_eff"] == pytest.approx(0.62)
        assert m["llm.prompt_tokens"] == 295487.0
        assert m["llm.model_mem_gb"] == pytest.approx(19791209280 / 1024**3)
        assert m["llm.active_reqs"] == 2.0
        assert m["llm.waiting_reqs"] == 0.0

    def test_partial_payload(self):
        assert extract_omlx_stats({"avg_generation_tps": 3.2}) == {"llm.gen_tps": 3.2}


class TestExtractComfyui:
    def test_full_payload(self):
        m = extract_comfyui(COMFY_QUEUE, COMFY_SYS)
        assert m["comfyui.queue_running"] == 1.0
        assert m["comfyui.queue_pending"] == 2.0
        assert m["comfyui.model_mem_gb"] == pytest.approx(1.0)

    def test_missing_devices(self):
        # CPU-only or pre-device-stats builds: still counts the queue.
        m = extract_comfyui(COMFY_QUEUE, {"system": {"comfyui_version": "0.3.59"}})
        assert m["comfyui.queue_running"] == 1.0
        assert m["comfyui.queue_pending"] == 2.0
        assert "comfyui.model_mem_gb" not in m

    def test_empty_payload(self):
        m = extract_comfyui({}, {})
        assert m == {"comfyui.queue_running": 0.0, "comfyui.queue_pending": 0.0}


class TestExtractModelSeries:
    def test_list_payload(self):
        models = [
            {"id": "mlx-community/Qwen3-32B-4bit", "memory_used": 19_769_876_544, "active_requests": 2},
            {"id": "Qwen2.5-Coder-14B", "memory_used": 16_211_894_272, "active_requests": 0},
        ]
        m = extract_model_series(models)
        assert m["llm.model.mlx-community-qwen3-32b-4bit.mem_gb"] == pytest.approx(
            19_769_876_544 / 1024**3
        )
        assert m["llm.model.qwen2-5-coder-14b.reqs"] == 0.0

    def test_wrapped_payload(self):
        m = extract_model_series({"models": [{"name": "foo/bar-8b", "memory_bytes": 1024**3}]})
        assert m["llm.model.foo-bar-8b.mem_gb"] == pytest.approx(1.0)

    def test_live_omlx_shape(self):
        # Real /admin/api/models payload (omlx on port 8000): memory is
        # actual_size, and unloaded models report actual_size 0.
        m = extract_model_series(
            {
                "models": [
                    {
                        "id": "Qwen3.8-27B-bf16",
                        "loaded": True,
                        "is_loading": False,
                        "estimated_size": 57_449_286_809,
                        "actual_size": 55_155_563_160,
                        "pinned": False,
                    },
                    {
                        "id": "Idle-Model",
                        "loaded": False,
                        "estimated_size": 97_102_991_580,
                        "actual_size": 0,
                    },
                ]
            }
        )
        assert m["llm.model.qwen3-8-27b-bf16.mem_gb"] == pytest.approx(
            55_155_563_160 / 1024**3
        )
        assert "llm.model.idle-model.mem_gb" not in m

    def test_garbage_is_ignored(self):
        assert extract_model_series({"models": "nope"}) == {}
        assert extract_model_series([{"no_id": True}, 42, None]) == {}


class TestStore:
    def _store(self, **kw):
        return Store.in_memory(Retention(full_res_hours=1, rollup_days=30, rollup_seconds=300, **kw))

    def test_insert_and_recent_history(self):
        s = self._store()
        now = time.time()
        for i in range(10):
            s.insert("dev", now - 100 + i, "system", {"sys.cpu_util": i / 10})
        s.commit()
        h = s.history("dev", ["sys.cpu_util"], now - 200, now)
        assert len(h["sys.cpu_util"]) == 10
        assert h["sys.cpu_util"][-1][1] == pytest.approx(0.9)
        s.close()

    def test_rollup_and_prune(self):
        s = self._store()
        t0 = 1_700_000_000.0  # fixed past timestamp
        now = t0 + 3 * 3600
        # 2 samples/sec-value pairs across two 5-min buckets, older than full-res (1h)
        for i in range(10):
            ts = t0 + i * 30  # spans 0..270s → buckets at t0//300*300 and +300
            s.insert("dev", ts, "system", {"sys.gpu_util": float(i)})
        s.commit()
        stats = s.rollup_and_prune(now=now)
        assert stats["rollups_written"] >= 2  # one metric × two buckets
        assert stats["samples_pruned"] == 10
        # raw samples are gone; rollups remain. The bucket boundary below t0
        # (t0//300*300 = t0-200) is excluded by the from_ts filter, so the
        # first returned bucket is the 5-min window starting at t0+100.
        h = s.history("dev", ["sys.gpu_util"], t0, now)
        series = h["sys.gpu_util"]
        assert series, "expected rollup history"
        averages = [v for _, v in series]
        first_bucket_start = (int(t0 // 300) + 1) * 300
        first_bucket = [i for i in range(10) if first_bucket_start <= t0 + i * 30]
        assert first_bucket == list(range(4, 10))
        assert averages[0] == pytest.approx(sum(first_bucket) / len(first_bucket))
        s.close()

    def test_decimation_budget(self):
        s = self._store()
        now = time.time()
        for i in range(5000):
            s.insert("dev", now - 4000 + i, "system", {"x": float(i)})
        s.commit()
        from hub.db import MAX_POINTS

        assert len(s.history("dev", ["x"], now - 5000, now)["x"]) <= MAX_POINTS
        s.close()

    def test_rollup_bands(self):
        s = self._store()
        t0 = 1_700_000_000.0
        now = t0 + 3 * 3600
        for i, v in enumerate([10.0, 99.0, 3.0]):
            s.insert("dev", t0 + i, "system", {"sys.cpu_util": v})
        s.commit()
        s.rollup_and_prune(now=now)
        # samples land in the bucket starting at t0-200; query must include it
        series, bands = s.history_with_bands("dev", ["sys.cpu_util"], t0 - 300, now)
        # one 5-minute bucket covered all three samples
        assert [round(v) for _, v in series["sys.cpu_util"]] == [round((10.0 + 99.0 + 3.0) / 3)]
        b = bands["sys.cpu_util"]
        assert [v for _, v in b["min"]] == [3.0]
        assert [v for _, v in b["max"]] == [99.0]
        assert len(b["min"]) == 1 and len(b["max"]) == 1
        s.close()

    def test_full_res_history_has_no_bands(self):
        s = self._store()
        now = time.time()
        s.insert("dev", now - 10, "system", {"x": 1.0})
        s.commit()
        _, bands = s.history_with_bands("dev", ["x"], now - 60, now)
        assert bands == {}
        s.close()

    def test_history_merges_rollups_with_recent_samples(self):
        # 7d/30d charts request from_ts far behind the full-res cutoff; the
        # response must still end at the newest samples, not stop at the
        # oldest rollup horizon.
        s = self._store()  # full_res_hours=1
        now = time.time()
        old_bucket = int((now - 2 * 3600) // 300) * 300
        s.insert("dev", old_bucket + 10, "system", {"x": 10.0})
        s.insert("dev", old_bucket + 40, "system", {"x": 20.0})
        s.insert("dev", now - 100, "system", {"x": 30.0})
        s.insert("dev", now - 50, "system", {"x": 40.0})
        s.commit()
        stats = s.rollup_and_prune(now=now)
        assert stats["rollups_written"] == 1
        series, bands = s.history_with_bands("dev", ["x"], now - 3 * 3600, now)
        values = [v for _, v in series["x"]]
        assert values == [pytest.approx(15.0), 30.0, 40.0]
        timestamps = [ts for ts, _ in series["x"]]
        assert timestamps == sorted(timestamps)
        assert bands["x"]["min"] == [[pytest.approx(float(old_bucket)), 10.0]]
        assert bands["x"]["max"] == [[pytest.approx(float(old_bucket)), 20.0]]
        s.close()

    def test_history_past_range_uses_rollups_only(self):
        # A range fully behind the rollup horizon must not touch raw samples.
        s = self._store()
        now = time.time()
        for i in range(3):
            s.insert("dev", now - 2 * 3600 + i, "system", {"x": float(i)})
        s.commit()
        s.rollup_and_prune(now=now)
        horizon = int((now - 3600) // 300) * 300
        series, bands = s.history_with_bands("dev", ["x"], now - 3 * 3600, horizon)
        assert [round(v) for _, v in series["x"]] == [1]
        assert bands["x"]["min"] and bands["x"]["max"]
        s.close()


class TestConfig:
    def test_load(self, tmp_path):
        (tmp_path / "hub.toml").write_text(
            """
bind = "0.0.0.0"
port = 9999
data_dir = "./data"
poll_interval = 3

[retention]
full_res_hours = 12
rollup_days = 14

[[devices]]
name = "Mac Studio 1"
host = "studio-1.ts.net"
omlx_port = 8080
macmon_port = 9090
api_key = "k1"

[[devices]]
name = "GPU-less"
host = "mini.ts.net"
omlx_port = 0
macmon_port = 9091
"""
        )
        cfg = load(tmp_path / "hub.toml")
        assert cfg.bind == "0.0.0.0"
        assert cfg.port == 9999
        assert cfg.poll_interval == 3.0
        assert cfg.retention.full_res_hours == 12
        assert cfg.data_dir == (tmp_path / "data").resolve()
        d1, d2 = cfg.devices
        assert d1.id == "mac-studio-1"
        assert d1.omlx_base == "http://studio-1.ts.net:8080"
        assert d1.macmon_url == "http://studio-1.ts.net:9090/json"
        assert d2.omlx_base is None  # omlx_port 0 → disabled
        assert d2.macmon_url == "http://mini.ts.net:9091/json"

    def test_device_id_override(self, tmp_path):
        (tmp_path / "hub.toml").write_text(
            '[[devices]]\nname = "Mac Studio"\nhost = "x"\nid = "studio"\n'
            '[[devices]]\nname = "No Id"\nhost = "y"\n'
        )
        cfg = load(tmp_path / "hub.toml")
        assert cfg.devices[0].id == "studio"
        assert cfg.devices[1].id == "no-id"

    def test_device_ports_default_when_omitted(self, tmp_path):
        # Omitting a port key must fall back to the defaults, not silently
        # disable the source (a None port previously slipped past d.get()).
        (tmp_path / "hub.toml").write_text('[[devices]]\nname = "Mac"\nhost = "x"\n')
        cfg = load(tmp_path / "hub.toml")
        (dev,) = cfg.devices
        assert dev.omlx_port == 8000
        assert dev.omlx_base == "http://x:8000"
        assert dev.macmon_port == 9090
        assert dev.macmon_url == "http://x:9090/json"

    def test_comfyui_opt_in(self, tmp_path):
        # Unlike omlx/macmon, ComfyUI has no default port: omitted (or 0)
        # must disable it, an explicit port enables it.
        (tmp_path / "hub.toml").write_text(
            '[[devices]]\nname = "No Comfy"\nhost = "a"\n'
            '[[devices]]\nname = "Comfy"\nhost = "b"\ncomfyui_port = 8188\n'
            '[[devices]]\nname = "Off"\nhost = "c"\ncomfyui_port = 0\n'
        )
        cfg = load(tmp_path / "hub.toml")
        no_comfy, comfy, off = cfg.devices
        assert no_comfy.comfyui_port is None and no_comfy.comfyui_base is None
        assert comfy.comfyui_base == "http://b:8188"
        assert off.comfyui_port == 0 and off.comfyui_base is None

    def test_duplicate_ids_rejected(self, tmp_path):
        (tmp_path / "hub.toml").write_text(
            '[[devices]]\nname = "A B"\nhost = "x"\n[[devices]]\nname = "a-b"\nhost = "y"\n'
        )
        with pytest.raises(SystemExit):
            load(tmp_path / "hub.toml")

    def test_mock_mode_without_config(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HUB_MOCK", "1")
        monkeypatch.setenv("HUB_CONFIG", str(tmp_path / "missing.toml"))
        cfg = load()
        assert cfg.mock and len(cfg.devices) == 3


class TestPoller:
    def _poller(self, handler, device=None):
        cfg = Config()
        store = Store.in_memory()
        state = DeviceState(device or Device(name="Test", host="h"))
        poller = DevicePoller(state.device, cfg, store, state)
        poller._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=""
        )
        return poller, state, store

    async def test_macmon_success(self):
        def handler(request):
            return httpx.Response(200, json=MACMON_PAYLOAD)

        dev = Device(name="T", host="h", omlx_port=None, macmon_port=9090)
        poller, state, store = self._poller(handler, dev)
        assert await poller._tick_fast() is True
        assert state.online
        assert state.system["sys.cpu_temp_c"] == pytest.approx(43.73614)
        store.close()

    async def test_omlx_401_triggers_login(self):
        calls = []

        def handler(request):
            calls.append(request.url.path)
            if request.url.path == "/admin/api/login":
                return httpx.Response(200, json={"ok": True})
            if calls.count("/admin/api/login") == 0:
                return httpx.Response(401)
            return httpx.Response(200, json=OMLX_STATS)

        dev = Device(name="T", host="h", omlx_port=8080, macmon_port=None, api_key="k")
        poller, state, store = self._poller(handler, dev)
        assert await poller._tick_fast() is True
        assert "/admin/api/login" in calls
        assert state.llm["llm.gen_tps"] == pytest.approx(41.7)
        store.close()

    async def test_offline_marked(self):
        def handler(request):
            raise httpx.ConnectError("no route to host")

        dev = Device(name="T", host="h", omlx_port=None, macmon_port=9090)
        poller, state, store = self._poller(handler, dev)
        assert await poller._tick_fast() is False
        assert not state.online
        assert state.last_error == "unreachable"
        store.close()

    async def test_macmon_failure_tracked_per_source(self):
        # The 2026-08-25 incident: macmon died on both Studios while omlx kept
        # serving, so devices showed "online" with silently frozen sys.* stats.
        fail = {"macmon": False}

        def handler(request):
            if request.url.path == "/json":
                if fail["macmon"]:
                    raise httpx.ConnectError("connection refused")
                return httpx.Response(200, json=MACMON_PAYLOAD)
            return httpx.Response(200, json=OMLX_STATS)

        dev = Device(name="T", host="h", omlx_port=8080, macmon_port=9090)
        poller, state, store = self._poller(handler, dev)
        assert state.macmon_ok is None  # not polled yet → no UI badge

        fail["macmon"] = True
        assert await poller._tick_fast() is True
        assert state.online  # device stays "online" via omlx
        assert state.macmon_ok is False
        assert state.public()["macmon_ok"] is False
        assert state.macmon_last_ok is None

        fail["macmon"] = False
        assert await poller._tick_fast() is True
        assert state.macmon_ok is True
        assert state.macmon_last_ok is not None
        assert state.public()["macmon_ok"] is True
        store.close()

    async def test_models_tick_unwraps_payload_for_frontend(self):
        def handler(request):
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "id": "Qwen3.8-27B-bf16",
                            "loaded": True,
                            "actual_size": 55_155_563_160,
                        }
                    ]
                },
            )

        dev = Device(name="T", host="h", omlx_port=8000, macmon_port=None)
        poller, state, store = self._poller(handler, dev)
        await poller._tick_models()
        # The frontend renders raw.omlx_models directly — it must be a list,
        # not omlx's {"models": [...]} wrapper.
        assert isinstance(state.raw["omlx_models"], list)
        assert state.raw["omlx_models"][0]["id"] == "Qwen3.8-27B-bf16"
        store.commit()
        h = store.history(
            "t", ["llm.model.qwen3-8-27b-bf16.mem_gb"], time.time() - 10, time.time() + 1
        )
        assert h["llm.model.qwen3-8-27b-bf16.mem_gb"]
        store.close()

    async def test_comfyui_success(self):
        def handler(request):
            if request.url.path == "/queue":
                return httpx.Response(200, json=COMFY_QUEUE)
            return httpx.Response(200, json=COMFY_SYS)

        dev = Device(
            name="T", host="h", omlx_port=None, macmon_port=None, comfyui_port=8188
        )
        poller, state, store = self._poller(handler, dev)
        assert state.comfy_ok is None
        assert await poller._tick_fast() is True
        assert state.comfy_ok is True
        assert state.raw["comfyui"]["version"] == "0.3.59"
        assert state.raw["comfyui"]["device_type"] == "mps"
        assert state.raw["comfyui"]["queue_running"] == 1.0
        assert state.raw["comfyui"]["queue_pending"] == 2.0
        assert state.raw["comfyui"]["mem_gb"] == pytest.approx(1.0)
        store.commit()
        h = store.history("t", ["comfyui.model_mem_gb"], time.time() - 10, time.time() + 1)
        assert h["comfyui.model_mem_gb"][-1][1] == pytest.approx(1.0)
        store.close()

    async def test_comfyui_failure_clears_stale_raw(self):
        ok = {"comfy": True}

        def handler(request):
            if not ok["comfy"]:
                raise httpx.ConnectError("connection refused")
            if request.url.path == "/queue":
                return httpx.Response(200, json=COMFY_QUEUE)
            return httpx.Response(200, json=COMFY_SYS)

        dev = Device(
            name="T", host="h", omlx_port=None, macmon_port=None, comfyui_port=8188
        )
        poller, state, store = self._poller(handler, dev)
        assert await poller._tick_fast() is True
        assert "comfyui" in state.raw

        ok["comfy"] = False
        assert await poller._tick_fast() is False
        assert state.comfy_ok is False
        # Stale values must not linger for the UI — the card marks ComfyUI
        # unreachable instead of showing frozen queue depths.
        assert "comfyui" not in state.raw

        ok["comfy"] = True
        assert await poller._tick_fast() is True
        assert state.comfy_ok is True
        assert "comfyui" in state.raw
        store.close()


class TestApi:
    def _client(self):
        cfg = Config(devices=[Device(name="Studio One", host="h")])
        store = Store.in_memory()
        states = {d.id: DeviceState(d) for d in cfg.devices}
        states["studio-one"].system = {"sys.cpu_util": 0.5}
        states["studio-one"].mark_ok()
        now = time.time()
        store.insert("studio-one", now - 10, "system", {"sys.cpu_util": 0.4})
        store.insert("studio-one", now - 5, "system", {"sys.cpu_util": 0.6})
        store.commit()
        app = create_app(cfg, store, states, start_pollers=False)
        return TestClient(app), store

    def test_devices_and_latest(self):
        client, store = self._client()
        with client:
            r = client.get("/api/devices")
            assert r.status_code == 200
            devs = r.json()
            assert devs[0]["id"] == "studio-one"
            assert devs[0]["online"] is True
            r = client.get("/api/devices/studio-one/latest")
            assert r.json()["system"]["sys.cpu_util"] == 0.5
            r = client.get("/api/devices/ghost/latest")
            assert r.status_code == 404
        store.close()

    def test_devices_expose_omlx_admin_url(self):
        # The overview links straight to each device's omlx admin dashboard;
        # devices without omlx must advertise no link at all.
        cfg = Config(devices=[
            Device(name="Serving", host="h", omlx_port=8080),
            Device(name="Headless", host="m", omlx_port=None),
        ])
        store = Store.in_memory()
        app = create_app(cfg, store, {d.id: DeviceState(d) for d in cfg.devices}, start_pollers=False)
        with TestClient(app) as client:
            devs = {d["id"]: d for d in client.get("/api/devices").json()}
            assert devs["serving"]["omlx_admin_url"] == "http://h:8080/admin"
            assert devs["serving"]["has_omlx"] is True
            assert devs["headless"]["omlx_admin_url"] is None
            assert devs["headless"]["has_omlx"] is False
        store.close()

    def test_devices_expose_comfyui_flags(self):
        cfg = Config(devices=[
            Device(name="Comfy", host="c", comfyui_port=8188),
            Device(name="Plain", host="p"),
        ])
        store = Store.in_memory()
        states = {}
        for d in cfg.devices:
            states[d.id] = DeviceState(d)
            states[d.id].mark_ok()
        states["comfy"].comfy_ok = True
        app = create_app(cfg, store, states, start_pollers=False)
        with TestClient(app) as client:
            devs = {d["id"]: d for d in client.get("/api/devices").json()}
            assert devs["comfy"]["has_comfyui"] is True
            assert devs["comfy"]["comfy_ok"] is True
            assert devs["plain"]["has_comfyui"] is False
            assert devs["plain"]["comfy_ok"] is None
        store.close()

    def test_devices_expose_ip_fields(self):
        # The card header shows tailscale/local IPs; both stay None until the
        # poller's DNS resolution runs, then reflect the resolved addresses.
        cfg = Config(devices=[Device(name="Studio", host="studio.example.ts.net")])
        store = Store.in_memory()
        dev = cfg.devices[0]
        state = DeviceState(dev)
        app = create_app(cfg, store, {dev.id: state}, start_pollers=False)
        with TestClient(app) as client:
            served = client.get("/api/devices").json()[0]
            assert served["tailscale_ip"] is None
            assert served["local_ip"] is None
        poller = DevicePoller(dev, cfg, store, state)
        with patch("hub.poll._resolve_ipv4", new=AsyncMock(side_effect=lambda name: {
            "studio.example.ts.net": "100.64.0.7",
            "studio.local": "192.168.0.7",
        }.get(name))):
            asyncio.run(poller._resolve_ips())
        assert state.tailscale_ip == "100.64.0.7"
        assert state.local_ip == "192.168.0.7"
        assert state.public()["tailscale_ip"] == "100.64.0.7"
        store.close()

    def test_static_assets_disable_caching(self):
        # A stale cached app.js keeps old bugs alive after a refresh; assets
        # must always be revalidated against the hub.
        client, store = self._client()
        with client:
            for path in ("/", "/app.js", "/charts.js"):
                r = client.get(path)
                assert r.status_code == 200
                assert "no-cache" in r.headers["cache-control"]
        store.close()

    def test_history(self):
        client, store = self._client()
        with client:
            r = client.get("/api/devices/studio-one/history?metrics=sys.cpu_util&range=1h")
            assert r.status_code == 200
            series = r.json()["series"]["sys.cpu_util"]
            assert [round(v, 1) for _, v in series] == [0.4, 0.6]
            assert r.json()["bands"] == {}
            r = client.get("/api/devices/studio-one/history?metrics=&range=1h")
            assert r.status_code == 400
            r = client.get("/api/devices/studio-one/history?metrics=x&range=bogus")
            assert r.status_code == 400
        store.close()

    def test_history_bands_from_rollups(self):
        cfg = Config(devices=[Device(name="Studio One", host="h")])
        store = Store.in_memory(Retention(full_res_hours=1, rollup_days=30 * 365))
        states = {d.id: DeviceState(d) for d in cfg.devices}
        t0 = 1_700_000_000.0
        store.insert("studio-one", t0, "system", {"sys.cpu_util": 1.0})
        store.insert("studio-one", t0 + 60, "system", {"sys.cpu_util": 5.0})
        store.commit()
        store.rollup_and_prune(now=t0 + 2 * 3600)
        app = create_app(cfg, store, states, start_pollers=False)
        with TestClient(app) as client:
            r = client.get(
                f"/api/devices/studio-one/history"
                f"?metrics=sys.cpu_util&from_ts={t0 - 300}&to_ts={t0 + 3600}"
            )
            assert r.status_code == 200
            body = r.json()
            assert [round(v) for _, v in body["series"]["sys.cpu_util"]] == [3]
            band = body["bands"]["sys.cpu_util"]
            assert [v for _, v in band["min"]] == [1.0]
            assert [v for _, v in band["max"]] == [5.0]
        store.close()

    def test_history_rejects_bad_bounds(self):
        client, store = self._client()
        with client:
            base = "/api/devices/studio-one/history?metrics=sys.cpu_util"
            assert client.get(base + "&from_ts=1700000000").status_code == 400
            assert client.get(base + "&to_ts=1700000000").status_code == 400
            r = client.get(base + "&from_ts=1700000000&to_ts=1700001000")
            assert r.status_code == 200
            assert client.get(base + "&from_ts=200&to_ts=100").status_code == 400
            assert client.get(base + "&from_ts=100&to_ts=100").status_code == 400
            assert client.get(base + "&from_ts=nan&to_ts=200").status_code == 400
        store.close()

    def test_history_7d_includes_recent_samples(self):
        # The 7d/30d charts' from_ts sits far behind the full-res cutoff;
        # their right edge must still reach the live samples.
        cfg = Config(devices=[Device(name="Studio One", host="h")])
        store = Store.in_memory(Retention(full_res_hours=1))
        states = {d.id: DeviceState(d) for d in cfg.devices}
        now = time.time()
        store.insert("studio-one", now - 2 * 3600, "system", {"sys.cpu_util": 0.5})
        store.insert("studio-one", now - 60, "system", {"sys.cpu_util": 0.9})
        store.commit()
        store.rollup_and_prune(now=now)
        app = create_app(cfg, store, states, start_pollers=False)
        with TestClient(app) as client:
            r = client.get("/api/devices/studio-one/history?metrics=sys.cpu_util&range=7d")
            assert r.status_code == 200
            values = [v for _, v in r.json()["series"]["sys.cpu_util"]]
            assert values[-1] == pytest.approx(0.9)
            assert r.json()["bands"]["sys.cpu_util"]
        store.close()

    def test_app_shutdown_closes_store(self):
        # Lifespan teardown must await the cancelled background tasks, then
        # close the store — not close it while tasks may still write.
        client, store = self._client()
        with client:
            assert client.get("/api/devices").status_code == 200
        with pytest.raises(sqlite3.ProgrammingError):
            store.commit()
