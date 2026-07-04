from __future__ import annotations

from dataclasses import dataclass

from .area_model import AreaModel, Intersection
from .config import ControlConfig
from .traffic_state import TrafficState


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
    return pressure


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
) -> tuple[PhaseScore, ...]:
    area_pressure = area_pressure or {}
    scores: list[PhaseScore] = []
    for phase_index in intersection.green_phase_indices:
        phase_state = intersection.phases[phase_index]
        score = 0.0
        movements = 0
        for link in intersection.links:
            if link.signal_index >= len(phase_state):
                continue
            if phase_state[link.signal_index] not in "Gg":
                continue
            incoming = state.lane(link.incoming_lane)
            outgoing = state.lane(link.outgoing_lane)
            if mode == "local":
                movement_score = float(incoming.queue)
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
            score += movement_score
            movements += 1
        if movements:
            score /= movements
        if priority_link is not None and priority_link < len(phase_state):
            if phase_state[priority_link] in "Gg":
                score += 1_000.0
        scores.append(PhaseScore(phase_index, score))
    return tuple(scores)


def choose_phase(scores: tuple[PhaseScore, ...]) -> PhaseScore | None:
    if not scores:
        return None
    return max(scores, key=lambda item: (item.score, -item.phase_index))
