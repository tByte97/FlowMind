from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path
from typing import Any

from .config import ControlConfig


def load_control_config(path: Path) -> ControlConfig:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Cannot load ControlConfig from {path}: {error}") from error
    if not isinstance(raw, dict):
        raise ValueError("ControlConfig artifact must be a JSON object")
    payload: Any = raw.get("control_config", raw)
    if not isinstance(payload, dict):
        raise ValueError("control_config must be a JSON object")
    allowed = {item.name for item in fields(ControlConfig)}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ValueError("Unknown ControlConfig fields: " + ", ".join(unknown))
    normalized = dict(payload)
    if "queue_forecast_horizon_weights" in normalized:
        normalized["queue_forecast_horizon_weights"] = tuple(
            (int(horizon), float(weight))
            for horizon, weight in normalized["queue_forecast_horizon_weights"]
        )
    return ControlConfig(**normalized)


__all__ = ["load_control_config"]
