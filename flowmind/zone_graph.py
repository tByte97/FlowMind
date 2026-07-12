from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .area_model import AreaModel
from .config import ControlConfig
from .traffic_state import TrafficState


@dataclass(frozen=True)
class ZoneIntersection:
    tls_id: str
    name: str
    corridor_ids: tuple[str, ...]


@dataclass(frozen=True)
class Corridor:
    corridor_id: str
    tls_sequence: tuple[str, ...]


@dataclass(frozen=True)
class ZoneDefinition:
    zone_id: str
    name: str
    intersections: tuple[ZoneIntersection, ...]
    corridors: tuple[Corridor, ...]

    @property
    def tls_ids(self) -> tuple[str, ...]:
        return tuple(item.tls_id for item in self.intersections)


@dataclass(frozen=True)
class RoadSegment:
    segment_id: str
    corridor_id: str
    upstream_tls: str
    downstream_tls: str
    edge_ids: tuple[str, ...]
    lane_ids: tuple[str, ...]
    upstream_outgoing_lanes: tuple[str, ...]
    downstream_incoming_lanes: tuple[str, ...]
    length_meters: float
    capacity_slots: float


@dataclass(frozen=True)
class IntersectionStorage:
    tls_id: str
    lane_ids: tuple[str, ...]
    capacity_slots: float


@dataclass(frozen=True)
class AreaGraph:
    zone: ZoneDefinition
    segments: tuple[RoadSegment, ...]
    node_storage: tuple[IntersectionStorage, ...]

    def __post_init__(self) -> None:
        tls_ids = set(self.zone.tls_ids)
        segment_ids: set[str] = set()
        for segment in self.segments:
            if segment.segment_id in segment_ids:
                raise ValueError(f"Duplicate road segment: {segment.segment_id}")
            segment_ids.add(segment.segment_id)
            if segment.upstream_tls not in tls_ids or segment.downstream_tls not in tls_ids:
                raise ValueError(
                    f"Segment {segment.segment_id} references TLS outside the zone"
                )
            if segment.upstream_tls == segment.downstream_tls:
                raise ValueError(f"Segment {segment.segment_id} must connect two TLS")
            if segment.capacity_slots <= 0 or segment.length_meters <= 0:
                raise ValueError(
                    f"Segment {segment.segment_id} must have positive storage"
                )
        storage_ids = [item.tls_id for item in self.node_storage]
        if len(set(storage_ids)) != len(storage_ids):
            raise ValueError("Node storage TLS IDs must be unique")
        if set(storage_ids) != tls_ids:
            raise ValueError("Node storage must cover every TLS in the zone")

    @property
    def monitored_lane_ids(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    lane_id
                    for segment in self.segments
                    for lane_id in segment.lane_ids
                }
                | {
                    lane_id
                    for storage in self.node_storage
                    for lane_id in storage.lane_ids
                }
            )
        )

    def outgoing_segments(self, tls_id: str) -> tuple[RoadSegment, ...]:
        return tuple(
            segment
            for segment in self.segments
            if segment.upstream_tls == tls_id
        )

    def incoming_segments(self, tls_id: str) -> tuple[RoadSegment, ...]:
        return tuple(
            segment
            for segment in self.segments
            if segment.downstream_tls == tls_id
        )


@dataclass(frozen=True)
class SegmentTrafficState:
    segment_id: str
    capacity_slots: float
    occupied_slots: float
    free_slots: float
    queue: int
    occupancy: float
    spillback_probability: float


@dataclass(frozen=True)
class NodeStorageState:
    tls_id: str
    capacity_slots: float
    occupied_slots: float
    free_slots: float
    occupancy: float
    spillback_probability: float


@dataclass(frozen=True)
class AreaDecisionSnapshot:
    simulation_time: float
    traffic: TrafficState
    segment_states: dict[str, SegmentTrafficState]
    node_states: dict[str, NodeStorageState]
    downstream_risk_by_outgoing_lane: dict[str, float]
    area_pressure_by_incoming_lane: dict[str, float]


