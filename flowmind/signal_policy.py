from __future__ import annotations

from dataclasses import dataclass

from .area_model import Intersection
from .config import ControlConfig
from .traffic_state import TrafficState


@dataclass(frozen=True)
class PhaseScore:
    phase_index: int
    score: float


def score_phases(
    intersection: Intersection,
    state: TrafficState,
    mode: str,
    config: ControlConfig,
    priority_link: int | None = None,
) -> tuple[PhaseScore, ...]:
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
                movement_score = (
                    float(incoming.queue)
                    + incoming.occupancy * 5.0
                    - outgoing.occupancy * config.downstream_weight
                    - outgoing.queue * 0.5
                    - min(outgoing.free_slots, 20.0) * 0.05
                )
                if outgoing.occupancy >= config.blocked_occupancy:
                    movement_score -= 25.0
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
