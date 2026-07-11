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
    incoming_shape: tuple[tuple[float, float], ...] = ()
    outgoing_shape: tuple[tuple[float, float], ...] = ()


@dataclass(frozen=True)
class Intersection:
    tls_id: str
    position: tuple[float, float]
    phases: tuple[str, ...]
    links: tuple[ControlledLink, ...]
    phase_durations: tuple[float, ...] = ()

    @property
    def green_phase_indices(self) -> tuple[int, ...]:
        return tuple(
            index
            for index, state in enumerate(self.phases)
            if "y" not in state.lower() and any(signal in "Gg" for signal in state)
        )

    def default_phase_duration(self, phase_index: int) -> float | None:
        if 0 <= phase_index < len(self.phase_durations):
            duration = float(self.phase_durations[phase_index])
            if duration > 0:
                return duration
        return None


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

    @property
    def visual_lanes(self) -> tuple[dict[str, object], ...]:
        """Static SUMO lane geometry for a map view of this controlled area."""

        lanes: dict[str, dict[str, object]] = {}

        def add_lane(
            lane_id: str,
            shape: tuple[tuple[float, float], ...],
            direction: str,
            tls_id: str,
        ) -> None:
            if len(shape) < 2:
                return
            entry = lanes.setdefault(
                lane_id,
                {
                    "lane_id": lane_id,
                    "shape": [
                        [round(float(x), 3), round(float(y), 3)]
                        for x, y in shape
                    ],
                    "directions": set(),
                    "tls_ids": set(),
                },
            )
            directions = entry["directions"]
            tls_ids = entry["tls_ids"]
            if isinstance(directions, set):
                directions.add(direction)
            if isinstance(tls_ids, set):
                tls_ids.add(tls_id)

        for intersection in self.intersections:
            for link in intersection.links:
                add_lane(
                    link.incoming_lane,
                    link.incoming_shape,
                    "incoming",
                    intersection.tls_id,
                )
                add_lane(
                    link.outgoing_lane,
                    link.outgoing_shape,
                    "outgoing",
                    intersection.tls_id,
                )

        return tuple(
            {
                "lane_id": lane_id,
                "shape": entry["shape"],
                "directions": sorted(entry["directions"]),
                "tls_ids": sorted(entry["tls_ids"]),
            }
            for lane_id, entry in sorted(lanes.items())
        )

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
    phase_durations = tuple(float(phase.duration) for phase in program.getPhases())
    links = tuple(
        ControlledLink(
            incoming_lane=connection[0].getID(),
            outgoing_lane=connection[1].getID(),
            signal_index=int(connection[2]),
            incoming_shape=tuple(
                (float(point[0]), float(point[1]))
                for point in connection[0].getShape(includeJunctions=True)
            ),
            outgoing_shape=tuple(
                (float(point[0]), float(point[1]))
                for point in connection[1].getShape(includeJunctions=True)
            ),
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
    return Intersection(str(tls.getID()), position, phases, links, phase_durations)


def discover_area(
    net_path: str | Path,
    zone_size: int = 6,
    requested_tls: tuple[str, ...] = (),
    strict_requested: bool = True,
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
        if missing and strict_requested:
            raise ValueError(f"Unknown or unsupported traffic lights: {', '.join(missing)}")
        selected = [by_id[tls_id] for tls_id in requested_tls if tls_id in by_id]
        if not selected:
            raise RuntimeError("No requested controllable traffic lights found")
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
