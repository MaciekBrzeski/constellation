"""Wire-level message helpers for the control plane.

Messages are JSON dicts with a ``type`` field. Two types currently:

* ``heartbeat`` — worker → master, sent ~1 Hz.
* ``cmd`` — master → worker, sent on user action from the web UI.

Keep this module dependency-free (stdlib only) so workers can import it
without pulling in FastAPI or the rest of the GUI stack.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

DEFAULT_HEARTBEAT_PORT = 49501  # worker → master
DEFAULT_COMMAND_PORT = 49502    # master → worker
HEARTBEAT_INTERVAL_S = 1.0
WORKER_OFFLINE_AFTER_S = 5.0


@dataclass
class HeartbeatMessage:
    worker_id: str
    animal: str
    action: str
    available_animals: list[str]
    available_actions_by_animal: dict[str, list[str]] = field(default_factory=dict)
    tier: str = "low"
    bpm: float = 0.0
    aubio_active: bool = False

    def encode(self) -> bytes:
        payload = {"type": "heartbeat", **asdict(self)}
        return json.dumps(payload).encode("utf-8")


@dataclass
class CommandMessage:
    name: str          # "set_animal" | "set_action" | future
    value: Any         # e.g. animal name or action string

    def encode(self) -> bytes:
        return json.dumps({"type": "cmd", "name": self.name, "value": self.value}).encode(
            "utf-8"
        )


def decode(data: bytes) -> dict[str, Any] | None:
    """Parse a JSON message; return None on any malformed input."""
    try:
        msg = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(msg, dict):
        return None
    return msg
