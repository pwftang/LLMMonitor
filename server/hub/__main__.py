from __future__ import annotations

import logging

import uvicorn

from .api import create_app
from .config import load
from .db import Store
from .poll import DeviceState


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    cfg = load()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    store = Store(cfg.data_dir / "llmmonitor.sqlite3", cfg.retention)
    states = {d.id: DeviceState(d) for d in cfg.devices}
    app = create_app(cfg, store, states, start_pollers=True)
    uvicorn.run(app, host=cfg.bind, port=cfg.port, log_level="info")


if __name__ == "__main__":
    main()
