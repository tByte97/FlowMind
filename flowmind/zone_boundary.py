from __future__ import annotations

from dataclasses import dataclass

from .area_model import AreaModel
from .zone_graph import AreaGraph


@dataclass(frozen=True)
class ZoneBoundary:
    incoming_lane_ids: tuple[str, ...]
    outgoing_lane_ids: tuple[str, ...]
    incoming_edge_ids: tuple[str, ...]
    outgoing_edge_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        overlap = set(self.incoming_edge_ids) & set(self.outgoing_edge_ids)
        if overlap:
            raise ValueError(
                "Zone boundary edges must have one direction: "
                + ", ".join(sorted(overlap))
            )

    @property
    def valid(self) -> bool:
        return bool(self.incoming_edge_ids and self.outgoing_edge_ids)

    def as_summary(self) -> dict[str, object]:
        return {
            "zone_boundary_incoming_edges": self.incoming_edge_ids,
            "zone_boundary_outgoing_edges": self.outgoing_edge_ids,
            "zone_boundary_incoming_edge_count": len(self.incoming_edge_ids),
            "zone_boundary_outgoing_edge_count": len(self.outgoing_edge_ids),
        }


def build_zone_boundary(area: AreaModel, graph: AreaGraph) -> ZoneBoundary:
    """Derive directed entry/exit edges around the fixed evaluation zone."""

    # Every lane traversed by a graph segment is internal to the controlled
    # area.  Looking only at the selected endpoint lanes is insufficient for
    # larger zones: a shortest path can cross another controlled junction and
    # make the same directed edge appear as both an entry and an exit.
    internal_lanes = {
        lane_id
        for segment in graph.segments
        for lane_id in segment.lane_ids
    }
    area_incoming_edges = {_edge_id(lane_id) for lane_id in area.incoming_lanes}
    area_outgoing_edges = {_edge_id(lane_id) for lane_id in area.outgoing_lanes}
    internal_edges = {_edge_id(lane_id) for lane_id in internal_lanes}
    # A directed edge seen as outgoing at one controlled TLS and incoming at
    # another is an intra-zone connector even when it is not part of a named
    # corridor.  It must not be counted on either side of the boundary.
    internal_edges.update(area_incoming_edges & area_outgoing_edges)
    incoming_lanes = tuple(
        sorted(
            lane_id
            for lane_id in area.incoming_lanes
            if _edge_id(lane_id) not in internal_edges
        )
    )
    outgoing_lanes = tuple(
        sorted(
            lane_id
            for lane_id in area.outgoing_lanes
            if _edge_id(lane_id) not in internal_edges
        )
    )
    incoming_edges = tuple(sorted({_edge_id(lane_id) for lane_id in incoming_lanes}))
    outgoing_edges = tuple(sorted({_edge_id(lane_id) for lane_id in outgoing_lanes}))
    return ZoneBoundary(
        incoming_lane_ids=incoming_lanes,
        outgoing_lane_ids=outgoing_lanes,
        incoming_edge_ids=incoming_edges,
        outgoing_edge_ids=outgoing_edges,
    )


def _edge_id(lane_id: str) -> str:
    edge_id, separator, lane_index = str(lane_id).rpartition("_")
    if separator and lane_index.isdigit():
        return edge_id
    return str(lane_id)


__all__ = ["ZoneBoundary", "build_zone_boundary"]
