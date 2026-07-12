from __future__ import annotations

from dataclasses import dataclass

from .area_model import AreaModel, Intersection
from .config import ControlConfig
from .traffic_state import LaneState, TrafficState


@dataclass(frozen=True)
class PhaseScore:
    phase_index: int
    score: float


def movement_pressure(
    incoming_queue: int,
    incoming_vehicle_count: int,
    incoming_occupancy: float,
    outgoing_queue: int,
    outgoing_occupancy: float,
    outgoing_free_slots: float,
    config: ControlConfig,
) -> float:
    """Pressure score for one controlled movement."""

    if not lane_has_demand(
        LaneState(
            incoming_queue,
            incoming_vehicle_count,
            incoming_occupancy,
            0.0,
            0.0,
        ),
        config,
    ):
        return -float(config.empty_approach_penalty)

    pressure = (
        float(incoming_queue)
        + float(incoming_vehicle_count) * 0.25
        + incoming_occupancy * 8.0
        - outgoing_occupancy * config.downstream_weight
        - float(outgoing_queue) * 0.65
        + min(outgoing_free_slots, 20.0) * 0.08
    )
    if outgoing_occupancy >= config.blocked_occupancy:
        pressure -= 30.0
    if (
        incoming_queue >= config.congested_queue_threshold
        or incoming_occupancy >= config.congested_occupancy_threshold
    ):
        pressure += float(config.congested_approach_bonus)
        pressure += max(
            float(incoming_queue - config.congested_queue_threshold),
            0.0,
        ) * 0.5
    return pressure


def movement_has_blocked_downstream(
    outgoing: LaneState,
    config: ControlConfig,
    *,
    required_storage_slots: float | None = None,
) -> bool:
    """Hard gate a movement that cannot fit another vehicle downstream."""

    required_slots = (
        float(config.min_downstream_storage_slots)
        if required_storage_slots is None
        else float(required_storage_slots)
    )
    return (
        outgoing.occupancy >= config.blocked_occupancy
        or outgoing.free_slots < max(required_slots, 0.0)
    )


def lane_has_demand(lane: LaneState, config: ControlConfig) -> bool:
    if not lane.valid:
        return False
    return (
        lane.queue > 0
        or lane.vehicle_count > 0
        or lane.occupancy >= max(config.congested_occupancy_threshold * 0.25, 0.05)
    )


def demand_wait_bonus(wait_seconds: float, config: ControlConfig) -> float:
    if wait_seconds < float(config.demand_timer_seconds):
        return 0.0
    active_wait = wait_seconds - float(config.demand_timer_seconds)
    return min(
        active_wait * float(config.demand_wait_weight),
        float(config.max_demand_wait_bonus),
    )


def effective_min_green(
    config: ControlConfig,
    intersection: Intersection,
    phase_index: int,
) -> float:
    configured_minimum = float(config.min_green)
    if config.use_default_phase_timing:
        default_duration = intersection.default_phase_duration(phase_index)
        if default_duration is not None:
            configured_minimum = min(configured_minimum, default_duration)
    sumo_minimum = intersection.phase_min_duration(phase_index)
    return max(
        1.0,
        configured_minimum,
        sumo_minimum if sumo_minimum is not None else 0.0,
    )


def effective_max_green(
    config: ControlConfig,
    intersection: Intersection,
    phase_index: int,
) -> float:
    if not config.use_default_phase_timing:
        return float(config.max_green)
    default_duration = intersection.default_phase_duration(phase_index)
    if default_duration is None:
        return float(config.max_green)
    return max(
        effective_min_green(config, intersection, phase_index),
        min(float(config.max_green), default_duration + config.default_green_extension),
    )


def area_pressure_by_incoming_lane(
    area: AreaModel,
    state: TrafficState,
    config: ControlConfig,
) -> dict[str, float]:
    """Aggregate pressure from every controlled movement in the area.

    The controller still applies each traffic-light program locally, but these
    values make every intersection aware of pressure observed elsewhere in the
    zone. Lanes that repeatedly feed congested downstream roads get lower
    priority, while lanes that can drain blocked approaches get a stronger
    signal earlier.
    """

    totals: dict[str, float] = {}
    counts: dict[str, int] = {}
    for intersection in area.intersections:
        for link in intersection.links:
            incoming = state.lane(link.incoming_lane)
            outgoing = state.lane(link.outgoing_lane)
            pressure = movement_pressure(
                incoming.queue,
                incoming.vehicle_count,
                incoming.occupancy,
                outgoing.queue,
                outgoing.occupancy,
                outgoing.free_slots,
                config,
            )
            totals[link.incoming_lane] = (
                totals.get(link.incoming_lane, 0.0) + pressure
            )
            counts[link.incoming_lane] = counts.get(link.incoming_lane, 0) + 1
    return {
        lane_id: totals[lane_id] / max(counts[lane_id], 1)
        for lane_id in totals
    }


