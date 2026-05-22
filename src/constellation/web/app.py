"""FastAPI control dashboard.

Single-page browser UI served from the master Pi. Live state pushed via
WebSocket; commands sent via HTTP POST. The app is built by
``create_app(detector, control_channel)`` so it doesn't import heavy
audio dependencies at module load.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from constellation import netmode
from constellation.control.master import ControlChannel


class FloatValue(BaseModel):
    value: float


class StringValue(BaseModel):
    value: str


def _state_snapshot(detector: Any, channel: ControlChannel) -> dict[str, Any]:
    now = time.monotonic()
    workers = [
        {
            "worker_id": w.worker_id,
            "ip": w.ip,
            "online": w.is_online(now),
            "animal": w.animal,
            "action": w.action,
            "available_animals": w.available_animals,
            "available_actions_by_animal": w.available_actions_by_animal,
            "tier": w.tier,
            "bpm": w.bpm,
            "aubio_active": w.aubio_active,
            "last_seen_ago_s": now - w.last_seen,
        }
        for w in channel.workers()
    ]
    return {
        "master": {
            "tier": getattr(detector, "energy_tier", "low"),
            "bpm": float(getattr(detector, "bpm", 0.0)),
            "last_beat_strength": float(getattr(detector, "last_beat_strength", 0.0)),
            "last_beat_audio_ms": float(getattr(detector, "last_beat_audio_ms", 0.0)),
            "sensitivity": float(getattr(detector, "sensitivity", 0.0)),
            "noise_floor": float(getattr(detector, "noise_floor", 0.0)),
            "tier_intensity": float(getattr(detector, "tier_intensity", 0.0)),
            "backend": str(getattr(detector, "backend_name", "rms")),
            "band_flux": {
                b: float(v)
                for b, v in (getattr(detector, "band_flux", {}) or {}).items()
            },
        },
        "workers": workers,
        "ts": now,
    }


def create_app(detector: Any, channel: ControlChannel) -> FastAPI:
    app = FastAPI(title="constellation control")
    static_dir = Path(__file__).parent / "static"

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse((static_dir / "index.html").read_text(encoding="utf-8"))

    @app.get("/api/state")
    def api_state() -> dict[str, Any]:
        return _state_snapshot(detector, channel)

    @app.post("/api/master/sensitivity")
    def set_sensitivity(payload: FloatValue) -> dict[str, Any]:
        if not (0.1 <= payload.value <= 10.0):
            raise HTTPException(400, "sensitivity out of range [0.1, 10.0]")
        # Single attribute write — safe across threads (Python GIL + 1 word).
        setattr(detector, "sensitivity", float(payload.value))
        return {"ok": True, "sensitivity": float(payload.value)}

    @app.post("/api/worker/{worker_id}/animal")
    def set_animal(worker_id: str, payload: StringValue) -> dict[str, Any]:
        ok = channel.send_command(worker_id, "set_animal", payload.value)
        if not ok:
            raise HTTPException(404, f"worker {worker_id} unknown")
        return {"ok": True}

    @app.post("/api/worker/{worker_id}/action")
    def set_action(worker_id: str, payload: StringValue) -> dict[str, Any]:
        ok = channel.send_command(worker_id, "set_action", payload.value)
        if not ok:
            raise HTTPException(404, f"worker {worker_id} unknown")
        return {"ok": True}

    @app.get("/api/network/mode")
    def network_mode() -> dict[str, Any]:
        return {
            "available": netmode.scripts_available(),
            "mode": netmode.current_mode(),
        }

    @app.post("/api/network/ap")
    def network_ap() -> dict[str, Any]:
        # Blocking subprocess — FastAPI runs sync routes in a threadpool, so
        # the WebSocket loop keeps streaming while this runs.
        ok, output = netmode.switch_to_ap()
        return {"ok": ok, "output": output, "mode": netmode.current_mode()}

    @app.post("/api/network/wifi")
    def network_wifi() -> dict[str, Any]:
        ok, output = netmode.switch_to_wifi()
        return {"ok": ok, "output": output, "mode": netmode.current_mode()}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            while True:
                await websocket.send_json(_state_snapshot(detector, channel))
                await asyncio.sleep(0.1)  # 10 Hz
        except WebSocketDisconnect:
            return

    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    return app
