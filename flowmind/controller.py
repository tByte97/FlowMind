from __future__ import annotations

from dataclasses import dataclass, field

from .area_model import AreaModel
from .config import ControlConfig
from .decision_feed import DecisionEvent
from .priority_flow import priority_links
from .queue_forecast import QueueForecastEnsemble, QueueForecastModel
from .safety_validator import SafetyValidator
from .signal_policy import (
    area_pressure_by_incoming_lane,
    choose_phase,
    effective_max_green,
    effective_min_green,
    lane_has_demand,
    score_phases,
)
from .traffic_state import TrafficStateReader


@dataclass
class QueueForecastSample:
    time: float
    tls_id: str
    candidates: int
    min_prediction: float
    mean_prediction: float
    max_prediction: float


@dataclass
class ControllerStats:
    decisions: int = 0
    extensions: int = 0
    advances: int = 0
    priority_decisions: int = 0
    phase_out_of_range_skips: int = 0
    clearance_phase_skips: int = 0
    min_green_skips: int = 0
    scoreless_skips: int = 0
    queue_forecast_predictions: int = 0
    queue_forecast_failures: int = 0
    queue_forecast_samples: list[QueueForecastSample] = field(default_factory=list)
    decision_events: list[DecisionEvent] = field(default_factory=list)


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
        queue_forecast: QueueForecastModel | QueueForecastEnsemble | None = None,
        queue_forecast_sample_interval: int = 5,
    ) -> None:
        if mode not in {"local", "flowmind"}:
            raise ValueError("Adaptive controller mode must be local or flowmind")
        self._traci = traci_connection
        self._area = area
        self._mode = mode
        self._config = config
        self._priority_vehicle = priority_vehicle
        self._queue_forecast = queue_forecast if mode == "flowmind" else None
        self._queue_forecast_sample_interval = queue_forecast_sample_interval
        self._corridor_manager = None
        self._reader = TrafficStateReader(
            traci_connection,
            area,
            config.sensor_range_meters,
        )
        self._safety = SafetyValidator(traci_connection, area, config)
        self._lane_demand_started_at: dict[str, float] = {}
        self.stats = ControllerStats()

    def set_corridor_manager(self, manager: object) -> None:
        self._corridor_manager = manager

    def step(self, simulation_time: float) -> None:
        if int(simulation_time) % self._config.decision_interval:
            return

        traffic = self._reader.read()
        demand_wait_by_lane = self._update_demand_timers(
            traffic,
            simulation_time,
        )
        area_pressure = (
            area_pressure_by_incoming_lane(self._area, traffic, self._config)
            if self._mode == "flowmind"
            else {}
        )
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
                self.stats.phase_out_of_range_skips += 1
                continue
            current_state = intersection.phases[current_phase]
            if "y" in current_state.lower() or not any(
                signal in "Gg" for signal in current_state
            ):
                self.stats.clearance_phase_skips += 1
                continue

            spent = float(self._traci.trafficlight.getSpentDuration(tls_id))
            min_green = effective_min_green(self._config, intersection, current_phase)
            max_green = effective_max_green(self._config, intersection, current_phase)
            if spent < min_green:
                self.stats.min_green_skips += 1
                continue

            priority_link = overrides.get(tls_id)
            queue_forecast = {}
            if self._queue_forecast is not None:
                try:
                    queue_forecast = self._queue_forecast.predict_intersection(
                        mode=self._mode,
                        simulation_time=simulation_time,
                        intersection=intersection,
                        state=traffic,
                        current_phase=current_phase,
                        phase_elapsed=spent,
                        control=self._config,
                        sample_interval=self._queue_forecast_sample_interval,
                    )
                    self.stats.queue_forecast_predictions += len(queue_forecast)
                    if queue_forecast:
                        values = tuple(queue_forecast.values())
                        self.stats.queue_forecast_samples.append(
                            QueueForecastSample(
                                time=round(simulation_time, 3),
                                tls_id=tls_id,
                                candidates=len(values),
                                min_prediction=round(min(values), 5),
                                mean_prediction=round(sum(values) / len(values), 5),
                                max_prediction=round(max(values), 5),
                            )
                        )
                except Exception:
                    self.stats.queue_forecast_failures += 1

            scores = score_phases(
                intersection,
                traffic,
                self._mode,
                self._config,
                priority_link,
                area_pressure,
                queue_forecast,
                demand_wait_by_lane,
            )
            best = choose_phase(scores)
            if best is None:
                self.stats.scoreless_skips += 1
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
            ) and spent < max_green
            if should_extend:
                safety = self._safety.validate_extension(
                    tls_id,
                    current_phase,
                    simulation_time,
                    spent_duration=spent,
                    priority=priority_link is not None,
                )
                if not safety.allowed:
                    continue
                remaining = min(
                    float(self._config.decision_interval),
                    max_green - spent,
                )
                self._traci.trafficlight.setPhaseDuration(
                    tls_id,
                    max(remaining, 1.0),
                )
                self.stats.extensions += 1
                self._record_decision(
                    simulation_time,
                    tls_id,
                    (
                        "Продовжено зелений для швидкої"
                        if priority_link is not None
                        else "Продовжено зелену фазу"
                    ),
                    (
                        f"Перехрестя {tls_id}: фаза {current_phase} продовжена "
                        f"на {max(remaining, 1.0):.0f} с; оцінка попиту "
                        f"{best.score:.2f}."
                    ),
                    "success" if priority_link is not None else "info",
                )
            else:
                next_phase = (current_phase + 1) % len(intersection.phases)
                safety = self._safety.validate_transition(
                    tls_id,
                    current_phase,
                    next_phase,
                    simulation_time,
                    spent_duration=spent,
                    priority=priority_link is not None,
                )
                if not safety.allowed:
                    continue
                self._traci.trafficlight.setPhase(tls_id, next_phase)
                self.stats.advances += 1
                self._record_decision(
                    simulation_time,
                    tls_id,
                    (
                        "Підготовлено фазу для швидкої"
                        if priority_link is not None
                        else "Змінено фазу через стан черги"
                    ),
                    (
                        f"Перехрестя {tls_id}: перехід із фази {current_phase} "
                        f"до {next_phase}; найкраща оцінка {best.score:.2f}."
                    ),
                    "warning" if priority_link is not None else "info",
                )

    def _update_demand_timers(
        self,
        traffic: object,
        simulation_time: float,
    ) -> dict[str, float]:
        active: dict[str, float] = {}
        for lane_id in self._area.incoming_lanes:
            if lane_has_demand(traffic.lane(lane_id), self._config):
                started_at = self._lane_demand_started_at.setdefault(
                    lane_id,
                    simulation_time,
                )
                active[lane_id] = max(simulation_time - started_at, 0.0)
            else:
                self._lane_demand_started_at.pop(lane_id, None)
        return active

    def _record_decision(
        self,
        simulation_time: float,
        tls_id: str,
        title: str,
        detail: str,
        level: str,
    ) -> None:
        self.stats.decision_events.append(
            DecisionEvent(
                time=round(simulation_time, 3),
                category="controller",
                title=title,
                detail=detail,
                level=level,
                tls_id=tls_id,
            )
        )
        self.stats.decision_events = self.stats.decision_events[-100:]