def load_zone_definition(zone_path: str | Path) -> ZoneDefinition:
    payload = json.loads(Path(zone_path).read_text(encoding="utf-8"))
    raw_intersections = payload.get("intersections")
    raw_corridors = payload.get("corridors")
    if not isinstance(raw_intersections, list) or not raw_intersections:
        raise ValueError("Zone must contain a non-empty intersections list")
    if not isinstance(raw_corridors, dict) or not raw_corridors:
        raise ValueError("Zone must contain a non-empty corridors object")

    intersections: list[ZoneIntersection] = []
    for item in raw_intersections:
        if not isinstance(item, dict) or not item.get("tls_id"):
            raise ValueError("Every zone intersection must contain tls_id")
        corridor_ids = item.get("corridors", ())
        if not isinstance(corridor_ids, list) or not corridor_ids:
            raise ValueError(
                f"Intersection {item['tls_id']} must belong to a corridor"
            )
        intersections.append(
            ZoneIntersection(
                tls_id=str(item["tls_id"]),
                name=str(item.get("name", item["tls_id"])),
                corridor_ids=tuple(str(value) for value in corridor_ids),
            )
        )
    tls_ids = tuple(item.tls_id for item in intersections)
    if len(set(tls_ids)) != len(tls_ids):
        raise ValueError("Zone traffic-light IDs must be unique")

    corridors: list[Corridor] = []
    for corridor_id, sequence_value in raw_corridors.items():
        if not isinstance(sequence_value, list) or len(sequence_value) < 2:
            raise ValueError(f"Corridor {corridor_id} must contain at least two TLS")
        sequence = tuple(str(value) for value in sequence_value)
        if len(set(sequence)) != len(sequence):
            raise ValueError(f"Corridor {corridor_id} contains duplicate TLS IDs")
        missing = sorted(set(sequence) - set(tls_ids))
        if missing:
            raise ValueError(
                f"Corridor {corridor_id} references unknown TLS: {', '.join(missing)}"
            )
        corridors.append(Corridor(str(corridor_id), sequence))

    corridor_ids = {item.corridor_id for item in corridors}
    for intersection in intersections:
        missing = sorted(set(intersection.corridor_ids) - corridor_ids)
        if missing:
            raise ValueError(
                f"Intersection {intersection.tls_id} references unknown corridors: "
                f"{', '.join(missing)}"
            )
        actual = {
            corridor.corridor_id
            for corridor in corridors
            if intersection.tls_id in corridor.tls_sequence
        }
        if actual != set(intersection.corridor_ids):
            raise ValueError(
                f"Intersection {intersection.tls_id} corridor membership is inconsistent"
            )
    return ZoneDefinition(
        zone_id=str(payload.get("id", Path(zone_path).stem)),
        name=str(payload.get("name", payload.get("id", "FlowMind zone"))),
        intersections=tuple(intersections),
        corridors=tuple(corridors),
    )


