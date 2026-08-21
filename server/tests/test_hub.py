import time

import httpx
import pytest
from fastapi.testclient import TestClient

from hub.api import create_app
from hub.config import Config, Device, Retention, load
from hub.db import Store
from hub.poll import (
    DevicePoller,
    DeviceState,
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
