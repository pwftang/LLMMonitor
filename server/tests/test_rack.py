"""GET /api/rack + GET /rack contract tests.

Fixtures are sanitized: fake ids/names, no hosts, no keys, no prompt or
response content — the same shape the endpoint is contractually limited to.
"""

import time

from fastapi.testclient import TestClient

from hub.api import create_app
from hub.config import Config, Device, Retention
from hub.db import Store
from hub.poll import DeviceState
from hub.rack import build_rack

# Sanitized route-event rows: only the fields /api/rack may echo.
ROUTE_EVENTS = [
    {
        "request_id": "req-2",
        "recorded_at": 1_757_548_900.0,
        "deployment": "studio-a",
        "status_code": 200,
    },
    {
        "request_id": "req-1",
        "recorded_at": 1_757_548_800.0,
        "deployment": "studio-b",
        "status_code": 502,
    },
]

ROUTER_READY = {
    "status": "ready",
    "backends": {
        "studio-a": {
            "healthy": True,
            "models": ["Qwen3-27B"],
            "default_model": "Qwen3-27B",
        },
        "studio-b": {
            "healthy": False,
            "models": [],
            "default_model": None,
        },
    },
}


def _env(*, router_on_a=True, online_a=True, online_b=True):
    """Two inference servers; A optionally carries the router."""
    cfg = Config(devices=[
        Device(name="Studio A", host="hidden.invalid",
               router_port=8100 if router_on_a else None),
        Device(name="Studio B", host="hidden.invalid"),
    ])
    store = Store.in_memory(Retention())
    states = {d.id: DeviceState(d) for d in cfg.devices}
    now = time.time()
    if online_a:
        states["studio-a"].mark_ok()
        states["studio-a"].llm = {
            "llm.gen_tps": 41.7, "llm.prefill_tps": 512.3,
            "llm.active_reqs": 2.0, "llm.model_mem_gb": 18.4,
        }
        states["studio-a"].llm_updated_at = now - 3
        states["studio-a"].pressure_level = "normal"
        states["studio-a"].system = {"sys.mem_pressure_pct": 0.7}
        states["studio-a"].macmon_last_ok = now - 5
        states["studio-a"].raw["router"] = {"ready": ROUTER_READY,
                                           "stats": None, "models": None}
        states["studio-a"].router_ok = True
        states["studio-a"].router_auth_ok = True
        states["studio-a"].router_updated_at = now - 4
    else:
        states["studio-a"].mark_offline("unreachable")
    if online_b:
        states["studio-b"].mark_ok()
        states["studio-b"].raw["omlx_models"] = [
            {"id": "Idle-Model", "loaded": False},
            {"id": "Qwen3-4B", "loaded": True},
        ]
        states["studio-b"].llm_updated_at = now - 2
    else:
        states["studio-b"].mark_offline("unreachable")
    app = create_app(cfg, store, states, start_pollers=False)
    return TestClient(app), store, states, now


