from __future__ import annotations

import json
from dataclasses import dataclass
from math import hypot
from pathlib import Path
from statistics import median

import sumolib


@dataclass(frozen=True)
class ControlledLink:
    incoming_lane: str
    outgoing_lane: str
    signal_index: int


@dataclass(frozen=True)
class Intersection:
    tls_id: str
    position: tuple[float, float]
    phases: tuple[str, ...]
    links: tuple[ControlledLink, ...]

    @property
    def green_phase_indices(self) -> tuple[int, ...]:
        return tuple(
            index
            for index, state in enumerate(self.phases)
            if "y" not in state.lower() and any(signal in "Gg" for signal in state)
        )


@dataclass(frozen=True)
class AreaModel:
    intersections: tuple[Intersection, ...]

    @property
    def tls_ids(self) -> tuple[str, ...]:
        return tuple(item.tls_id for item in self.intersections)

    @property
    def incoming_lanes(self) -> tuple[str, ...]:
        return tuple(
            sorted({link.incoming_lane for item in self.intersections for link in item.links})
        )

    @property
    def outgoing_lanes(self) -> tuple[str, ...]:
        return tuple(
            sorted({link.outgoing_lane for item in self.intersections for link in item.links})
        )

    def intersection(self, tls_id: str) -> Intersection:
        return next(item for item in self.intersections if item.tls_id == tls_id)


def load_zone_tls_ids(zone_path: str | Path) -> tuple[str, ...]:
    payload = json.loads(Path(zone_path).read_text(encoding="utf-8"))
    intersections = payload.get("intersections")
    if not isinstance(intersections, list) or not intersections:
        raise ValueError("Zone definition must contain a non-empty intersections list")
    tls_ids = tuple(
        str(item["tls_id"])
        for item in intersections
        if isinstance(item, dict) and item.get("tls_id")
    )
    if len(tls_ids) != len(intersections):
        raise ValueError("Every zone intersection must contain tls_id")
    if len(set(tls_ids)) != len(tls_ids):
        raise ValueError("Zone traffic-light IDs must be unique")
    if not 4 <= len(tls_ids) <= 6:
        raise ValueError("A FlowMind zone must contain 4 to 6 traffic lights")
    return tls_ids


def _intersection_from_tls(tls: object) -> Intersection | None:
    programs = tls.getPrograms()
    if not programs:
        return None

    program = next(iter(programs.values()))
    phases = tuple(phase.state for phase in program.getPhases())
    links = tuple(
        ControlledLink(
            incoming_lane=connection[0].getID(),
            outgoing_lane=connection[1].getID(),
            signal_index=int(connection[2]),
        )
        for connection in tls.getConnections()
    )
    if not links or len([state for state in phases if "G" in state or "g" in state]) < 2:
        return None

    edge_centres = []
    for edge in tls.getEdges():
        coordinates = edge.getToNode().getCoord()
        edge_centres.append((float(coordinates[0]), float(coordinates[1])))
    if not edge_centres:
        return None

    position = (
        sum(point[0] for point in edge_centres) / len(edge_centres),
        sum(point[1] for point in edge_centres) / len(edge_centres),
    )
    return Intersection(str(tls.getID()), position, phases, links)


def discover_area(
    net_path: str | Path,
    zone_size: int = 6,
    requested_tls: tuple[str, ...] = (),
) -> AreaModel:
    """Load a reproducible, compact traffic-light area from a SUMO network.

    When IDs are not supplied, the function selects substantial intersections
    nearest to the median traffic-light position. This favours a dense central
    zone and avoids tiny pedestrian-only programs.
    """

    network = sumolib.net.readNet(
        str(net_path), withPrograms=True, withConnections=True
    )
    candidates = [
        item
        for tls in network.getTrafficLights()
        if (item := _intersection_from_tls(tls)) is not None
        and len(item.links) >= 4
    ]
    if not candidates:
        raise RuntimeError("No controllable traffic lights found in the SUMO network")

    by_id = {item.tls_id: item for item in candidates}
    if requested_tls:
        missing = sorted(set(requested_tls) - set(by_id))
        if missing:
            raise ValueError(f"Unknown or unsupported traffic lights: {', '.join(missing)}")
        selected = [by_id[tls_id] for tls_id in requested_tls]
    else:
        centre = (
            median(item.position[0] for item in candidates),
            median(item.position[1] for item in candidates),
        )
        seed = min(
            candidates,
            key=lambda item: hypot(
                item.position[0] - centre[0], item.position[1] - centre[1]
            ),
        )
        selected = sorted(
            candidates,
            key=lambda item: (
                hypot(
                    item.position[0] - seed.position[0],
                    item.position[1] - seed.position[1],
                ),
                item.tls_id,
            ),
        )[:zone_size]

    return AreaModel(tuple(selected))
