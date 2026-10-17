"""Rack-dashboard aggregation for GET /api/rack.

Pure function over state the hub ALREADY holds (DeviceState snapshots, the
SQLite store's route_events, and poll timestamps). It performs NO network
I/O: the rack view must stay useful when devices are offline, and a kiosk
refresh must never fan out into upstream polls.

Truthfulness rules baked into the contract:

- Every metric value is paired with an ``*_age_s`` (seconds since that value
  was last refreshed). A null age means "never observed" — never zero.
- ``status`` is one of ``live`` (fresh), ``stale`` (known but older than
  ``stale_after_s``), ``offline`` (device unreachable), ``unknown`` (never
  polled). A completed or historical value is never labelled live/current.
- Only sanitized scalars are emitted: no hosts/IPs, no credentials, no
  prompts, responses, or raw request payloads.
"""

from __future__ import annotations

import time
from typing import Any

from .db import Store
from .poll import DeviceState

SCHEMA_VERSION = 1

# A value younger than this is "live"; older is "stale". The hub polls every
# poll_interval seconds, so this is a few missed polls of slack.
DEFAULT_STALE_AFTER_S = 30.0


def _num(d: dict, key: str) -> float | None:
    v = d.get(key)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    return None


def _age(now: float, ts: float | None) -> float | None:
    """Seconds since ts; None when ts is missing/invalid (never 0)."""
    if not isinstance(ts, (int, float)) or ts <= 0:
        return None
    return max(0.0, now - float(ts))


def _server_row(state: DeviceState, store: Store, now: float, stale_after_s: float) -> dict:
    llm = state.llm or {}
    system = state.system or {}

    seen_age = _age(now, state.last_seen)
    llm_age = _age(now, state.llm_updated_at)
    router_age = _age(now, state.router_updated_at)

    if not state.online:
        status = "offline"
    elif seen_age is None or seen_age > stale_after_s:
        status = "stale"
    else:
        status = "live"

    inference = state.device.omlx_base is not None

    # Loaded model: prefer the router's own view (default_model of a healthy
    # backend), else the newest locally-loaded omlx model. Both are
    # point-in-time snapshots stamped with the router/models poll time.
    loaded_model: str | None = None
    loaded_age = router_age
    raw_router = state.raw.get("router")
    if isinstance(raw_router, dict):
        backends = (raw_router.get("ready") or {}).get("backends")
        if isinstance(backends, dict):
            for b in backends.values():
                if not isinstance(b, dict) or not b.get("healthy"):
                    continue
                default = b.get("default_model")
                if isinstance(default, str) and default:
                    loaded_model = default
                    break
                models = b.get("models")
                if isinstance(models, list) and models and isinstance(models[0], str):
                    loaded_model = models[0]
                    break
    if loaded_model is None:
        models = state.raw.get("omlx_models")
        if isinstance(models, list):
            for m in reversed(models):  # omlx lists loaded models last
                if isinstance(m, dict) and m.get("loaded") and isinstance(m.get("id"), str):
                    loaded_model = m["id"]
                    loaded_age = llm_age  # stamped by the models loop, ~llm cadence
                    break

    # Last routing decision: newest sanitized route event in the hub's DB.
    last_decision: dict | None = None
    try:
        events = store.recent_events(state.device.id, 10)
    except Exception:  # pragma: no cover — defensive; DB read shouldn't raise
        events = []
    for ev in events:
        dep = ev.get("deployment")
        if isinstance(dep, str) and dep:
            last_decision = {
                "deployment": dep,
                "ts": ev.get("recorded_at"),
                "age_s": _age(now, ev.get("recorded_at")),
                "status_code": ev.get("status_code")
                if isinstance(ev.get("status_code"), (int, float))
                else None,
            }
            break

    # "Active" = this server is the one serving right now: the newest routed
    # decision landed on it, or it has requests in flight. Deliberately
    # conservative: never active while offline/stale.
    active_requests = _num(llm, "llm.active_reqs")
    active = bool(
        inference
        and status == "live"
        and ((active_requests or 0) > 0
             or (last_decision is not None and last_decision["deployment"] == state.device.id))
    )

    router: dict | None = None
    if state.device.router_url is not None:
        ready_status: str | None = None
        if isinstance(raw_router, dict):
            rs = (raw_router.get("ready") or {}).get("status")
            if isinstance(rs, str):
                ready_status = rs
        # Recent average queue wait (router wait_metrics), a TTFT proxy. It
        # lives only in the store's router series; take the newest point in
        # the last hour and stamp its real age — never presented as a live
        # per-request TTFT.
        wait_avg: float | None = None
        wait_age: float | None = None
        try:
            pts = store.history(
                state.device.id, ["router.wait_avg_s"], now - 3600, now
            ).get("router.wait_avg_s") or []
            if pts:
                wait_ts, wait_avg = pts[-1]
                wait_age = _age(now, wait_ts)
        except Exception:  # pragma: no cover — defensive DB read
            pass
        router = {
            "configured": True,
            "ok": state.router_ok,
            "ready": ready_status,
            "auth_ok": state.router_auth_ok,
            "wait_avg_s": wait_avg,
            "wait_avg_age_s": wait_age,
            "age_s": router_age,
        }

    return {
        "id": state.device.id,
        "name": state.device.name,
        "status": status,
        "active": active,
        "inference": inference,
        "online": state.online,
        "last_seen_age_s": seen_age,
        "loaded_model": loaded_model,
        "loaded_model_age_s": loaded_age,
        "active_requests": active_requests,
        "active_requests_age_s": llm_age,
        "gen_tps": _num(llm, "llm.gen_tps"),
        "gen_tps_age_s": llm_age,
        "prefill_tps": _num(llm, "llm.prefill_tps"),
        "prefill_tps_age_s": llm_age,
        "model_mem_gb": _num(llm, "llm.model_mem_gb"),
        "model_mem_age_s": llm_age,
        "pressure_level": state.pressure_level,
        "mem_pressure_pct": _num(system, "sys.mem_pressure_pct"),
        "mem_pressure_age_s": _age(now, state.macmon_last_ok),
        "last_decision": last_decision,
        "router": router,
    }


def build_rack(
    states: dict[str, DeviceState],
    store: Store,
    *,
    now: float | None = None,
    stale_after_s: float = DEFAULT_STALE_AFTER_S,
) -> dict:
    """Aggregate the rack snapshot. Pure: reads memory + SQLite only."""
    now = time.time() if now is None else now
    servers = [
        _server_row(states[k], store, now, stale_after_s)
        for k in sorted(states)
    ]

    seen_ages = [s["last_seen_age_s"] for s in servers if s["last_seen_age_s"] is not None]
    attempted = any(
        states[k].last_attempt is not None for k in states
    )
    overall_age = min(seen_ages) if seen_ages else None
    if not any(s["online"] for s in servers):
        # Every server known-unreachable: offline once we've polled at least
        # once, otherwise "unknown" (never polled).
        overall = "offline" if attempted else "unknown"
    elif overall_age is None or overall_age > stale_after_s:
        overall = "stale"
    else:
        overall = "live"

    router: dict = {"configured": False, "ok": None, "ready": None,
                    "auth_ok": None, "age_s": None}
    for s in servers:
        if s["router"] and (router["ok"] is not True):
            router = s["router"]
    router = dict(router)
    router["stale_after_s"] = stale_after_s

    return {
        "schema": SCHEMA_VERSION,
        "generated_at": now,
        "stale_after_s": stale_after_s,
        "overall": {"status": overall, "age_s": overall_age},
        "router": router,
        "servers": servers,
    }
