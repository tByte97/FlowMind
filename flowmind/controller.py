from __future__ import annotations

from dataclasses import dataclass, field

from .area_model import AreaModel, Intersection
from .config import ControlConfig
from .corridor_recovery import CorridorRecoveryPlanner
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
    movement_has_blocked_downstream,
    score_phases,
)
from .traffic_state import TrafficState, TrafficStateReader
from .zone_graph import (
    AreaDecisionSnapshot,
    AreaGraph,
    build_area_decision_snapshot,
)
from .zone_optimizer import optimize_zone_phases


@dataclass
class QueueForecastSample:
    time: float
    tls_id: str
    candidates: int
    min_prediction: float
    mean_prediction: float
    max_prediction: float
    horizon_seconds: int
    evaluation_time: float
    forecast_contract: str
    prediction_target: str
    confidence: float
    ood: bool
    diagnostic_reasons: str
    shadow: bool
    used_for_control: bool
    observed_time: float | None = None
    observed_mean: float | None = None
    mean_absolute_error: float | None = None


@dataclass
class PendingQueueForecast:
    sample: QueueForecastSample
    lane_predictions: tuple[tuple[str, float, float, float], ...]


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
    queue_forecast_shadow_predictions: int = 0
    queue_forecast_control_predictions: int = 0
    queue_forecast_ood_predictions: int = 0
    queue_forecast_shadow_evaluations: int = 0
    queue_forecast_shadow_absolute_error: float = 0.0
    queue_forecast_rejection_reasons: dict[str, int] = field(default_factory=dict)
    sensor_failures: int = 0
    stale_lane_samples: int = 0
    invalid_state_skips: int = 0
    fallback_activations: int = 0
    safety_rejections: int = 0
    safety_rejection_reasons: dict[str, int] = field(default_factory=dict)
    corridor_preparation_targets: int = 0
    corridor_downstream_blocks: int = 0
    corridor_recovery_actions: int = 0
    queue_forecast_samples: list[QueueForecastSample] = field(default_factory=list)
    decision_events: list[DecisionEvent] = field(default_factory=list)

    @property
    def queue_forecast_shadow_mae(self) -> float | None:
        if self.queue_forecast_shadow_evaluations <= 0:
            return None
        return (
            self.queue_forecast_shadow_absolute_error
            / self.queue_forecast_shadow_evaluations
        )


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
        self._lane_history: dict[str, list[tuple[float, int, int]]] = {}
        self._next_decision_at = float(config.decision_interval)
        self._fallback_tls_ids: set[str] = set()
        self._pending_queue_forecasts: list[PendingQueueForecast] = []
        self._corridor_recovery = CorridorRecoveryPlanner()
        self.stats = ControllerStats()

    def set_corridor_manager(self, manager: object) -> None:
        self._corridor_manager = manager

    def step(self, simulation_time: float) -> None:
        if not self._decision_due(simulation_time):
            return

        traffic = self._reader.read(simulation_time)
        self.stats.sensor_failures += len(traffic.sensor_error_lane_ids)
        self.stats.stale_lane_samples += len(traffic.stale_lane_ids)
        self._resolve_pending_queue_forecasts(traffic, simulation_time)
        history_context = self._historical_context(traffic, simulation_time)
        queue_growth_by_lane = {
            lane_id: values.get("incoming_queue_growth_30s", 0.0)
            for lane_id, values in history_context.items()
        }
        demand_wait_by_lane = self._update_demand_timers(
            traffic,
            simulation_time,
        )
        snapshot = self._area_snapshot(traffic, simulation_time)
        recovery_handled = self._recover_corridor_offsets(simulation_time)
        if self._corridor_manager is not None:
            overrides = self._filter_corridor_targets(
                self._corridor_manager.get_priority_overrides(),
                traffic,
                snapshot,
                simulation_time,
                priority=True,
            )
            preparation_overrides = self._filter_corridor_targets(
                self._corridor_manager.get_preparation_overrides(),
                traffic,
                snapshot,
                simulation_time,
                priority=False,
            )
            self.stats.corridor_preparation_targets += len(preparation_overrides)
        else:
            overrides = priority_links(
                self._traci, self._priority_vehicle, self._config
            )
            preparation_overrides = {}
        corridor_target_ids = set(overrides) | set(preparation_overrides)
        prepared: list[PreparedIntersectionDecision] = []
        for intersection in self._area.intersections:
            tls_id = intersection.tls_id
            if tls_id in recovery_handled:
                continue
            invalid_lane_ids = tuple(
                sorted(
                    lane_id
                    for link in intersection.links
                    for lane_id in (link.incoming_lane, link.outgoing_lane)
                    if not traffic.lane(lane_id).valid
                )
            )
            if invalid_lane_ids:
                self.stats.invalid_state_skips += 1
                self._activate_fallback(
                    tls_id,
                    simulation_time,
                    invalid_lane_ids,
                )
                continue
            self._deactivate_fallback(tls_id, simulation_time)
            current_phase = int(self._traci.trafficlight.getPhase(tls_id))
            if not 0 <= current_phase < len(intersection.phases):
                self.stats.phase_out_of_range_skips += 1
                continue
            spent = float(self._traci.trafficlight.getSpentDuration(tls_id))
            if tls_id in corridor_target_ids:
                self._corridor_recovery.capture(
                    intersection,
                    current_phase,
                    spent,
                    simulation_time,
                )
            current_state = intersection.phases[current_phase]
            if "y" in current_state.lower() or not any(
                signal in "Gg" for signal in current_state
            ):
                self.stats.clearance_phase_skips += 1
                continue

            min_green = effective_min_green(self._config, intersection, current_phase)
            max_green = effective_max_green(self._config, intersection, current_phase)
            if spent < min_green:
                self.stats.min_green_skips += 1
                continue

            priority_link = overrides.get(tls_id)
            preparation_link = preparation_overrides.get(tls_id)
            queue_forecast: dict[tuple[int, int], float] = {}
            if self._queue_forecast is not None:
                try:
                    raw_forecast = self._queue_forecast.predict_intersection(
                        mode=self._mode,
                        simulation_time=simulation_time,
                        intersection=intersection,
                        state=traffic,
                        current_phase=current_phase,
                        phase_elapsed=spent,
                        control=self._config,
                        sample_interval=self._queue_forecast_sample_interval,
                        context_by_lane=history_context,
                        area_context_by_lane=self._area_model_context(snapshot),
                    )
                    diagnostics = self._queue_forecast.last_diagnostics
                    count = len(raw_forecast)
                    self.stats.queue_forecast_predictions += count
                    if diagnostics.ood:
                        self.stats.queue_forecast_ood_predictions += count
                    influence_allowed = (
                        not self._config.queue_forecast_shadow_mode
                        and diagnostics.influence_allowed
                        and diagnostics.confidence
                        >= self._config.queue_forecast_min_confidence
                    )
                    rejection_reasons = list(diagnostics.reasons)
                    if (
                        diagnostics.confidence
                        < self._config.queue_forecast_min_confidence
                    ):
                        rejection_reasons.append("confidence_below_threshold")
                    if self._config.queue_forecast_shadow_mode:
                        self.stats.queue_forecast_shadow_predictions += count
                    elif influence_allowed:
                        self.stats.queue_forecast_control_predictions += count
                    for reason in dict.fromkeys(rejection_reasons):
                        self.stats.queue_forecast_rejection_reasons[reason] = (
                            self.stats.queue_forecast_rejection_reasons.get(reason, 0)
                            + 1
                        )
                    if influence_allowed:
                        queue_forecast = raw_forecast
                    if raw_forecast:
                        self._record_queue_forecast(
                            simulation_time,
                            intersection,
                            current_phase,
                            traffic,
                            raw_forecast,
                            diagnostics.forecast_contract,
                            diagnostics.confidence,
                            diagnostics.ood,
                            tuple(dict.fromkeys(rejection_reasons)),
                            influence_allowed,
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
                preparation_link,
                queue_growth_by_lane,
                snapshot.platoon_arrival_by_incoming_lane,
                snapshot.downstream_storage_by_outgoing_lane,
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

        zone_choices = (
            optimize_zone_phases(
                self._area,
                self._area_graph,
                snapshot,
                {
                    decision.intersection.tls_id: decision.scores
                    for decision in prepared
                },
                self._config,
            )
            if self._mode == "flowmind" and self._area_graph is not None
            else {}
        )
        for decision in prepared:
            choice = zone_choices.get(decision.intersection.tls_id)
            self._apply_prepared_decision(
                decision,
                simulation_time,
                target_phase=(choice.phase_index if choice is not None else None),
            )
        self._remember_traffic(traffic, simulation_time)

    def _filter_corridor_targets(
        self,
        targets: dict[str, int],
        traffic: TrafficState,
        snapshot: AreaDecisionSnapshot,
        simulation_time: float,
        *,
        priority: bool,
    ) -> dict[str, int]:
        if self._corridor_manager is None:
            return targets
        accepted: dict[str, int] = {}
        for tls_id, signal_index in targets.items():
            try:
                intersection = self._area.intersection(tls_id)
            except StopIteration:
                reason = "TLS outside controlled emergency zone"
            else:
                movements = tuple(
                    link
                    for link in intersection.links
                    if link.signal_index == signal_index
                )
                required_slots = (
                    self._config.priority_min_storage_slots
                    if priority
                    else self._config.min_downstream_storage_slots
                )
                has_safe_exit = bool(movements) and all(
                    (
                        not movement_has_blocked_downstream(
                            traffic.lane(link.outgoing_lane),
                            self._config,
                            required_storage_slots=required_slots,
                        )
                        and snapshot.downstream_risk_by_outgoing_lane.get(
                            link.outgoing_lane,
                            0.0,
                        )
                        < self._config.spillback_hard_gate_probability
                    )
                    for link in movements
                )
                if movements and has_safe_exit:
                    accepted[tls_id] = signal_index
                    continue
                reason = (
                    "no controlled movement for TLS signal index"
                    if not movements
                    else "downstream storage or graph spillback is blocked"
                )
            self.stats.corridor_downstream_blocks += 1
            self._corridor_manager.record_downstream_block(
                tls_id,
                reason,
                simulation_time,
            )
        return accepted

    def _recover_corridor_offsets(self, simulation_time: float) -> set[str]:
        manager = self._corridor_manager
        if manager is None or getattr(manager.state, "name", "") != "RECOVERY":
            return set()
        handled: set[str] = set()
        for tls_id in tuple(manager.recovery_tls):
            try:
                intersection = self._area.intersection(tls_id)
            except StopIteration:
                manager.confirm_recovery(tls_id, simulation_time)
                continue
            target = self._corridor_recovery.target(tls_id, simulation_time)
            if target is None:
                manager.confirm_recovery(tls_id, simulation_time)
                continue
            handled.add(tls_id)
            current_phase = int(self._traci.trafficlight.getPhase(tls_id))
            if not 0 <= current_phase < len(intersection.phases):
                continue
            spent = float(self._traci.trafficlight.getSpentDuration(tls_id))
            if current_phase == target.phase_index:
                self._traci.trafficlight.setPhaseDuration(
                    tls_id,
                    max(target.remaining_duration, 1.0),
                )
                self._corridor_recovery.mark_restored(tls_id)
                manager.confirm_recovery(tls_id, simulation_time)
                self.stats.corridor_recovery_actions += 1
                self._record_decision(
                    simulation_time,
                    tls_id,
                    "Відновлено базовий фазовий offset",
                    (
                        f"Перехрестя {tls_id}: фаза {target.phase_index}, "
                        f"залишок {target.remaining_duration:.1f} с."
                    ),
                    "success",
                )
                continue
            state = intersection.phases[current_phase]
            if (
                any(signal in "Gg" for signal in state)
                and "y" not in state.lower()
                and spent
                >= effective_min_green(self._config, intersection, current_phase)
            ):
                if self._advance_to_clearance(
                    intersection,
                    current_phase,
                    simulation_time,
                    spent,
                    priority=False,
                    title="Recovery: перехід до базового offset",
                    detail=(
                        f"Перехрестя {tls_id}: безпечне просування до "
                        f"цільової фази {target.phase_index}."
                    ),
                    level="info",
                ):
                    self.stats.corridor_recovery_actions += 1
        return handled

    def _record_queue_forecast(
        self,
        simulation_time: float,
        intersection: Intersection,
        current_phase: int,
        traffic: TrafficState,
        predictions: dict[tuple[int, int], float],
        forecast_contract: str,
        confidence: float,
        ood: bool,
        reasons: tuple[str, ...],
        used_for_control: bool,
    ) -> None:
        # Shadow accuracy is measurable only for the action SUMO actually
        # executed. Alternative candidate phases remain counterfactual and are
        # never compared with the observed outcome of another phase.
        by_link = {
            link_index: prediction
            for (phase_index, link_index), prediction in predictions.items()
            if phase_index == current_phase
        }
        lane_predictions = tuple(
            (
                intersection.links[link_index].incoming_lane,
                prediction,
                float(traffic.lane(
                    intersection.links[link_index].incoming_lane
                ).queue),
                float(traffic.lane(
                    intersection.links[link_index].incoming_lane
                ).vehicle_count),
            )
            for link_index, prediction in by_link.items()
            if 0 <= link_index < len(intersection.links)
        )
        if not lane_predictions:
            return
        values = tuple(
            prediction
            for _lane_id, prediction, _initial_queue, _initial_count in lane_predictions
        )
        horizon = self._queue_forecast.evaluation_horizon_seconds
        prediction_target = getattr(
            self._queue_forecast,
            "prediction_target",
            "target_incoming_queue",
        )
        sample = QueueForecastSample(
            time=round(simulation_time, 3),
            tls_id=intersection.tls_id,
            candidates=len(values),
            min_prediction=round(min(values), 5),
            mean_prediction=round(sum(values) / len(values), 5),
            max_prediction=round(max(values), 5),
            horizon_seconds=horizon,
            evaluation_time=round(simulation_time + horizon, 3),
            forecast_contract=forecast_contract,
            prediction_target=prediction_target,
            confidence=round(confidence, 5),
            ood=ood,
            diagnostic_reasons=";".join(reasons),
            shadow=self._config.queue_forecast_shadow_mode,
            used_for_control=used_for_control,
        )
        self.stats.queue_forecast_samples.append(sample)
        self._pending_queue_forecasts.append(
            PendingQueueForecast(sample, lane_predictions)
        )

    def _resolve_pending_queue_forecasts(
        self,
        traffic: TrafficState,
        simulation_time: float,
    ) -> None:
        pending: list[PendingQueueForecast] = []
        for item in self._pending_queue_forecasts:
            if simulation_time + 1e-9 < item.sample.evaluation_time:
                pending.append(item)
                continue
            observations = []
            for lane_id, prediction, initial_queue, initial_count in item.lane_predictions:
                lane = traffic.lane(lane_id)
                if not lane.valid:
                    continue
                observations.append(
                    (
                        _observed_forecast_target(
                            item.sample.prediction_target,
                            initial_queue,
                            initial_count,
                            float(lane.queue),
                            float(lane.vehicle_count),
                        ),
                        float(prediction),
                    )
                )
            if not observations:
                pending.append(item)
                continue
            actual_mean = sum(actual for actual, _prediction in observations) / len(
                observations
            )
            mae = sum(
                abs(actual - prediction) for actual, prediction in observations
            ) / len(observations)
            item.sample.observed_time = round(simulation_time, 3)
            item.sample.observed_mean = round(actual_mean, 5)
            item.sample.mean_absolute_error = round(mae, 5)
            if item.sample.shadow:
                self.stats.queue_forecast_shadow_evaluations += 1
                self.stats.queue_forecast_shadow_absolute_error += mae
        self._pending_queue_forecasts = pending
    def _activate_fallback(
        self,
        tls_id: str,
        simulation_time: float,
        invalid_lane_ids: tuple[str, ...],
    ) -> None:
        if tls_id in self._fallback_tls_ids:
            return
        trafficlight = self._traci.trafficlight
        set_program = getattr(trafficlight, "setProgram", None)
        if callable(set_program):
            intersection = self._area.intersection(tls_id)
            if intersection.program_id:
                set_program(intersection.tls_id, intersection.program_id)
        self._fallback_tls_ids.add(tls_id)
        self.stats.fallback_activations += 1
        preview = ", ".join(invalid_lane_ids[:3])
        suffix = "…" if len(invalid_lane_ids) > 3 else ""
        self._record_decision(
            simulation_time,
            tls_id,
            "Активовано перевірений TLS fallback",
            (
                f"Перехрестя {tls_id}; невалідні lane samples: "
                f"{preview}{suffix}. Адаптивні команди призупинено лише для TLS."
            ),
            "warning",
        )

    def _deactivate_fallback(self, tls_id: str, simulation_time: float) -> None:
        if tls_id not in self._fallback_tls_ids:
            return
        self._fallback_tls_ids.remove(tls_id)
        self._record_decision(
            simulation_time,
            tls_id,
            "Сенсорні дані TLS відновлено",
            f"Перехрестя {tls_id} повернулося до адаптивного керування.",
            "success",
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
            platoon_arrival_by_incoming_lane={},
            downstream_storage_by_outgoing_lane={},
            upstream_queue_by_incoming_lane={},
            downstream_occupancy_by_outgoing_lane={},
        )

    def _historical_context(
        self,
        traffic: TrafficState,
        simulation_time: float,
    ) -> dict[str, dict[str, float]]:
        result: dict[str, dict[str, float]] = {}
        for lane_id, lane in traffic.lanes.items():
            values: dict[str, float] = {}
            history = self._lane_history.get(lane_id, ())
            for window in (15, 30):
                previous = next(
                    (
                        item
                        for item in reversed(history)
                        if item[0] <= simulation_time - window + 1e-9
                    ),
                    None,
                )
                previous_queue = previous[1] if previous is not None else lane.queue
                previous_count = (
                    previous[2] if previous is not None else lane.vehicle_count
                )
                elapsed = max(
                    simulation_time - previous[0]
                    if previous is not None
                    else float(window),
                    1.0,
                )
                delta = lane.vehicle_count - previous_count
                values[f"incoming_queue_growth_{window}s"] = float(
                    lane.queue - previous_queue
                )
                values[f"arrival_rate_{window}s"] = max(delta, 0) / elapsed
                values[f"discharge_rate_{window}s"] = max(-delta, 0) / elapsed
            result[lane_id] = values
        return result

    def _remember_traffic(
        self,
        traffic: TrafficState,
        simulation_time: float,
    ) -> None:
        for lane_id, lane in traffic.lanes.items():
            history = self._lane_history.setdefault(lane_id, [])
            history.append((simulation_time, lane.queue, lane.vehicle_count))
            while len(history) > 1 and history[1][0] < simulation_time - 35.0:
                history.pop(0)

    @staticmethod
    def _area_model_context(
        snapshot: AreaDecisionSnapshot,
    ) -> dict[str, dict[str, float]]:
        lane_ids = (
            set(snapshot.platoon_arrival_by_incoming_lane)
            | set(snapshot.downstream_storage_by_outgoing_lane)
            | set(snapshot.upstream_queue_by_incoming_lane)
            | set(snapshot.downstream_occupancy_by_outgoing_lane)
        )
        return {
            lane_id: {
                "platoon_arrival_30s": snapshot.platoon_arrival_by_incoming_lane.get(
                    lane_id,
                    0.0,
                ),
                "downstream_storage_slots": (
                    snapshot.downstream_storage_by_outgoing_lane.get(lane_id, 0.0)
                ),
                "upstream_neighbour_queue": snapshot.upstream_queue_by_incoming_lane.get(
                    lane_id,
                    0.0,
                ),
                "downstream_neighbour_occupancy": (
                    snapshot.downstream_occupancy_by_outgoing_lane.get(lane_id, 0.0)
                ),
            }
            for lane_id in lane_ids
        }

    def _apply_prepared_decision(
        self,
        decision: PreparedIntersectionDecision,
        simulation_time: float,
        target_phase: int | None = None,
    ) -> None:
        intersection = decision.intersection
        tls_id = intersection.tls_id
        current_phase = decision.current_phase
        spent = decision.spent
        max_green = decision.max_green
        priority_link = decision.priority_link
        scores = decision.scores
        best = (
            next(
                (item for item in scores if item.phase_index == target_phase),
                None,
            )
            if target_phase is not None
            else choose_phase(scores)
        )
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
            (
                best.phase_index == current_phase
                if target_phase is not None
                else (
                    best.phase_index == current_phase
                    or current_score >= best.score - self._config.hysteresis
                )
            )
            and spent < max_green
        )
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


def _observed_forecast_target(
    target: str,
    initial_queue: float,
    initial_vehicle_count: float,
    observed_queue: float,
    observed_vehicle_count: float,
) -> float:
    if "queue_reduction" in target:
        return initial_queue - observed_queue
    if "delta_queue" in target:
        return observed_queue - initial_queue
    if "discharged_vehicles" in target:
        return max(initial_vehicle_count - observed_vehicle_count, 0.0)
    return observed_queue
