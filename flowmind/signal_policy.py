from __future__ import annotations

from dataclasses import dataclass

from .area_model import AreaModel, Intersection
from .config import ControlConfig
from .traffic_state import LaneState, TrafficState


@dataclass(frozen=True)
class PhaseScore:
    phase_index: int
    score: float
    blocked_signal_indices: tuple[int, ...] = ()


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
    queue_growth_by_lane: dict[str, float] | None = None,
    platoon_arrival_by_incoming_lane: dict[str, float] | None = None,
    downstream_storage_by_outgoing_lane: dict[str, float] | None = None,
    blocked_signals_by_phase: dict[int, tuple[int, ...]] | None = None,
) -> tuple[PhaseScore, ...]:
    area_pressure = area_pressure or {}
    queue_forecast = queue_forecast or {}
    demand_wait_by_lane = demand_wait_by_lane or {}
    downstream_risk_by_outgoing_lane = downstream_risk_by_outgoing_lane or {}
    queue_growth_by_lane = queue_growth_by_lane or {}
    platoon_arrival_by_incoming_lane = (
        platoon_arrival_by_incoming_lane or {}
    )
    downstream_storage_by_outgoing_lane = (
        downstream_storage_by_outgoing_lane or {}
    )
    blocked_signals_by_phase = blocked_signals_by_phase or phase_signal_masks(
        intersection,
        state,
        config,
        priority_link=priority_link,
        preparation_link=preparation_link,
        downstream_risk_by_outgoing_lane=downstream_risk_by_outgoing_lane,
    )
    scores: list[PhaseScore] = []
    for phase_index in intersection.green_phase_indices:
        phase_state = intersection.phases[phase_index]
        blocked_signal_indices = blocked_signals_by_phase.get(phase_index, ())
        blocked_signal_set = set(blocked_signal_indices)
        green_links = tuple(
            (link_index, link)
            for link_index, link in enumerate(intersection.links)
            if link.signal_index < len(phase_state)
            and phase_state[link.signal_index] in "Gg"
            and link.signal_index not in blocked_signal_set
        )
        if not green_links:
            continue
        turns_per_incoming: dict[str, int] = {}
        for _link_index, link in green_links:
            turns_per_incoming[link.incoming_lane] = (
                turns_per_incoming.get(link.incoming_lane, 0) + 1
            )
        score = 0.0
        served_incoming_lanes: set[str] = set()
        demand_movements = 0
        for link_index, link in enumerate(intersection.links):
            if link.signal_index >= len(phase_state):
                continue
            if phase_state[link.signal_index] not in "Gg":
                continue
            if link.signal_index in blocked_signal_set:
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
            if has_demand:
                demand_movements += 1
            turning_ratio = 1.0 / max(
                turns_per_incoming.get(link.incoming_lane, 1),
                1,
            )
            if mode == "local":
                movement_score = (
                    float(incoming.queue)
                    if has_demand
                    else -float(config.empty_approach_penalty)
                )
                movement_score *= turning_ratio
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
                movement_score *= turning_ratio
                horizon = float(config.coordination_horizon_seconds)
                saturation_capacity = (
                    float(config.saturation_flow_vph_per_lane)
                    / 3600.0
                    * horizon
                    * turning_ratio
                )
                predicted_arrivals = platoon_arrival_by_incoming_lane.get(
                    link.incoming_lane,
                    0.0,
                )
                discharge = min(
                    float(incoming.vehicle_count) + predicted_arrivals,
                    saturation_capacity,
                )
                movement_score += (
                    float(incoming.queue)
                    * horizon
                    / 60.0
                    * config.objective_delay_weight
                )
                movement_score += max(
                    queue_growth_by_lane.get(link.incoming_lane, 0.0),
                    0.0,
                ) * config.objective_queue_growth_weight
                movement_score += (
                    float(incoming.queue) * config.objective_stops_weight
                )
                movement_score += (
                    discharge * config.objective_throughput_weight
                )
                movement_score += (
                    predicted_arrivals * config.platoon_arrival_weight
                )
                movement_score -= (
                    graph_spillback_risk * config.objective_spillback_weight
                )
                if downstream_storage_by_outgoing_lane:
                    movement_score += min(
                        downstream_storage_by_outgoing_lane.get(
                            link.outgoing_lane,
                            0.0,
                        ),
                        saturation_capacity,
                    ) * 0.05
            if is_preparation_movement:
                movement_score += float(config.corridor_prepare_bonus)
            if has_demand:
                movement_score += demand_wait_bonus(
                    demand_wait_by_lane.get(link.incoming_lane, 0.0),
                    config,
                )
            score += movement_score
            served_incoming_lanes.add(link.incoming_lane)
        movements = len(served_incoming_lanes)
        if movements:
            score /= movements
        if movements and not demand_movements:
            score -= float(config.empty_phase_penalty)
        if priority_link is not None and 0 <= priority_link < len(phase_state):
            if (
                phase_state[priority_link] in "Gg"
                and priority_link not in blocked_signal_set
            ):
                score += 1_000.0
        scores.append(
            PhaseScore(
                phase_index,
                score,
                blocked_signal_indices,
            )
        )
    return tuple(scores)


def phase_signal_masks(
    intersection: Intersection,
    state: TrafficState,
    config: ControlConfig,
    *,
    priority_link: int | None = None,
    preparation_link: int | None = None,
    downstream_risk_by_outgoing_lane: dict[str, float] | None = None,
) -> dict[int, tuple[int, ...]]:
    """Return green signal indices that must be red-masked per phase.

    A signal index is the smallest independently controllable SUMO movement
    group. If any demanded turn behind that signal has no safe downstream
    storage, the whole signal index is masked while other green groups in the
    same validated phase may continue.
    """

    downstream_risk = downstream_risk_by_outgoing_lane or {}
    result: dict[int, tuple[int, ...]] = {}
    for phase_index in intersection.green_phase_indices:
        phase_state = intersection.phases[phase_index]
        blocked: set[int] = set()
        for link in intersection.links:
            signal_index = link.signal_index
            if signal_index >= len(phase_state):
                continue
            if phase_state[signal_index] not in "Gg":
                continue
            incoming = state.lane(link.incoming_lane)
            outgoing = state.lane(link.outgoing_lane)
            has_demand = lane_has_demand(incoming, config)
            is_priority = priority_link is not None and signal_index == priority_link
            is_preparation = (
                preparation_link is not None and signal_index == preparation_link
            )
            if not (has_demand or is_priority or is_preparation):
                continue
            graph_blocked = (
                downstream_risk.get(link.outgoing_lane, 0.0)
                >= config.spillback_hard_gate_probability
            )
            required_slots = (
                config.priority_min_storage_slots
                if is_priority
                else config.min_downstream_storage_slots
            )
            if graph_blocked or movement_has_blocked_downstream(
                outgoing,
                config,
                required_storage_slots=required_slots,
            ):
                blocked.add(signal_index)
        result[phase_index] = tuple(sorted(blocked))
    return result


def choose_phase(scores: tuple[PhaseScore, ...]) -> PhaseScore | None:
    if not scores:
        return None
    return max(scores, key=lambda item: (item.score, -item.phase_index))
