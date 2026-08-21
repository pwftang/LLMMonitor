from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


@dataclass
class Device:
    name: str
    host: str
    omlx_port: int | None = 8000
    macmon_port: int | None = 9090
    api_key: str | None = None

    @property
    def id(self) -> str:
        return _slug(self.name)

    @property
    def macmon_url(self) -> str | None:
        if not self.macmon_port:
            return None
        return f"http://{self.host}:{self.macmon_port}/json"

    @property
    def omlx_base(self) -> str | None:
        if not self.omlx_port:
            return None
        return f"http://{self.host}:{self.omlx_port}"


@dataclass
class Retention:
    full_res_hours: float = 24
    rollup_days: float = 30
    rollup_seconds: int = 300


@dataclass
class Config:
    bind: str = "127.0.0.1"
    port: int = 8400
    data_dir: Path = Path("./data")
    web_dir: Path | None = None
    poll_interval: float = 2.0
    models_interval: float = 5.0
    offline_backoff: float = 30.0
    request_timeout: float = 5.0
    retention: Retention = field(default_factory=Retention)
    devices: list[Device] = field(default_factory=list)
    mock: bool = False

    def default_web_dir(self) -> Path:
        return Path(__file__).resolve().parent.parent.parent / "web"


def _candidate_paths(explicit: str | os.PathLike | None) -> list[Path]:
    if explicit is not None:
        return [Path(explicit)]
    env = os.environ.get("HUB_CONFIG")
    if env:
        return [Path(env)]
    # default: CWD/hub.toml, else next to the repo (…/server/hub/config.py → …/hub.toml)
    project_root = Path(__file__).resolve().parent.parent.parent
    return [Path("hub.toml"), project_root / "hub.toml"]


def load(path: str | os.PathLike | None = None) -> Config:
    candidates = _candidate_paths(path)
    cfg_path = next((p for p in candidates if p.exists()), candidates[0])
    mock = os.environ.get("HUB_MOCK") == "1"
    if not cfg_path.exists():
        if mock:
            return Config(mock=True, devices=_mock_devices())
        raise SystemExit(
            f"Config not found: {cfg_path}\n"
            "Copy hub.example.toml to hub.toml and fill in your devices."
        )

    raw = tomllib.loads(cfg_path.read_text())
    retention_raw = raw.pop("retention", {})
    cfg_dir = cfg_path.resolve().parent
    cfg = Config()
    cfg.bind = raw.get("bind", cfg.bind)
    cfg.port = int(raw.get("port", cfg.port))
    cfg.data_dir = _resolve(cfg_dir, raw.get("data_dir", "./data"))
    if "web_dir" in raw:
        cfg.web_dir = _resolve(cfg_dir, raw["web_dir"])
    cfg.poll_interval = float(raw.get("poll_interval", cfg.poll_interval))
    cfg.models_interval = float(raw.get("models_interval", cfg.models_interval))
    cfg.offline_backoff = float(raw.get("offline_backoff", cfg.offline_backoff))
    cfg.request_timeout = float(raw.get("request_timeout", cfg.request_timeout))
    cfg.retention = Retention(**retention_raw)
    cfg.devices = [
        Device(
            name=d["name"],
            host=d["host"],
            omlx_port=d.get("omlx_port"),
            macmon_port=d.get("macmon_port"),
            api_key=d.get("api_key"),
        )
        for d in raw.get("devices", [])
    ]
    cfg.mock = mock
    if mock and not cfg.devices:
        cfg.devices = _mock_devices()
    if not cfg.devices:
        raise SystemExit(f"No [[devices]] configured in {cfg_path}")

    ids = [d.id for d in cfg.devices]
    if len(ids) != len(set(ids)):
        raise SystemExit("Duplicate device ids (derived from names) in config")
    return cfg


def _resolve(base: Path, p: str | os.PathLike) -> Path:
    path = Path(p)
    return path if path.is_absolute() else (base / path).resolve()


def _mock_devices() -> list[Device]:
    return [
        Device(name="Mock Studio A", host="127.0.0.1", omlx_port=None, macmon_port=None),
        Device(name="Mock Studio B", host="127.0.0.1", omlx_port=None, macmon_port=None),
        Device(name="Mock MacBook", host="127.0.0.1", omlx_port=None, macmon_port=None),
    ]
