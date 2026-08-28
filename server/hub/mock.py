"""Synthetic telemetry for HUB_MOCK=1 — dev and demo without real devices."""

from __future__ import annotations

import asyncio
import math
import random
import time

from .config import Config, Device
from .db import Store
from .poll import DeviceState


class _Walk:
    """Mean-reverting random walk clamped to [lo, hi]."""

    def __init__(self, lo: float, hi: float, start: float, step: float):
        self.lo, self.hi, self.v, self.step = lo, hi, start, step

    def next(self, drift: float = 0.0) -> float:
        self.v += random.uniform(-self.step, self.step) + (drift - self.v) * 0.02
        self.v = max(self.lo, min(self.hi, self.v))
        return self.v


MODELS = [
    ("mlx-community/Qwen3-32B-4bit", 18.4),
    ("mlx-community/Llama-3.3-70B-4bit", 38.9),
    ("mlx-community/Qwen2.5-Coder-14B-8bit", 15.1),
]

# Installed but never loaded — exercises the "Available models" drill-down panel.
AVAILABLE_MODELS = [
    ("mlx-community/Gemma-3-12B-4bit", 7.3),
    ("mlx-community/Phi-4-14B-4bit", 8.9),
    ("mlx-community/Mistral-Small-3.2-24B-6bit", 19.2),
]


