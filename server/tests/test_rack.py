"""GET /api/rack + GET /rack contract tests.

Fixtures are sanitized: fake ids/names, no hosts, no keys, no prompt or
response content — the same shape the endpoint is contractually limited to.

Topology modelled here mirrors the real rack: two oMLX Mac inference servers
plus a third router-only LXC device. Route events live under the ROUTER
device's store (the hub drains them from the router's admin API), and server
panels are only the oMLX devices.
"""

import time

from fastapi.testclient import TestClient

from hub.api import create_app
from hub.config import Config, Device, Retention
from hub.db import Store
from hub.poll import DeviceState
from hub.rack import build_rack

# Sanitized route-event rows: only the fields /api/rack may echo. These are
# inserted under the router-only device ("router-1"), never a server's id.
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


def _env(*, router_on_a=False, online_a=True, online_b=True, online_router=True,
         router_active_reqs=0.0):
    """Two oMLX Mac servers + a third router-only LXC device.

    `router_on_a=True` additionally co-hosts the router config on studio-a
    (legacy single-host setup); the router-only LXC is still present. The
    router-only device has no omlx/macmon ports, so it must never become a
    server panel.
    """
    cfg = Config(devices=[
        Device(name="Studio A", host="hidden.invalid",
               router_port=8100 if router_on_a else None),
        Device(name="Studio B", host="hidden.invalid"),
        # Router-only LXC: router port only, no omlx/macmon (ports 0/None).
        Device(name="Router LXC", host="hidden.invalid", omlx_port=None,
               macmon_port=None, router_port=8200),
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
    else:
        states["studio-a"].mark_offline("unreachable")
    if online_b:
        states["studio-b"].mark_ok()
        states["studio-b"].raw["omlx_models"] = [
            {"id": "Idle-Model", "loaded": False},
            {"id": "Qwen3-4B", "loaded": True},
        ]
        states["studio-b"].llm = {"llm.active_reqs": router_active_reqs}
        states["studio-b"].llm_updated_at = now - 2
    else:
        states["studio-b"].mark_offline("unreachable")
    # The router-only LXC is the primary router host: it holds the router
    # poll payload and the drained route events.
    if online_router:
        states["router-lxc"].mark_ok()
        states["router-lxc"].raw["router"] = {"ready": ROUTER_READY,
                                              "stats": None, "models": None}
        states["router-lxc"].router_ok = True
        states["router-lxc"].router_auth_ok = True
        states["router-lxc"].router_updated_at = now - 4
    else:
        states["router-lxc"].mark_offline("unreachable")
    if online_a:
        # studio-a is NOT router-configured by default, so its per-server
        # loaded_model must come from its own local omlx model list.
        states["studio-a"].raw["omlx_models"] = [
            {"id": "Qwen3-27B", "loaded": True},
        ]
        if router_on_a:
            # Co-hosted router view is staler/partial; the LXC still wins
            # for the global router block.
            states["studio-a"].raw["router"] = {
                "ready": {"status": "degraded", "backends": {}},
                "stats": None, "models": None,
            }
            states["studio-a"].router_ok = False
            states["studio-a"].router_auth_ok = True
            states["studio-a"].router_updated_at = now - 60
    app = create_app(cfg, store, states, start_pollers=False)
    return TestClient(app), store, states, now


class TestRackContract:
    def test_router_only_device_is_not_a_server_panel(self):
        client, store, _, _ = _env()
        with client:
            body = client.get("/api/rack").json()
        assert body["schema"] == 1
        assert set(body) == {"schema", "generated_at", "stale_after_s",
                             "overall", "router", "servers"}
        # Exactly two server rows: the two oMLX Macs. The router-only LXC is
        # a real device but never a server panel.
        assert [s["id"] for s in body["servers"]] == ["studio-a", "studio-b"]
        row = body["servers"][0]
        # Stable contract: every row carries the same keys regardless of
        # availability (unknown = null, never absent, never zero-filled).
        # No per-server last_decision and no per-server router block — those
        # are global router facts now.
        expected = {
            "id", "name", "status", "active", "inference", "online",
            "last_seen_age_s", "loaded_model", "loaded_model_age_s",
            "active_requests", "active_requests_age_s",
            "gen_tps", "gen_tps_age_s", "prefill_tps", "prefill_tps_age_s",
            "model_mem_gb", "model_mem_age_s",
            "pressure_level", "mem_pressure_pct", "mem_pressure_age_s",
        }
        assert set(row) == expected
        assert set(body["servers"][1]) == expected
        store.close()

    def test_global_router_comes_from_router_only_state(self):
        client, store, _, _ = _env()
        store.insert_events("router-lxc", ROUTE_EVENTS)
        store.commit()
        with client:
            body = client.get("/api/rack").json()
        r = body["router"]
        assert r["configured"] is True
        assert r["ok"] is True and r["auth_ok"] is True
        assert r["ready"] == "ready"
        assert 0 <= r["age_s"] <= 10
        # The newest route event from the ROUTER device's store surfaces as
        # router.last_decision — sanitized fields only.
        d = r["last_decision"]
        assert set(d) == {"deployment", "ts", "age_s", "status_code"}
        assert d["deployment"] == "studio-a"
        assert d["status_code"] == 200
        assert d["ts"] == ROUTE_EVENTS[0]["recorded_at"]
        assert d["age_s"] > 0
        store.close()

    def test_router_prefers_healthy_router_state_when_co_hosted(self):
        # studio-a also has a router config but its poll is failing/stale;
        # the healthy router-only LXC must supply the global router block.
        client, store, _, _ = _env(router_on_a=True)
        store.insert_events("router-lxc", ROUTE_EVENTS)
        store.commit()
        with client:
            body = client.get("/api/rack").json()
        r = body["router"]
        assert r["ok"] is True
        assert r["ready"] == "ready"  # not studio-a's "degraded"
        assert r["last_decision"]["deployment"] == "studio-a"
        store.close()

    def test_values_and_freshness(self):
        client, store, _, now = _env()
        with client:
            body = client.get("/api/rack").json()
        a = body["servers"][0]
        assert a["status"] == "live"
        assert a["active"] is True  # 2 live requests in flight
        assert a["gen_tps"] == 41.7
        assert a["prefill_tps"] == 512.3
        assert a["active_requests"] == 2.0
        assert 0 <= a["gen_tps_age_s"] <= 10
        assert a["loaded_model"] == "Qwen3-27B"  # studio-a's own loaded omlx model
        assert a["mem_pressure_pct"] == 0.7
        assert body["overall"]["status"] == "live"
        store.close()

    def test_loaded_model_falls_back_to_local_omlx(self):
        client, store, _, _ = _env()
        with client:
            body = client.get("/api/rack").json()
        b = body["servers"][1]
        assert b["loaded_model"] == "Qwen3-4B"
        store.close()

    def test_active_uses_live_active_requests_only(self):
        # Route events naming a deployment never make a server active:
        # deployment names are not device ids and events belong to the
        # router. Only fresh live active_requests > 0 drives emphasis.
        client, store, _, _ = _env()
        store.insert_events("router-lxc", ROUTE_EVENTS)
        store.commit()
        with client:
            body = client.get("/api/rack").json()
        a, b = body["servers"]
        assert a["active"] is True   # 2 live in-flight requests
        assert b["active"] is False  # newest decision said "studio-b",
                                    # but its live active_requests is 0
        assert body["router"]["last_decision"]["deployment"] == "studio-a"
        store.close()

    def test_active_requires_live_status(self):
        # active_requests > 0 on STALE data must not light the panel.
        client, store, states, now = _env(router_active_reqs=3.0)
        states["studio-b"].last_seen = now - 3600
        states["studio-b"].llm_updated_at = now - 3600
        with client:
            body = client.get("/api/rack").json()
        b = body["servers"][1]
        assert b["status"] == "stale"
        assert b["active_requests"] == 3.0
        assert b["active"] is False
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
        assert body["router"]["configured"] is False
        assert body["router"]["last_decision"] is None
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
        store.insert_events("router-lxc", [{
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
    def test_wait_avg_from_router_device_store(self):
        cfg = Config(devices=[
            Device(name="Studio A", host="h"),
            Device(name="Router LXC", host="h", omlx_port=None,
                   macmon_port=None, router_port=8200),
        ])
        store = Store.in_memory()
        states = {d.id: DeviceState(d) for d in cfg.devices}
        states["studio-a"].mark_ok()
        rs = states["router-lxc"]
        rs.mark_ok()
        rs.router_ok = True
        rs.router_updated_at = time.time()
        now = time.time()
        # wait_avg lives under the ROUTER device's series, not a server's.
        store.insert("router-lxc", now - 60, "router", {"router.wait_avg_s": 2.5})
        store.commit()
        body = build_rack(states, store, now=now)
        assert [s["id"] for s in body["servers"]] == ["studio-a"]
        r = body["router"]
        assert r["configured"] is True
        assert r["wait_avg_s"] == 2.5
        assert 55 <= r["wait_avg_age_s"] <= 70
        store.close()

    def test_wait_avg_absent_is_none(self):
        cfg = Config(devices=[Device(name="A", host="h", router_port=8100)])
        store = Store.in_memory()
        state = DeviceState(cfg.devices[0])
        state.mark_ok()
        body = build_rack({state.device.id: state}, store)
        r = body["router"]
        assert r["wait_avg_s"] is None
        assert r["wait_avg_age_s"] is None
        assert r["last_decision"] is None
        store.close()
