from __future__ import annotations

from .config import ControlConfig


def priority_links(
    traci_connection: object,
    vehicle_id: str | None,
    config: ControlConfig,
) -> dict[str, int]:
    """Return upcoming signal-link indexes for an optional priority vehicle."""

    if not vehicle_id or vehicle_id not in traci_connection.vehicle.getIDList():
        return {}
    result: dict[str, int] = {}
    for tls_id, link_index, distance, _state in traci_connection.vehicle.getNextTLS(
        vehicle_id
    ):
        if distance > config.priority_distance:
            continue
        result.setdefault(str(tls_id), int(link_index))
    return result