def build_area_decision_snapshot(
    area: AreaModel,
    graph: AreaGraph,
    traffic: TrafficState,
    simulation_time: float,
    config: ControlConfig,
) -> AreaDecisionSnapshot:
    if not set(graph.zone.tls_ids).issubset(area.tls_ids):
        raise ValueError("AreaGraph TLS IDs must be present in AreaModel")

    from .signal_policy import area_pressure_by_incoming_lane

    segment_states = {
        segment.segment_id: _segment_state(segment, traffic, config)
        for segment in graph.segments
    }
    node_states = {
        storage.tls_id: _node_state(storage, traffic, config)
        for storage in graph.node_storage
    }
    downstream_risk: dict[str, float] = {}
    max_hops = max(int(config.downstream_graph_hops), 1)
    for origin in graph.segments:
        risks = [
            max(
                segment_states[origin.segment_id].spillback_probability,
                node_states[origin.downstream_tls].spillback_probability,
            )
        ]
        frontier = ((origin.downstream_tls, origin.upstream_tls),)
        visited_segments = {origin.segment_id}
        for hop in range(2, max_hops + 1):
            next_frontier: list[tuple[str, str]] = []
            hop_risk = 0.0
            for tls_id, previous_tls in frontier:
                for candidate in graph.outgoing_segments(tls_id):
                    if candidate.segment_id in visited_segments:
                        continue
                    if candidate.downstream_tls == previous_tls:
                        continue
                    visited_segments.add(candidate.segment_id)
                    next_frontier.append((candidate.downstream_tls, tls_id))
                    hop_risk = max(
                        hop_risk,
                        segment_states[candidate.segment_id].spillback_probability,
                        node_states[candidate.downstream_tls].spillback_probability,
                    )
            if not next_frontier:
                break
            risks.append(
                hop_risk * float(config.downstream_graph_decay) ** (hop - 1)
            )
            frontier = tuple(next_frontier)
        combined_risk = 1.0
        for risk in risks:
            combined_risk *= 1.0 - _clamp(risk)
        combined_risk = 1.0 - combined_risk
        for lane_id in origin.upstream_outgoing_lanes:
            downstream_risk[lane_id] = max(
                downstream_risk.get(lane_id, 0.0),
                combined_risk,
            )

    return AreaDecisionSnapshot(
        simulation_time=float(simulation_time),
        traffic=traffic,
        segment_states=segment_states,
        node_states=node_states,
        downstream_risk_by_outgoing_lane=downstream_risk,
        area_pressure_by_incoming_lane=area_pressure_by_incoming_lane(
            area,
            traffic,
            config,
        ),
    )


def _segment_state(
    segment: RoadSegment,
    traffic: TrafficState,
    config: ControlConfig,
) -> SegmentTrafficState:
    lanes = tuple(traffic.lane(lane_id) for lane_id in segment.lane_ids)
    vehicle_count = sum(lane.vehicle_count for lane in lanes)
    queue = sum(lane.queue for lane in lanes)
    observed_occupancy = max((lane.occupancy for lane in lanes), default=0.0)
    capacity = max(float(segment.capacity_slots), 1.0)
    fill_ratio = max(observed_occupancy, min(vehicle_count / capacity, 1.0))
    spillback = _spillback_probability(fill_ratio, queue / capacity, config)
    return SegmentTrafficState(
        segment_id=segment.segment_id,
        capacity_slots=capacity,
        occupied_slots=min(float(vehicle_count), capacity),
        free_slots=max(capacity - vehicle_count, 0.0),
        queue=queue,
        occupancy=fill_ratio,
        spillback_probability=spillback,
    )


def _node_state(
    storage: IntersectionStorage,
    traffic: TrafficState,
    config: ControlConfig,
) -> NodeStorageState:
    lanes = tuple(traffic.lane(lane_id) for lane_id in storage.lane_ids)
    vehicle_count = sum(lane.vehicle_count for lane in lanes)
    queue = sum(lane.queue for lane in lanes)
    capacity = max(float(storage.capacity_slots), 1.0)
    fill_ratio = max(
        max((lane.occupancy for lane in lanes), default=0.0),
        min(vehicle_count / capacity, 1.0),
    )
    return NodeStorageState(
        tls_id=storage.tls_id,
        capacity_slots=capacity,
        occupied_slots=min(float(vehicle_count), capacity),
        free_slots=max(capacity - vehicle_count, 0.0),
        occupancy=fill_ratio,
        spillback_probability=_spillback_probability(
            fill_ratio,
            queue / capacity,
            config,
        ),
    )


def _spillback_probability(
    occupancy: float,
    queue_ratio: float,
    config: ControlConfig,
) -> float:
    start = min(float(config.spillback_start_occupancy), 0.99)
    occupancy_risk = _clamp((occupancy - start) / max(1.0 - start, 0.01))
    queue_risk = _clamp(queue_ratio)
    probability = occupancy_risk * 0.7 + queue_risk * 0.3
    if occupancy >= config.blocked_occupancy:
        probability = max(probability, 0.9)
    return _clamp(probability)


def _clamp(value: float) -> float:
    return min(max(float(value), 0.0), 1.0)
