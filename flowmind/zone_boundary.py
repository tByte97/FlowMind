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

    internal_incoming = {
        lane_id
        for segment in graph.segments
        for lane_id in segment.downstream_incoming_lanes
    }
    internal_outgoing = {
        lane_id
        for segment in graph.segments
        for lane_id in segment.upstream_outgoing_lanes
    }
    incoming_lanes = tuple(
        sorted(set(area.incoming_lanes) - internal_incoming)
    )
    outgoing_lanes = tuple(
        sorted(set(area.outgoing_lanes) - internal_outgoing)
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
