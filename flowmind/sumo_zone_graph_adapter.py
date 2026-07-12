from __future__ import annotations

from pathlib import Path

import sumolib

from .area_model import AreaModel, Intersection
from .zone_graph import (
    AreaGraph,
    IntersectionStorage,
    RoadSegment,
    ZoneDefinition,
)


class SumoZoneGraphAdapter:
    """Build the pre-MVP road graph from SUMO without leaking SUMO types."""

    def __init__(self, net_path: str | Path) -> None:
        self._net_path = Path(net_path)

    def build_graph(
        self,
        zone: ZoneDefinition,
        area: AreaModel,
    ) -> AreaGraph:
        if not set(zone.tls_ids).issubset(area.tls_ids):
            raise ValueError("ZoneDefinition TLS IDs must be present in AreaModel")
        network = sumolib.net.readNet(
            str(self._net_path),
            withConnections=True,
        )
        zone_tls_ids = set(zone.tls_ids)
        by_tls = {item.tls_id: item for item in area.intersections}
        segments: list[RoadSegment] = []
        for corridor in zone.corridors:
            for left, right in zip(
                corridor.tls_sequence,
                corridor.tls_sequence[1:],
                strict=False,
            ):
                segments.append(
                    self._build_segment(
                        network,
                        corridor.corridor_id,
                        by_tls[left],
                        by_tls[right],
                    )
                )
                segments.append(
                    self._build_segment(
                        network,
                        corridor.corridor_id,
                        by_tls[right],
                        by_tls[left],
                    )
                )
        storage = tuple(
            self._build_storage(network, intersection)
            for intersection in area.intersections
            if intersection.tls_id in zone_tls_ids
        )
        return AreaGraph(zone, tuple(segments), storage)

    @staticmethod
    def _build_segment(
        network: object,
        corridor_id: str,
        upstream: Intersection,
        downstream: Intersection,
    ) -> RoadSegment:
        outgoing_lanes = tuple(
            sorted({link.outgoing_lane for link in upstream.links})
        )
        incoming_lanes = tuple(
            sorted({link.incoming_lane for link in downstream.links})
        )
        candidates: list[tuple[float, tuple[object, ...], str, str]] = []
        edge_path_cache: dict[tuple[str, str], tuple[tuple[object, ...] | None, float]] = {}
        for outgoing_lane_id in outgoing_lanes:
            outgoing_lane = network.getLane(outgoing_lane_id)
            if not outgoing_lane.allows("passenger"):
                continue
            from_edge = outgoing_lane.getEdge()
            for incoming_lane_id in incoming_lanes:
                incoming_lane = network.getLane(incoming_lane_id)
                if not incoming_lane.allows("passenger"):
                    continue
                to_edge = incoming_lane.getEdge()
                key = (str(from_edge.getID()), str(to_edge.getID()))
                if key not in edge_path_cache:
                    path, cost = network.getShortestPath(
                        from_edge,
                        to_edge,
                        vClass="passenger",
                    )
                    edge_path_cache[key] = (
                        tuple(path) if path is not None else None,
                        float(cost),
                    )
                path, cost = edge_path_cache[key]
                if path:
                    candidates.append(
                        (cost, path, outgoing_lane_id, incoming_lane_id)
                    )
        if not candidates:
            raise RuntimeError(
                f"No passenger road path from TLS {upstream.tls_id} "
                f"to {downstream.tls_id} in corridor {corridor_id}"
            )
        cost, edge_path, _outgoing_lane, _incoming_lane = min(
            candidates,
            key=lambda item: (
                item[0],
                tuple(str(edge.getID()) for edge in item[1]),
                item[2],
                item[3],
            ),
        )
        first_edge_id = str(edge_path[0].getID())
        last_edge_id = str(edge_path[-1].getID())
        selected_outgoing = tuple(
            lane_id
            for lane_id in outgoing_lanes
            if str(network.getLane(lane_id).getEdge().getID()) == first_edge_id
            and network.getLane(lane_id).allows("passenger")
        )
        selected_incoming = tuple(
            lane_id
            for lane_id in incoming_lanes
            if str(network.getLane(lane_id).getEdge().getID()) == last_edge_id
            and network.getLane(lane_id).allows("passenger")
        )
        segment_lanes = tuple(
            sorted(
                {
                    str(lane.getID())
                    for edge in edge_path
                    for lane in edge.getLanes()
                    if lane.allows("passenger")
                }
            )
        )
        capacity_slots = sum(
            max(float(network.getLane(lane_id).getLength()) / 7.5, 1.0)
            for lane_id in segment_lanes
        )
        return RoadSegment(
            segment_id=f"{corridor_id}:{upstream.tls_id}>{downstream.tls_id}",
            corridor_id=corridor_id,
            upstream_tls=upstream.tls_id,
            downstream_tls=downstream.tls_id,
            edge_ids=tuple(str(edge.getID()) for edge in edge_path),
            lane_ids=segment_lanes,
            upstream_outgoing_lanes=selected_outgoing,
            downstream_incoming_lanes=selected_incoming,
            length_meters=max(float(cost), 1.0),
            capacity_slots=max(capacity_slots, 1.0),
        )

    @staticmethod
    def _build_storage(
        network: object,
        intersection: Intersection,
    ) -> IntersectionStorage:
        lane_ids = tuple(
            sorted(
                {
                    link.incoming_lane
                    for link in intersection.links
                    if network.getLane(link.incoming_lane).allows("passenger")
                }
            )
        )
        if not lane_ids:
            raise RuntimeError(
                f"TLS {intersection.tls_id} has no passenger storage lanes"
            )
        capacity = sum(
            max(float(network.getLane(lane_id).getLength()) / 7.5, 1.0)
            for lane_id in lane_ids
        )
        return IntersectionStorage(
            tls_id=intersection.tls_id,
            lane_ids=lane_ids,
            capacity_slots=max(capacity, 1.0),
        )
