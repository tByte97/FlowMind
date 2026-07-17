from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import ControlConfig
from .provenance import canonical_sha256
from .provenance import file_sha256


ML_APPROVAL_SCHEMA_VERSION = 1


def validate_queue_control_approval(
    approval_path: Path,
    model_paths: tuple[Path, ...],
    network_path: Path,
    zone_path: Path,
    control: ControlConfig,
) -> dict[str, Any]:
    try:
        payload = json.loads(approval_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Cannot read queue-control approval: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError("Queue-control approval must be a JSON object")
    expected = {
        "schema_version": ML_APPROVAL_SCHEMA_VERSION,
        "status": "approved",
        "network_sha256": file_sha256(network_path),
        "zone_sha256": file_sha256(zone_path),
        "model_artifact_sha256": [file_sha256(path) for path in model_paths],
        "control_config_sha256": canonical_sha256(control),
    }
    mismatched = [
        key for key, value in expected.items() if payload.get(key) != value
    ]
    if mismatched:
        raise ValueError(
            "Queue-control approval does not match runtime artifacts: "
            + ", ".join(mismatched)
        )
    return payload


__all__ = [
    "ML_APPROVAL_SCHEMA_VERSION",
    "validate_queue_control_approval",
]
