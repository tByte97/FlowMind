from __future__ import annotations

from dataclasses import dataclass, field

from .area_model import AreaModel, Intersection
from .config import ControlConfig
from .decision_feed import DecisionEvent
from .priority_flow import priority_links
from .queue_forecast import QueueForecastEnsemble, QueueForecastModel
from .safety_validator import SafetyValidator, clearance_duration
from .signal_policy import (
    PhaseScore,
    area_pressure_by_incoming_lane,
    choose_phase,
    effective_max_green,
    effective_min_green,
    lane_has_demand,
    score_phases,
)
from .traffic_state import TrafficState, TrafficStateReader
from .zone_graph import (
    AreaDecisionSnapshot,
    AreaGraph,
    build_area_decision_snapshot,
)


@dataclass
class QueueForecastSample:
    time: float
    tls_id: str
    candidates: int
    min_prediction: float
    mean_prediction: float
    max_prediction: float


@dataclass(frozen=True)
class PreparedIntersectionDecision:
    intersection: Intersection
    current_phase: int
    spent: float
    max_green: float
    priority_link: int | None
    scores: tuple[PhaseScore, ...]


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
    sensor_failures: int = 0
    stale_lane_samples: int = 0
    invalid_state_skips: int = 0
    fallback_activations: int = 0
    safety_rejections: int = 0
    safety_rejection_reasons: dict[str, int] = field(default_factory=dict)
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
        area_graph: AreaGraph | None = None,
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
        self._area_graph = area_graph
        self._corridor_manager = None
        self._reader = TrafficStateReader(
            traci_connection,
            area,
            config.sensor_range_meters,
            area_graph.monitored_lane_ids if area_graph is not None else (),
            config.sensor_last_known_good_ttl,
        )
        self._safety = SafetyValidator(traci_connection, area, config)
        self._lane_demand_started_at: dict[str, float] = {}
        self._next_decision_at = float(config.decision_interval)
        self._fallback_active = False
        self.stats = ControllerStats()

    def set_corridor_manager(self, manager: object) -> None:
        self._corridor_manager = manager

    def step(self, simulation_time: float) -> None:
        if not self._decision_due(simulation_time):
            return

        traffic = self._reader.read(simulation_time)
        self.stats.sensor_failures += len(traffic.sensor_error_lane_ids)
        self.stats.stale_lane_samples += len(traffic.stale_lane_ids)
        if not traffic.usable:
            self.stats.invalid_state_skips += 1
            self._activate_fallback(simulation_time, traffic.invalid_lane_ids)
            return
        if self._fallback_active:
            self._fallback_active = False
            self._record_decision(
                simulation_time,
                "",
                "Сенсорні дані відновлено",
                "FlowMind повернувся з перевіреного TLS fallback до адаптивного керування.",
                "success",
            )
        demand_wait_by_lane = self._update_demand_timers(
            traffic,
            simulation_time,
        )
        snapshot = self._area_snapshot(traffic, simulation_time)
        if self._corridor_manager is not None:
            overrides = self._corridor_manager.get_priority_overrides()
        else:
            overrides = priority_links(
                self._traci, self._priority_vehicle, self._config
            )
        prepared: list[PreparedIntersectionDecision] = []
        for intersection in self._area.intersections:
            tls_id = intersection.tls_id
            current_phase = int(self._traci.trafficlight.getPhase(tls_id))
            if not 0 <= current_phase < len(intersection.phases):
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
                snapshot.area_pressure_by_incoming_lane,
                queue_forecast,
                demand_wait_by_lane,
                snapshot.downstream_risk_by_outgoing_lane,
            )
            prepared.append(
                PreparedIntersectionDecision(
                    intersection=intersection,
                    current_phase=current_phase,
                    spent=spent,
                    max_green=max_green,
                    priority_link=priority_link,
                    scores=scores,
                )
            )

        for decision in prepared:
            self._apply_prepared_decision(decision, simulation_time)

    def _activate_fallback(
        self,
        simulation_time: float,
        invalid_lane_ids: tuple[str, ...],
    ) -> None:
        if self._fallback_active:
            return
        trafficlight = self._traci.trafficlight
        set_program = getattr(trafficlight, "setProgram", None)
        if callable(set_program):
            for intersection in self._area.intersections:
                if intersection.program_id:
                    set_program(intersection.tls_id, intersection.program_id)
        self._fallback_active = True
        self.stats.fallback_activations += 1
        preview = ", ".join(invalid_lane_ids[:3])
        suffix = "…" if len(invalid_lane_ids) > 3 else ""
        self._record_decision(
            simulation_time,
            "",
            "Активовано перевірений TLS fallback",
            (
                f"Невалідні lane samples: {preview}{suffix}. "
                "Адаптивні команди призупинено."
            ),
            "warning",
        )

    def _decision_due(self, simulation_time: float) -> bool:
        now = float(simulation_time)
        if now + 1e-9 < self._next_decision_at:
            return False
        interval = float(self._config.decision_interval)
        elapsed_intervals = int((now - self._next_decision_at) // interval) + 1
        self._next_decision_at += elapsed_intervals * interval
        return True

    def _area_snapshot(
        self,
        traffic: TrafficState,
        simulation_time: float,
    ) -> AreaDecisionSnapshot:
        if self._area_graph is not None and self._mode == "flowmind":
            return build_area_decision_snapshot(
                self._area,
                self._area_graph,
                traffic,
                simulation_time,
                self._config,
            )
        return AreaDecisionSnapshot(
            simulation_time=float(simulation_time),
            traffic=traffic,
            segment_states={},
            node_states={},
            downstream_risk_by_outgoing_lane={},
            area_pressure_by_incoming_lane=(
                area_pressure_by_incoming_lane(
                    self._area,
                    traffic,
                    self._config,
                )
                if self._mode == "flowmind"
                else {}
            ),
        )

    def _apply_prepared_decision(
        self,
        decision: PreparedIntersectionDecision,
        simulation_time: float,
    ) -> None:
        intersection = decision.intersection
        tls_id = intersection.tls_id
        current_phase = decision.current_phase
        spent = decision.spent
        max_green = decision.max_green
        priority_link = decision.priority_link
        scores = decision.scores
        best = choose_phase(scores)
        if best is None:
            self.stats.scoreless_skips += 1
            self.stats.decisions += 1
            if priority_link is not None:
                self.stats.priority_decisions += 1
            self._advance_to_clearance(
                intersection,
                current_phase,
                simulation_time,
                spent,
                priority=False,
                title="Закрито зелений через заповнений downstream",
                detail=(
                    f"Перехрестя {tls_id}: безпечних зелених фаз "
                    "немає; поточний рух закрито через "
                    "clearance-фазу."
                ),
                level="warning",
            )
            return
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
                self._record_safety_rejection(
                    simulation_time,
                    tls_id,
                    safety.reason,
                )
                return
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
            self._advance_to_clearance(
                intersection,
                current_phase,
                simulation_time,
                spent,
                priority=priority_link is not None,
                title=(
                    "Підготовлено фазу для швидкої"
                    if priority_link is not None
                    else "Змінено фазу через стан черги"
                ),
                detail=(
                    f"Перехрестя {tls_id}: перехід із фази {current_phase} "
                    f"до {(current_phase + 1) % len(intersection.phases)}; "
                    f"найкраща оцінка {best.score:.2f}."
                ),
                level="warning" if priority_link is not None else "info",
            )

    def _advance_to_clearance(
        self,
        intersection: Intersection,
        current_phase: int,
        simulation_time: float,
        spent: float,
        *,
        priority: bool,
        title: str,
        detail: str,
        level: str,
    ) -> bool:
        tls_id = intersection.tls_id
        next_phase = (current_phase + 1) % len(intersection.phases)
        safety = self._safety.validate_transition(
            tls_id,
            current_phase,
            next_phase,
            simulation_time,
            spent_duration=spent,
            priority=priority,
        )
        if not safety.allowed:
            self._record_safety_rejection(
                simulation_time,
                tls_id,
                safety.reason,
            )
            return False
        self._traci.trafficlight.setPhase(tls_id, next_phase)
        next_state = intersection.phases[next_phase]
        if "y" in next_state.lower() or not any(
            signal in "GgYy" for signal in next_state
        ):
            self._traci.trafficlight.setPhaseDuration(
                tls_id,
                clearance_duration(
                    intersection,
                    next_phase,
                    self._config.clearance_seconds,
                ),
            )
        self.stats.advances += 1
        self._record_decision(
            simulation_time,
            tls_id,
            title,
            detail,
            level,
        )
        return True

    def _record_safety_rejection(
        self,
        simulation_time: float,
        tls_id: str,
        reason: str,
    ) -> None:
        self.stats.safety_rejections += 1
        self.stats.safety_rejection_reasons[reason] = (
            self.stats.safety_rejection_reasons.get(reason, 0) + 1
        )
        self._record_decision(
            simulation_time,
            tls_id,
            "Safety відхилив команду",
            f"Перехрестя {tls_id}: {reason}.",
            "warning",
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