class MockPoller:
    def __init__(self, device: Device, cfg: Config, store: Store, state: DeviceState):
        self.device, self.cfg, self.store, self.state = device, cfg, store, state
        rng = random.Random(device.id)
        random.Random()  # keep global rng for simplicity of interpolated phases
        self.available = AVAILABLE_MODELS
        self.phase = rng.uniform(0, 10)
        self.cpu = _Walk(0.02, 0.95, 0.15, 0.06)
        self.gpu = _Walk(0.0, 1.0, 0.3, 0.10)
        self.gen_tps = _Walk(0.0, 60.0, 12.0, 8.0)
        self.prefill_tps = _Walk(0.0, 900.0, 80.0, 90.0)
        self.cached = _Walk(0.0, 2_000_000.0, 400_000.0, 60_000.0)
        self.prompt_tokens = rng.uniform(200_000, 800_000)
        self.ram_gb = 128.0 if "studio" in device.id else 32.0
        self.models = rng.sample(MODELS, k=rng.randint(1, 2))
        # The "macbook" mock cycles offline to exercise the offline UI.
        self.flaps = "mac" in device.id and "studio" not in device.id
        self.t = 0.0

    async def run(self) -> None:
        self.state.omlx_version = "0.6.2"
        self.state.macmon_ok = True
        # Nominal addresses so the card header shows both IP rows
        octet = sum(map(ord, self.device.id)) % 200 + 10
        self.state.tailscale_ip = f"100.64.0.{octet}"
        self.state.local_ip = f"192.168.0.{octet}"
        while True:
            self.t += self.cfg.poll_interval
            if self.flaps and (self.t % 300.0) > 180.0:
                self.state.mark_offline("device asleep (mock)")
                await asyncio.sleep(self.cfg.poll_interval)
                continue
            self.state.mark_ok()
            now = time.time()
            busy = 0.5 + 0.5 * math.sin(self.t / 90 + self.phase)
            system = self._system(busy)
            llm = self._llm(busy)
            models_payload, am_models = self._models(busy > 0.35, busy, llm)
            llm["llm.active_reqs"] = float(sum(e["active_requests"] for e in am_models))
            llm["llm.waiting_reqs"] = float(sum(len(e["waiting"]) for e in am_models))
            self.state.system = system
            self.state.llm = llm
            self.state.pressure_level = "normal" if busy < 0.8 else "warning"
            self.store.insert(self.device.id, now, "system", system)
            self.store.insert(self.device.id, now, "llm", llm)
            self.state.raw["omlx_models"] = models_payload
            self.state.raw["omlx_stats"] = {
                "avg_prefill_tps": llm["llm.prefill_tps"],
                "avg_generation_tps": llm["llm.gen_tps"],
                "total_cached_tokens": llm["llm.cached_tokens"],
                "cache_efficiency": llm["llm.cache_eff"],
                "total_prompt_tokens": llm["llm.prompt_tokens"],
                "model_memory_used": int(llm["llm.model_mem_gb"] * (1024**3)),
                "pressure_level": self.state.pressure_level,
                "total_active_requests": llm["llm.active_reqs"],
                "total_waiting_requests": llm["llm.waiting_reqs"],
                "active_models": {
                    "models": am_models,
                    "total_active_requests": int(llm["llm.active_reqs"]),
                    "total_waiting_requests": int(llm["llm.waiting_reqs"]),
                },
            }
            model_series = {}
            for m in models_payload:
                slug = m["id"].lower().replace("/", "-").replace(".", "-").replace("_", "-")
                model_series[f"llm.model.{slug}.mem_gb"] = m["actual_size"] / (1024**3)
            self.store.insert(self.device.id, now, "models", model_series)
            await asyncio.sleep(self.cfg.poll_interval)

    def _models(self, working: bool, busy: float, llm: dict) -> tuple[list[dict], list[dict]]:
        """Per-model payload + active_models entries. The first model cycles
        prompt processing (0-20 s) -> generating (20-40 s) -> idle (40-60 s)."""
        phase = (self.t % 60.0) / 20.0
        epoch = int(self.t // 60)
        payload, am_models = [], []
        for i, (name, gb) in enumerate(self.models):
            entry = {
                "id": name,
                "actual_size": int(gb * 1024**3),
                "pinned": i == 0,
                "is_loading": False,
                "active_requests": 0,
                "idle_seconds": None,
                "ttl_remaining_seconds": None,
                "prefilling": [],
                "generating": [],
                "waiting": [],
            }
            if i == 0 and working:
                if phase < 1.0:
                    total = 4096
                    processed = int(total * (0.1 + 0.85 * phase))
                    entry["active_requests"] = 1
                    entry["prefilling"] = [{
                        "request_id": f"mock-{epoch}-pp",
                        "processed": processed,
                        "total": total,
                        "speed": 600 + 700 * busy,
                        "eta": max(0.0, (total - processed) / 1000),
                        "elapsed": 1.0 + phase * 8,
                    }]
                elif phase < 2.0:
                    entry["active_requests"] = 1
                    entry["generating"] = [{
                        "request_id": f"mock-{epoch}-gen",
                        "elapsed_seconds": 4.0 + (phase - 1) * 16,
                        "generated_tokens": 50 + int((phase - 1) * 300),
                        "tokens_per_second": max(1.0, llm["llm.gen_tps"]),
                        "last_activity_age_seconds": 0.05,
                        "prompt_tokens": 512,
                        "max_tokens": 2048,
                    }]
                else:
                    entry["idle_seconds"] = (phase - 2.0) * 20.0
            else:
                entry["idle_seconds"] = float(self.t % 600.0)
                if not entry["pinned"]:
                    entry["ttl_remaining_seconds"] = max(0.0, 600.0 - (self.t % 600.0))
            if busy > 0.85 and i == 0:
                entry["waiting"] = [{
                    "request_id": f"mock-{epoch}-wait",
                    "queue_position": 1,
                    "elapsed_seconds": float(self.t % 10),
                    "prompt_tokens": 1024,
                }]
            am_models.append(entry)
            payload.append({
                "id": name,
                "loaded": True,
                "is_loading": False,
                "actual_size": entry["actual_size"],
                "pinned": entry["pinned"],
                "active_requests": entry["active_requests"],
            })
        for name, gb in self.available:
            payload.append({
                "id": name,
                "loaded": False,
                "is_loading": False,
                "actual_size": int(gb * 1024**3),
                "pinned": False,
                "active_requests": 0,
            })
        return payload, am_models

    def _system(self, busy: float) -> dict[str, float]:
        cpu = self.cpu.next(busy * 0.7)
        gpu = self.gpu.next(busy * 0.85)
        model_mem = sum(gb for _, gb in self.models)
        ram_used = 14.0 + model_mem + busy * 20
        cpu_power = 2 + cpu * 38
        gpu_power = 0.5 + gpu * 45
        return {
            "sys.cpu_util": cpu,
            "sys.gpu_util": gpu,
            "sys.cpu_power_w": cpu_power,
            "sys.gpu_power_w": gpu_power,
            "sys.ane_power_w": 0.2,
            "sys.all_power_w": cpu_power + gpu_power + 3.0,
            "sys.sys_power_w": cpu_power + gpu_power + 22.0,
            "sys.ram_power_w": 4.0 + ram_used * 0.08,
            "sys.cpu_temp_c": 38 + cpu * 42,
            "sys.gpu_temp_c": 35 + gpu * 40,
            "sys.ram_total_gb": self.ram_gb,
            "sys.ram_used_gb": min(ram_used, self.ram_gb * 0.97),
            "sys.ram_used_pct": min(ram_used / self.ram_gb, 0.97),
            "sys.swap_used_gb": max(0.0, busy * 4 - 1.5),
            "sys.fan_rpm_max": 900 + busy * 1800,
            "sys.fan_count": 2.0,
            "sys.ecpu_freq_mhz": 600 + cpu * 2000,
            "sys.pcpu_freq_mhz": 600 + cpu * 3900,
            "sys.gpu_freq_mhz": 400 + gpu * 1100,
        }

    def _llm(self, busy: float) -> dict[str, float]:
        active = max(0, round(busy * 6 + random.uniform(-2, 1)))
        gen = self.gen_tps.next(45 if active else 0.0)
        prefill = self.prefill_tps.next(600 if active else 0.0)
        self.prompt_tokens += prefill * self.cfg.poll_interval
        model_mem = sum(gb for _, gb in self.models)
        return {
            "llm.prefill_tps": prefill,
            "llm.gen_tps": gen if active else 0.0,
            "llm.cached_tokens": self.cached.next(),
            "llm.prompt_tokens": self.prompt_tokens,
            # percent-scale, matching real omlx cache_efficiency (0–100)
            "llm.cache_eff": 35 + 30 * math.sin(self.t / 240 + self.phase),
            "llm.model_mem_gb": model_mem,
            "llm.active_reqs": float(active),
            "llm.waiting_reqs": float(max(0, active - 4)),
        }
