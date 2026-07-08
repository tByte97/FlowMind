from __future__ import annotations

from dataclasses import dataclass

from .area_model import AreaModel
from .config import ControlConfig
from .signal_policy import effective_max_green, effective_min_green


@dataclass(frozen=True)
class SafetyDecision:
    allowed: bool
    reason: str = "ok"


class SafetyValidator:
    """Validate signal phase decisions before TraCI applies them.

    FlowMind deliberately avoids arbitrary signal states. The validator
    enforces that policy at the controller boundary: only the current phase can
    be extended, or the immediately next phase in SUMO's own program can be
    selected. That preserves yellow/all-red transitions in the existing plan.
    """

    def __init__(
        self,
        traci_connection: object,
        area: AreaModel,
        config: ControlConfig,
    ) -> None:
        self._traci = traci_connection
        self._area = area
        self._config = config
        self._priority_started_at: dict[str, float] = {}

    def validate_transition(
        self,
        tls_id: str,
        current_phase: int,
        target_phase: int,
        simulation_time: float,
        *,
        spent_duration: float | None = None,
        priority: bool = False,
    ) -> SafetyDecision:
        """Check whether a phase transition is allowed."""

        try:
            intersection = self._area.intersection(tls_id)
        except StopIteration:
            return SafetyDecision(False, "unknown traffic light")

        phase_count = len(intersection.phases)
        if current_phase < 0 or current_phase >= phase_count:
            return SafetyDecision(False, "current phase out of range")
        if target_phase < 0 or target_phase >= phase_count:
            return SafetyDecision(False, "target phase out of range")

        current_state = intersection.phases[current_phase]
        target_state = intersection.phases[target_phase]
        min_green = effective_min_green(self._config, intersection, current_phase)
        max_green = effective_max_green(self._config, intersection, current_phase)
        if self._is_malformed_state(current_state, intersection):
            return SafetyDecision(False, "current phase signal state is malformed")
        if self._is_malformed_state(target_state, intersection):
            return SafetyDecision(False, "target phase signal state is malformed")

        spent = (
            float(spent_duration)
            if spent_duration is not None
            else self._spent_duration(tls_id)
        )
        if priority and not self._priority_override_allowed(
            tls_id, simulation_time
        ):
            return SafetyDecision(False, "priority override timeout")
        if not priority:
            self._priority_started_at.pop(tls_id, None)

        if target_phase == current_phase:
            if spent >= max_green and any(
                signal in "Gg" for signal in current_state
            ):
                return SafetyDecision(False, "max green reached")
            return SafetyDecision(True)

        expected_next = (current_phase + 1) % phase_count
        if target_phase != expected_next:
            return SafetyDecision(False, "phase skip is not allowed")

        if any(signal in "Gg" for signal in current_state):
            if spent < min_green:
                return SafetyDecision(False, "min green not satisfied")
        elif (
            "y" in current_state.lower()
            and spent < self._config.clearance_seconds
        ):
            return SafetyDecision(False, "yellow clearance not satisfied")
        elif not any(signal in "GgYy" for signal in current_state):
            if spent < self._config.clearance_seconds:
                return SafetyDecision(False, "all-red clearance not satisfied")

        return SafetyDecision(True)

    def validate_extension(
        self,
        tls_id: str,
        current_phase: int,
        simulation_time: float,
        *,
        spent_duration: float | None = None,
        priority: bool = False,
    ) -> SafetyDecision:
        return self.validate_transition(
            tls_id,
            current_phase,
            current_phase,
            simulation_time,
            spent_duration=spent_duration,
            priority=priority,
        )

    def is_transition_safe(
        self,
        tls_id: str,
        current_phase: int,
        target_phase: int,
    ) -> bool:
        """Backward-compatible boolean API used by simple tests/callers."""

        return self.validate_transition(
            tls_id,
            current_phase,
            target_phase,
            0.0,
        ).allowed

    def _spent_duration(self, tls_id: str) -> float:
        try:
            return float(self._traci.trafficlight.getSpentDuration(tls_id))
        except Exception:
            return 0.0

    def _priority_override_allowed(
        self, tls_id: str, simulation_time: float
    ) -> bool:
        started_at = self._priority_started_at.setdefault(tls_id, simulation_time)
        return (
            simulation_time - started_at
            <= float(self._config.max_priority_override)
        )

    @staticmethod
    def _is_malformed_state(state: str, intersection: object) -> bool:
        signal_indices = [
            link.signal_index
            for link in getattr(intersection, "links", ())
        ]
        return bool(signal_indices) and max(signal_indices) >= len(state)