class TestRackContract:
    def test_shape_and_stable_keys(self):
        client, store, _, _ = _env()
        with client:
            body = client.get("/api/rack").json()
        assert body["schema"] == 1
        assert set(body) == {"schema", "generated_at", "stale_after_s",
                             "overall", "router", "servers"}
        assert [s["id"] for s in body["servers"]] == ["studio-a", "studio-b"]
        row = body["servers"][0]
        # Stable contract: every row carries the same keys regardless of
        # availability (unknown = null, never absent, never zero-filled).
        expected = {
            "id", "name", "status", "active", "inference", "online",
            "last_seen_age_s", "loaded_model", "loaded_model_age_s",
            "active_requests", "active_requests_age_s",
            "gen_tps", "gen_tps_age_s", "prefill_tps", "prefill_tps_age_s",
            "model_mem_gb", "model_mem_age_s",
            "pressure_level", "mem_pressure_pct", "mem_pressure_age_s",
            "last_decision", "router",
        }
        assert set(row) == expected
        assert set(body["servers"][1]) == expected
        store.close()

    def test_values_and_freshness(self):
        client, store, _, now = _env()
        with client:
            body = client.get("/api/rack").json()
        a = body["servers"][0]
        assert a["status"] == "live"
        assert a["active"] is True  # 2 requests in flight
        assert a["gen_tps"] == 41.7
        assert a["prefill_tps"] == 512.3
        assert a["active_requests"] == 2.0
        assert 0 <= a["gen_tps_age_s"] <= 10
        assert a["loaded_model"] == "Qwen3-27B"  # router's healthy backend default
        assert a["mem_pressure_pct"] == 0.7
        assert body["overall"]["status"] == "live"
        r = a["router"]
        assert r["configured"] is True and r["ok"] is True and r["ready"] == "ready"
        store.close()

    def test_loaded_model_falls_back_to_local_omlx(self):
        client, store, _, _ = _env(router_on_a=False)
        with client:
            body = client.get("/api/rack").json()
        b = body["servers"][1]
        assert b["loaded_model"] == "Qwen3-4B"
        assert b["router"] is None
        store.close()

    def test_last_decision_is_newest_sanitized_event(self):
        client, store, _, _ = _env()
        store.insert_events("studio-a", ROUTE_EVENTS)
        store.commit()
        with client:
            body = client.get("/api/rack").json()
        d = body["servers"][0]["last_decision"]
        assert d["deployment"] == "studio-a"
        assert d["status_code"] == 200
        assert set(d) == {"deployment", "ts", "age_s", "status_code"}
        store.close()

    def test_active_follows_last_decision(self):
        client, store, _, _ = _env()
        # studio-b's newest decision landed on itself: studio-b is the
        # active panel even with no in-flight count known there.
        store.insert_events("studio-a", [ROUTE_EVENTS[0]])
        store.insert_events("studio-b", [ROUTE_EVENTS[1]])
        store.commit()
        with client:
            body = client.get("/api/rack").json()
        a, b = body["servers"]
        assert a["active"] is True   # also has 2 in-flight
        assert b["active"] is True   # newest decision landed here
        store.close()

    def test_offline_row_keeps_history_but_is_never_live(self):
        client, store, _, _ = _env(online_a=False)
        with client:
            body = client.get("/api/rack").json()
        a = body["servers"][0]
        assert a["status"] == "offline"
        assert a["online"] is False
        assert a["active"] is False
        assert a["gen_tps"] is None  # llm dict was never populated offline
        assert a["gen_tps_age_s"] is None
        assert body["overall"]["status"] == "live"  # B still fresh
        store.close()

    def test_all_offline_overall(self):
        client, store, _, _ = _env(online_a=False, online_b=False)
        with client:
            body = client.get("/api/rack").json()
        assert body["overall"]["status"] == "offline"
        assert all(s["status"] == "offline" for s in body["servers"])
        store.close()

    def test_stale_when_last_seen_ages_out(self):
        client, store, states, now = _env()
        states["studio-a"].last_seen = now - 3600
        states["studio-a"].llm_updated_at = now - 3600  # metrics aged too
        with client:
            body = client.get("/api/rack").json()
        a = body["servers"][0]
        assert a["status"] == "stale"
        assert a["active"] is False  # never "active" on stale data
        assert a["gen_tps"] == 41.7  # historical value still shown…
        assert a["gen_tps_age_s"] > 3500  # …but honestly aged
        store.close()

    def test_never_polled_is_unknown_not_zero(self):
        cfg = Config(devices=[Device(name="Ghost", host="hidden.invalid")])
        store = Store.in_memory()
        states = {d.id: DeviceState(d) for d in cfg.devices}
        app = create_app(cfg, store, states, start_pollers=False)
        with TestClient(app) as client:
            body = client.get("/api/rack").json()
        g = body["servers"][0]
        assert g["status"] == "offline"
        assert g["last_seen_age_s"] is None
        assert g["active_requests"] is None
        assert body["overall"]["status"] == "unknown"
        store.close()

    def test_no_upstream_calls_during_request(self):
        # /api/rack must not touch the network. Patch httpx at the seam the
        # pollers use; any call would raise.
        import socket
        client, store, _, _ = _env()
        real = socket.socket

        def no_net(*a, **k):
            raise AssertionError("rack request attempted a network call")

        import unittest.mock as m
        with client, m.patch.object(socket, "socket", no_net), \
                m.patch.object(__import__("httpx").AsyncClient, "request",
                               side_effect=AssertionError("http call")):
            r = client.get("/api/rack")
            assert r.status_code == 200
        assert real is socket.socket
        store.close()

    def test_sanitization_no_secrets_or_hosts(self):
        client, store, states, _ = _env()
        # Poison the state with things that must never surface.
        states["studio-a"].raw["omlx_stats"] = {"api_key": "SECRET-KEY"}
        states["studio-a"].device.api_key = "SECRET-KEY"
        store.insert_events("studio-a", [{
            "request_id": "req-x", "recorded_at": time.time(),
            "deployment": "studio-a", "status_code": 200,
            "prompt": "PROMPT-LEAK", "response_body": "RESPONSE-LEAK",
        }])
        store.commit()
        with client:
            text = client.get("/api/rack").text
        for banned in ("SECRET-KEY", "hidden.invalid", "PROMPT-LEAK",
                       "RESPONSE-LEAK", "request_id", "tailnet"):
            assert banned not in text
        store.close()


class TestRackPage:
    def test_serves_dedicated_page_no_cache(self):
        client, store, _, _ = _env()
        with client:
            r = client.get("/rack")
            assert r.status_code == 200
            assert "no-cache" in r.headers["cache-control"]
            assert "rack.js" in r.text and "rack.css" in r.text
            # Dedicated lightweight page: no charts bundle, no module app.js.
            assert "charts.js" not in r.text
        store.close()

    def test_assets_reachable(self):
        client, store, _, _ = _env()
        with client:
            for path in ("/rack.css", "/rack.js"):
                r = client.get(path)
                assert r.status_code == 200
                assert "no-cache" in r.headers["cache-control"]
        store.close()


class TestRackAggregator:
    def test_wait_avg_from_store_with_age(self):
        cfg = Config(devices=[Device(name="A", host="h", router_port=8100)])
        store = Store.in_memory()
        state = DeviceState(cfg.devices[0])
        state.mark_ok()
        state.router_ok = True
        state.router_updated_at = time.time()
        now = time.time()
        store.insert("a", now - 60, "router", {"router.wait_avg_s": 2.5})
        store.commit()
        body = build_rack({state.device.id: state}, store, now=now)
        r = body["servers"][0]["router"]
        assert r["wait_avg_s"] == 2.5
        assert 55 <= r["wait_avg_age_s"] <= 70
        store.close()

    def test_wait_avg_absent_is_none(self):
        cfg = Config(devices=[Device(name="A", host="h", router_port=8100)])
        store = Store.in_memory()
        state = DeviceState(cfg.devices[0])
        state.mark_ok()
        body = build_rack({state.device.id: state}, store)
        r = body["servers"][0]["router"]
        assert r["wait_avg_s"] is None
        assert r["wait_avg_age_s"] is None
        store.close()
