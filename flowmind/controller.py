from __future__ import annotations

from dataclasses import dataclass

from .area_model import AreaModel
from .config import ControlConfig
from .priority_flow import priority_links
from .signal_policy import choose_phase, score_phases
from .traffic_state import TrafficStateReader


@dataclass
class ControllerStats:
    decisions: int = 0
    extensions: int = 0
    advances: int = 0
    priority_decisions: int = 0


class AreaSignalController:
    """Adaptive controller that preserves SUMO's safe phase order.

    The controller extends a useful green phase or advances to the next phase
    in the existing program. Yellow/all-red transitions are never skipped.
    """

    def __init__(
        self,
        traci_connection: object,
        area: AreaModel,
        mode: str,
        config: ControlConfig,
        priority_vehicle: str | None = None,
    ) -> None:
        if mode not in {"local", "flowmind"}:
            raise ValueError("Adaptive controller mode must be local or flowmind")
        self._traci = traci_connection
        self._area = area
        self._mode = mode
        self._config = config
        self._priority_vehicle = priority_vehicle
        self._corridor_manager = None
        self._reader = TrafficStateReader(traci_connection, area)
        self.stats = ControllerStats()

    def set_corridor_manager(self, manager: object) -> None:
        self._corridor_manager = manager

    def step(self, simulation_time: float) -> None:
        if int(simulation_time) % self._config.decision_interval:
            return

        traffic = self._reader.read()
        if self._corridor_manager is not None:
            overrides = self._corridor_manager.get_priority_overrides()
        else:
            overrides = priority_links(
                self._traci, self._priority_vehicle, self._config
            )
        for intersection in self._area.intersections:
            tls_id = intersection.tls_id
            current_phase = int(self._traci.trafficlight.getPhase(tls_id))
            if current_phase >= len(intersection.phases):
                continue
            current_state = intersection.phases[current_phase]
            if "y" in current_state.lower() or not any(
                signal in "Gg" for signal in current_state
            ):
                continue

            spent = float(self._traci.trafficlight.getSpentDuration(tls_id))
            if spent < self._config.min_green:
                continue

            priority_link = overrides.get(tls_id)
            scores = score_phases(
                intersection,
                traffic,
                self._mode,
                self._config,
                priority_link,
            )
            best = choose_phase(scores)
            if best is None:
                continue
            current_score = next(
                (item.score for item in scores if item.phase_index == current_phase),
                float("-inf"),
            )

            self.stats.decisions += 1
            if priority_link is not None:
                self.stats.priority_decisions += 1

            should_extend = (
                best.phase_index == current_phase
                or current_score >= best.score - self._config.hysteresis
            ) and spent < self._config.max_green
            if should_extend:
                remaining = min(
                    float(self._config.decision_interval),
                    float(self._config.max_green) - spent,
                )
                self._traci.trafficlight.setPhaseDuration(tls_id, max(remaining, 1.0))
                self.stats.extensions += 1
            else:
                next_phase = (current_phase + 1) % len(intersection.phases)
                self._traci.trafficlight.setPhase(tls_id, next_phase)
                self.stats.advances += 1
