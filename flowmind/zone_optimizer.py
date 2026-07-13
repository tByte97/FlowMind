from __future__ import annotations

from dataclasses import dataclass

from .area_model import AreaModel, Intersection
from .config import ControlConfig
from .signal_policy import PhaseScore
from .zone_graph import AreaDecisionSnapshot, AreaGraph, RoadSegment


@dataclass(frozen=True)
class ZonePhaseChoice:
    tls_id: str
    phase_index: int
    joint_objective: float


def optimize_zone_phases(
    area: AreaModel,
    graph: AreaGraph,
    snapshot: AreaDecisionSnapshot,
    scores_by_tls: dict[str, tuple[PhaseScore, ...]],
    config: ControlConfig,
) -> dict[str, ZonePhaseChoice]:
    """Coordinate phase targets over the whole zone.

    This layer depends only on FlowMind domain objects. SUMO remains an input
    and actuation adapter, so a production sensor/backend can provide the same
    area snapshot without changing the optimizer.
    """

    candidates = {
        tls_id: tuple(item.phase_index for item in scores)
        for tls_id, scores in scores_by_tls.items()
        if scores
    }
    score_lookup = {
        (tls_id, item.phase_index): float(item.score)
        for tls_id, scores in scores_by_tls.items()
        for item in scores
    }
    assignment = {
        tls_id: max(
            phases,
            key=lambda phase: (score_lookup[(tls_id, phase)], -phase),
        )
        for tls_id, phases in candidates.items()
    }
    if not assignment:
        return {}

    intersections = {
        intersection.tls_id: intersection
        for intersection in area.intersections
    }
    # Deterministic coordinate descent captures pairwise corridor coupling
    # without an exponential product of phase combinations for 20+ TLS.
    for _iteration in range(4):
        changed = False
        for tls_id in sorted(assignment):
            best_phase = assignment[tls_id]
            best_value = float("-inf")
            for phase_index in candidates[tls_id]:
                trial = dict(assignment)
                trial[tls_id] = phase_index
                value = _joint_objective(
                    trial,
                    intersections,
                    graph,
                    snapshot,
                    score_lookup,
                    config,
                )
                if value > best_value + 1e-9 or (
                    abs(value - best_value) <= 1e-9
                    and phase_index < best_phase
                ):
                    best_phase = phase_index
                    best_value = value
            if best_phase != assignment[tls_id]:
                assignment[tls_id] = best_phase
                changed = True
        if not changed:
            break

    objective = _joint_objective(
        assignment,
        intersections,
        graph,
        snapshot,
        score_lookup,
        config,
    )
    return {
        tls_id: ZonePhaseChoice(tls_id, phase_index, objective)
        for tls_id, phase_index in assignment.items()
    }


def _joint_objective(
    assignment: dict[str, int],
    intersections: dict[str, Intersection],
    graph: AreaGraph,
    snapshot: AreaDecisionSnapshot,
    score_lookup: dict[tuple[str, int], float],
    config: ControlConfig,
) -> float:
    value = sum(
        score_lookup[(tls_id, phase_index)]
        for tls_id, phase_index in assignment.items()
    )
    for segment in graph.segments:
        upstream_phase = assignment.get(segment.upstream_tls)
        downstream_phase = assignment.get(segment.downstream_tls)
        upstream = intersections.get(segment.upstream_tls)
        downstream = intersections.get(segment.downstream_tls)
        if (
            upstream_phase is None
            or downstream_phase is None
            or upstream is None
            or downstream is None
        ):
            continue
        value += _segment_coordination_value(
            segment,
            upstream,
            upstream_phase,
            downstream,
            downstream_phase,
            snapshot,
            config,
        )
    return value


def _segment_coordination_value(
    segment: RoadSegment,
    upstream: Intersection,
    upstream_phase: int,
    downstream: Intersection,
    downstream_phase: int,
    snapshot: AreaDecisionSnapshot,
    config: ControlConfig,
) -> float:
    releases = _phase_serves_outgoing(
        upstream,
        upstream_phase,
        set(segment.upstream_outgoing_lanes),
    )
    receives = _phase_serves_incoming(
        downstream,
        downstream_phase,
        set(segment.downstream_incoming_lanes),
    )
    state = snapshot.segment_states.get(segment.segment_id)
    if state is None:
        return 0.0
    incoming_platoon = sum(
        snapshot.platoon_arrival_by_incoming_lane.get(lane_id, 0.0)
        for lane_id in segment.downstream_incoming_lanes
    )
    horizon = float(config.coordination_horizon_seconds)
    travel_seconds = float(segment.length_meters) / 13.9
    horizon_factor = max(0.0, 1.0 - travel_seconds / horizon)
    storage_ratio = state.free_slots / max(state.capacity_slots, 1.0)
    risk = max(min(state.spillback_probability, 1.0), 0.0)
    weight = float(config.zone_coordination_weight)

    if releases and receives:
        return weight * horizon_factor * (
            min(incoming_platoon + state.occupied_slots, state.capacity_slots)
            * max(storage_ratio, 0.05)
            - risk * config.objective_spillback_weight
        )
    if releases:
        return -weight * (
            risk * config.objective_spillback_weight
            + incoming_platoon * max(horizon_factor, 0.25)
        )
    if receives:
        return weight * incoming_platoon * horizon_factor * 0.5
    return 0.0


def _phase_serves_outgoing(
    intersection: Intersection,
    phase_index: int,
    lane_ids: set[str],
) -> bool:
    return any(
        link.outgoing_lane in lane_ids
        for link in _green_links(intersection, phase_index)
    )


def _phase_serves_incoming(
    intersection: Intersection,
    phase_index: int,
    lane_ids: set[str],
) -> bool:
    return any(
        link.incoming_lane in lane_ids
        for link in _green_links(intersection, phase_index)
    )


def _green_links(
    intersection: Intersection,
    phase_index: int,
) -> tuple[object, ...]:
    if not 0 <= phase_index < len(intersection.phases):
        return ()
    state = intersection.phases[phase_index]
    return tuple(
        link
        for link in intersection.links
        if link.signal_index < len(state) and state[link.signal_index] in "Gg"
    )


__all__ = ["ZonePhaseChoice", "optimize_zone_phases"]