def score_phases(
    intersection: Intersection,
    state: TrafficState,
    mode: str,
    config: ControlConfig,
    priority_link: int | None = None,
    area_pressure: dict[str, float] | None = None,
    queue_forecast: dict[tuple[int, int], float] | None = None,
    demand_wait_by_lane: dict[str, float] | None = None,
    downstream_risk_by_outgoing_lane: dict[str, float] | None = None,
    preparation_link: int | None = None,
) -> tuple[PhaseScore, ...]:
    area_pressure = area_pressure or {}
    queue_forecast = queue_forecast or {}
    demand_wait_by_lane = demand_wait_by_lane or {}
    downstream_risk_by_outgoing_lane = downstream_risk_by_outgoing_lane or {}
    scores: list[PhaseScore] = []
    for phase_index in intersection.green_phase_indices:
        phase_state = intersection.phases[phase_index]
        score = 0.0
        movements = 0
        demand_movements = 0
        blocked_downstream = False
        for link_index, link in enumerate(intersection.links):
            if link.signal_index >= len(phase_state):
                continue
            if phase_state[link.signal_index] not in "Gg":
                continue
            incoming = state.lane(link.incoming_lane)
            outgoing = state.lane(link.outgoing_lane)
            has_demand = lane_has_demand(incoming, config)
            is_priority_movement = (
                priority_link is not None
                and link.signal_index == priority_link
            )
            is_preparation_movement = (
                preparation_link is not None
                and link.signal_index == preparation_link
            )
            graph_spillback_risk = downstream_risk_by_outgoing_lane.get(
                link.outgoing_lane,
                0.0,
            )
            if (
                has_demand or is_priority_movement or is_preparation_movement
            ) and graph_spillback_risk >= config.spillback_hard_gate_probability:
                blocked_downstream = True
                break
            if is_priority_movement and movement_has_blocked_downstream(
                outgoing,
                config,
                required_storage_slots=config.priority_min_storage_slots,
            ):
                blocked_downstream = True
                break
            if is_preparation_movement and movement_has_blocked_downstream(
                outgoing,
                config,
            ):
                blocked_downstream = True
                break
            if has_demand and movement_has_blocked_downstream(
                outgoing,
                config,
            ):
                blocked_downstream = True
                break
            if has_demand:
                demand_movements += 1
            if mode == "local":
                movement_score = (
                    float(incoming.queue)
                    if has_demand
                    else -float(config.empty_approach_penalty)
                )
            else:
                movement_score = movement_pressure(
                    incoming.queue,
                    incoming.vehicle_count,
                    incoming.occupancy,
                    outgoing.queue,
                    outgoing.occupancy,
                    outgoing.free_slots,
                    config,
                )
                movement_score += (
                    area_pressure.get(link.incoming_lane, 0.0)
                    * config.area_pressure_weight
                )
                movement_score += (
                    queue_forecast.get((phase_index, link_index), 0.0)
                    * config.queue_forecast_weight
                )
                movement_score -= (
                    graph_spillback_risk * config.downstream_graph_weight
                )
            if is_preparation_movement:
                movement_score += float(config.corridor_prepare_bonus)
            if has_demand:
                movement_score += demand_wait_bonus(
                    demand_wait_by_lane.get(link.incoming_lane, 0.0),
                    config,
                )
            score += movement_score
            movements += 1
        if blocked_downstream:
            continue
        if movements:
            score /= movements
        if movements and not demand_movements:
            score -= float(config.empty_phase_penalty)
        if priority_link is not None and 0 <= priority_link < len(phase_state):
            if phase_state[priority_link] in "Gg":
                score += 1_000.0
        scores.append(PhaseScore(phase_index, score))
    return tuple(scores)


def choose_phase(scores: tuple[PhaseScore, ...]) -> PhaseScore | None:
    if not scores:
        return None
    return max(scores, key=lambda item: (item.score, -item.phase_index))
