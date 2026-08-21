from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import Config
from .db import Store
from .poll import DevicePoller, DeviceState

log = logging.getLogger("hub.api")


class _NoCacheStaticFiles(StaticFiles):
    """Static files with Cache-Control: no-cache so browsers revalidate
    app.js/charts.js on every load instead of serving a stale bundle."""

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


RANGE_SECONDS = {
    "1h": 3600,
    "3h": 3 * 3600,
    "6h": 6 * 3600,
    "24h": 24 * 3600,
    "7d": 7 * 86400,
    "30d": 30 * 86400,
}


def create_app(cfg: Config, store: Store, states: dict[str, DeviceState], *, start_pollers: bool = False) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks: list[asyncio.Task] = []
        if start_pollers:
            from .mock import MockPoller

            for dev_id, state in states.items():
                poller_cls = MockPoller if cfg.mock else DevicePoller
                tasks.append(asyncio.create_task(
                    poller_cls(state.device, cfg, store, state).run()
                ))
        tasks.append(asyncio.create_task(_commit_loop(store)))
        tasks.append(asyncio.create_task(_rollup_loop(store)))
        yield
        for t in tasks:
            t.cancel()
        store.close()

    app = FastAPI(title="LLMMonitor hub", lifespan=lifespan)

    @app.get("/api/devices")
    async def list_devices() -> JSONResponse:
        return JSONResponse([s.public() for s in states.values()])

    @app.get("/api/devices/{dev_id}/latest")
    async def device_latest(dev_id: str) -> JSONResponse:
        state = states.get(dev_id)
        if not state:
            raise HTTPException(404, f"unknown device: {dev_id}")
        return JSONResponse(state.public())

    @app.get("/api/devices/{dev_id}/history")
    async def device_history(
        dev_id: str,
        metrics: str = Query(..., description="comma-separated metric names"),
        range: str = Query("1h"),
        from_ts: float | None = Query(None),
        to_ts: float | None = Query(None),
    ) -> JSONResponse:
        if dev_id not in states:
            raise HTTPException(404, f"unknown device: {dev_id}")
        now = time.time()
        if from_ts is None or to_ts is None:
            span = RANGE_SECONDS.get(range)
            if span is None:
                raise HTTPException(400, f"unknown range: {range} (use one of {sorted(RANGE_SECONDS)})")
            from_ts, to_ts = now - span, now
        metric_list = [m.strip() for m in metrics.split(",") if m.strip()]
        if not metric_list:
            raise HTTPException(400, "no metrics requested")
        if len(metric_list) > 64:
            raise HTTPException(400, "too many metrics in one request")
        data, bands = store.history_with_bands(dev_id, metric_list, from_ts, to_ts)
        return JSONResponse({"device": dev_id, "from": from_ts, "to": to_ts, "series": data, "bands": bands})

    web_dir = cfg.web_dir or cfg.default_web_dir()
    index = Path(web_dir) / "index.html"
    if index.exists():
        app.mount("/", _NoCacheStaticFiles(directory=str(web_dir), html=True), name="web")
    else:
        @app.get("/")
        async def no_frontend() -> dict:
            return {"error": f"frontend not found at {index}", "api": "see /api/devices"}

    return app


async def _commit_loop(store: Store) -> None:
    while True:
        await asyncio.sleep(5)
        store.commit()


async def _rollup_loop(store: Store) -> None:
    while True:
        await asyncio.sleep(3600)
        try:
            stats = store.rollup_and_prune()
            log.info("rollup+prune: %s", stats)
        except Exception:
            log.exception("rollup+prune failed")
