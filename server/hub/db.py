"""SQLite time-series store.

Samples are stored as compact JSON dicts of {metric: value} extracted at poll
time (``samples`` table). A background job folds them into 5-minute min/avg/max
buckets (``rollups`` table) and prunes by the configured retention windows.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Iterable

from .config import Retention

MAX_POINTS = 2000  # per-series point budget for history queries

_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    device  TEXT NOT NULL,
    ts      REAL NOT NULL,
    kind    TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_samples_dev_ts ON samples (device, ts);

CREATE TABLE IF NOT EXISTS rollups (
    device    TEXT NOT NULL,
    bucket_ts INTEGER NOT NULL,
    metric    TEXT NOT NULL,
    avg       REAL,
    min       REAL,
    max       REAL,
    n         INTEGER NOT NULL,
    PRIMARY KEY (device, bucket_ts, metric)
);
CREATE INDEX IF NOT EXISTS idx_rollups_lookup ON rollups (device, metric, bucket_ts);
"""


class Store:
    def __init__(self, path, retention: Retention):
        self.path = path
        self.retention = retention
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)

    @classmethod
    def in_memory(cls, retention: Retention | None = None) -> "Store":
        return cls(":memory:", retention or Retention())

    def insert(self, device: str, ts: float, kind: str, metrics: dict[str, float]) -> None:
        if not metrics:
            return
        payload = json.dumps(metrics, separators=(",", ":"))
        with self._lock:
            self._conn.execute(
                "INSERT INTO samples (device, ts, kind, payload) VALUES (?, ?, ?, ?)",
                (device, ts, kind, payload),
            )

    def commit(self) -> None:
        with self._lock:
            self._conn.commit()

    def rollup_and_prune(self, now: float | None = None) -> dict:
        """Fold raw samples older than the full-res window into buckets."""
        now = now or time.time()
        r = self.retention
        full_res_cutoff = now - r.full_res_hours * 3600
        rollup_cutoff = now - r.rollup_days * 86400
        # Only buckets fully inside the past-full-res region are rolled up.
        horizon_bucket = int(full_res_cutoff // r.rollup_seconds) * r.rollup_seconds

        rolled = pruned_samples = pruned_rollups = 0
        with self._lock:
            devices = [
                row[0] for row in self._conn.execute("SELECT DISTINCT device FROM samples")
            ]
            for device in devices:
                last = self._conn.execute(
                    "SELECT MAX(bucket_ts) FROM rollups WHERE device = ?", (device,)
                ).fetchone()[0]
                start = (
                    last + r.rollup_seconds
                    if last is not None
                    else int(
                        self._earliest_sample_ts(device) // r.rollup_seconds * r.rollup_seconds
                    )
                )
                for bucket in range(start, horizon_bucket, r.rollup_seconds):
                    rolled += self._rollup_bucket(device, bucket)

            cur = self._conn.execute(
                "DELETE FROM samples WHERE ts < ?", (full_res_cutoff,)
            )
            pruned_samples = cur.rowcount
            cur = self._conn.execute(
                "DELETE FROM rollups WHERE bucket_ts < ?", (int(rollup_cutoff),)
            )
            pruned_rollups = cur.rowcount
            self._conn.commit()
        return {
            "rollups_written": rolled,
            "samples_pruned": pruned_samples,
            "rollups_pruned": pruned_rollups,
        }

    def history(
        self, device: str, metrics: Iterable[str], from_ts: float, to_ts: float
    ) -> dict[str, list[list[float]]]:
        """Return {metric: [[ts, value], ...]}, sourcing rollups or samples."""
        full_res_cutoff = time.time() - self.retention.full_res_hours * 3600
        if from_ts < full_res_cutoff:
            return self._history_rollups(device, metrics, from_ts, to_ts)
        return self._history_samples(device, metrics, from_ts, to_ts)

    def _history_rollups(self, device, metrics, from_ts, to_ts):
        out: dict[str, list[list[float]]] = {m: [] for m in metrics}
        placeholders = ",".join("?" for _ in metrics)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT metric, bucket_ts, avg FROM rollups "
                f"WHERE device = ? AND metric IN ({placeholders}) "
                f"AND bucket_ts BETWEEN ? AND ? ORDER BY bucket_ts",
                (device, *metrics, int(from_ts), int(to_ts)),
            ).fetchall()
        for metric, bucket_ts, avg in rows:
            out[metric].append([bucket_ts, avg])
        return out

    def _history_samples(self, device, metrics, from_ts, to_ts):
        wanted = set(metrics)
        acc: dict[str, list[list[float]]] = {m: [] for m in metrics}
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, payload FROM samples WHERE device = ? AND ts BETWEEN ? AND ? "
                "ORDER BY ts",
                (device, from_ts, to_ts),
            ).fetchall()
        for ts, payload in rows:
            data = json.loads(payload)
            for m in wanted & data.keys():
                acc[m].append([ts, data[m]])
        return {m: _decimate(points, MAX_POINTS) for m, points in acc.items()}

    def _earliest_sample_ts(self, device: str) -> float:
        row = self._conn.execute(
            "SELECT MIN(ts) FROM samples WHERE device = ?", (device,)
        ).fetchone()
        return row[0] if row and row[0] is not None else time.time()

    def _rollup_bucket(self, device: str, bucket_ts: int) -> int:
        r = self.retention
        rows = self._conn.execute(
            "SELECT payload FROM samples WHERE device = ? AND ts >= ? AND ts < ?",
            (device, bucket_ts, bucket_ts + r.rollup_seconds),
        ).fetchall()
        if not rows:
            return 0
        metrics: dict[str, list[float]] = {}
        for (payload,) in rows:
            for k, v in json.loads(payload).items():
                metrics.setdefault(k, []).append(v)
        self._conn.executemany(
            "INSERT OR REPLACE INTO rollups (device, bucket_ts, metric, avg, min, max, n) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (device, bucket_ts, m, sum(vs) / len(vs), min(vs), max(vs), len(vs))
                for m, vs in metrics.items()
            ],
        )
        return len(metrics)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _decimate(points: list[list[float]], max_points: int) -> list[list[float]]:
    """Bucket-average downsampling to keep canvas rendering cheap."""
    if len(points) <= max_points:
        return points
    bucket_size = len(points) / max_points
    out = []
    for i in range(max_points):
        chunk = points[int(i * bucket_size) : int((i + 1) * bucket_size)] or [points[-1]]
        ts = sum(p[0] for p in chunk) / len(chunk)
        v = sum(p[1] for p in chunk) / len(chunk)
        out.append([ts, v])
    return out
