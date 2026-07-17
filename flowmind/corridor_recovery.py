from __future__ import annotations

from dataclasses import dataclass

from .area_model import Intersection


@dataclass(frozen=True)
class RecoveryTarget:
    tls_id: str
    phase_index: int
    phase_elapsed: float
    remaining_duration: float


@dataclass(frozen=True)
class _BaselinePlan:
    tls_id: str
    phase_durations: tuple[float, ...]
    cycle_offset: float
    captured_at: float


class CorridorRecoveryPlanner:
    """Track the baseline cycle offset before emergency intervention."""

    def __init__(self) -> None:
        self._plans: dict[str, _BaselinePlan] = {}

    @property
    def pending_tls(self) -> tuple[str, ...]:
        return tuple(self._plans)

    def capture(
        self,
        intersection: Intersection,
        phase_index: int,
        phase_elapsed: float,
        simulation_time: float,
    ) -> None:
        tls_id = intersection.tls_id
        if tls_id in self._plans or not 0 <= phase_index < len(intersection.phases):
            return
        durations = tuple(
            max(float(duration), 1.0)
            for duration in (
                intersection.phase_durations
                or tuple(1.0 for _phase in intersection.phases)
            )
        )
        offset = sum(durations[:phase_index]) + min(
            max(float(phase_elapsed), 0.0),
            durations[phase_index],
        )
        self._plans[tls_id] = _BaselinePlan(
            tls_id=tls_id,
            phase_durations=durations,
            cycle_offset=offset,
            captured_at=float(simulation_time),
        )

    def target(self, tls_id: str, simulation_time: float) -> RecoveryTarget | None:
        plan = self._plans.get(tls_id)
        if plan is None:
            return None
        cycle = sum(plan.phase_durations)
        offset = (
            plan.cycle_offset + max(float(simulation_time) - plan.captured_at, 0.0)
        ) % cycle
        elapsed_before = 0.0
        for phase_index, duration in enumerate(plan.phase_durations):
            if offset < elapsed_before + duration:
                elapsed = offset - elapsed_before
                return RecoveryTarget(
                    tls_id=tls_id,
                    phase_index=phase_index,
                    phase_elapsed=elapsed,
                    remaining_duration=max(duration - elapsed, 1.0),
                )
            elapsed_before += duration
        return RecoveryTarget(
            tls_id=tls_id,
            phase_index=0,
            phase_elapsed=0.0,
            remaining_duration=plan.phase_durations[0],
        )

    def mark_restored(self, tls_id: str) -> None:
        self._plans.pop(tls_id, None)


__all__ = ["CorridorRecoveryPlanner", "RecoveryTarget"]
