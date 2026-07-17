from __future__ import annotations

from dataclasses import asdict, dataclass
from math import isfinite
from typing import Protocol


VALID_SIGNAL_STATES = frozenset("rRyYgGsuoO")


@dataclass(frozen=True)
class Movement:
    """Technology-neutral movement controlled by one signal index."""

    movement_id: str
    signal_index: int
    incoming_lane: str
    outgoing_lane: str
    direction: str = ""


@dataclass(frozen=True)
class MovementConflict:
    first_movement_id: str
    second_movement_id: str
    reason: str = "right_of_way_foe"

    @property
    def pair(self) -> tuple[str, str]:
        return tuple(sorted((self.first_movement_id, self.second_movement_id)))


@dataclass(frozen=True)
class SignalPhase:
    state: str
    duration: float
    min_duration: float | None = None
    max_duration: float | None = None
    next_phases: tuple[int, ...] = ()
    name: str = ""


@dataclass(frozen=True)
class SignalPlan:
    program_id: str
    program_type: str
    phases: tuple[SignalPhase, ...]


@dataclass(frozen=True)
class TlsSafetyDefinition:
    tls_id: str
    signal_count: int
    movements: tuple[Movement, ...]
    conflicts: tuple[MovementConflict, ...]
    plans: tuple[SignalPlan, ...]
    active_program_id: str
    current_phase: int


@dataclass(frozen=True)
class TlsSafetyCatalog:
    intersections: tuple[TlsSafetyDefinition, ...]
    source: str


@dataclass(frozen=True)
class ActivePlanExpectation:
    program_id: str
    phase_states: tuple[str, ...]


class TlsSafetyDataSource(Protocol):
    """Boundary implemented by SUMO, controller or backend adapters."""

    def load_catalog(self, tls_ids: tuple[str, ...]) -> TlsSafetyCatalog: ...


@dataclass(frozen=True)
class ValidationIssue:
    severity: str
    code: str
    message: str
    tls_id: str
    program_id: str = ""
    phase_index: int | None = None
    movement_ids: tuple[str, ...] = ()

    def as_payload(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class TlsSafetyReport:
    source: str
    tls_count: int
    plan_count: int
    movement_count: int
    conflict_count: int
    issues: tuple[ValidationIssue, ...]

    @property
    def valid(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)

    @property
    def error_count(self) -> int:
        return sum(issue.severity == "error" for issue in self.issues)

    @property
    def warning_count(self) -> int:
        return sum(issue.severity == "warning" for issue in self.issues)

    def as_payload(self) -> dict[str, object]:
        return {
            "source": self.source,
            "valid": self.valid,
            "tls_count": self.tls_count,
            "plan_count": self.plan_count,
            "movement_count": self.movement_count,
            "conflict_count": self.conflict_count,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "issues": [issue.as_payload() for issue in self.issues],
        }

    def raise_for_errors(self) -> None:
        errors = [issue for issue in self.issues if issue.severity == "error"]
        if not errors:
            return
        first = errors[0]
        raise RuntimeError(
            f"TLS safety startup validation failed with {len(errors)} error(s); "
            f"first: {first.tls_id}/{first.program_id or '-'} "
            f"[{first.code}] {first.message}"
        )


def validate_tls_catalog(
    catalog: TlsSafetyCatalog,
    expected_active_plans: dict[str, ActivePlanExpectation] | None = None,
) -> TlsSafetyReport:
    expected_active_plans = expected_active_plans or {}
    issues: list[ValidationIssue] = []
    tls_ids: set[str] = set()
    for definition in catalog.intersections:
        if definition.tls_id in tls_ids:
            issues.append(
                _issue(definition.tls_id, "duplicate_tls", "Duplicate TLS ID")
            )
        tls_ids.add(definition.tls_id)
        issues.extend(
            _validate_definition(
                definition,
                expected_active_plans.get(definition.tls_id),
            )
        )
    missing_expected = sorted(set(expected_active_plans) - tls_ids)
    for tls_id in missing_expected:
        issues.append(
            _issue(
                tls_id,
                "expected_tls_missing",
                "Expected TLS is missing from the safety catalog",
            )
        )
    return TlsSafetyReport(
        source=catalog.source,
        tls_count=len(catalog.intersections),
        plan_count=sum(len(item.plans) for item in catalog.intersections),
        movement_count=sum(len(item.movements) for item in catalog.intersections),
        conflict_count=sum(len(item.conflicts) for item in catalog.intersections),
        issues=tuple(
            sorted(
                issues,
                key=lambda item: (
                    item.tls_id,
                    item.program_id,
                    item.phase_index if item.phase_index is not None else -1,
                    item.code,
                ),
            )
        ),
    )


def catalog_payload(catalog: TlsSafetyCatalog) -> dict[str, object]:
    return {
        "source": catalog.source,
        "intersections": [
            {
                "tls_id": item.tls_id,
                "signal_count": item.signal_count,
                "active_program_id": item.active_program_id,
                "current_phase": item.current_phase,
                "movements": [asdict(movement) for movement in item.movements],
                "conflicts": [asdict(conflict) for conflict in item.conflicts],
                "plans": [
                    {
                        "program_id": plan.program_id,
                        "program_type": plan.program_type,
                        "phases": [asdict(phase) for phase in plan.phases],
                    }
                    for plan in item.plans
                ],
            }
            for item in catalog.intersections
        ],
    }


def _validate_definition(
    definition: TlsSafetyDefinition,
    expected_active_plan: ActivePlanExpectation | None = None,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    tls_id = definition.tls_id
    movement_by_id = {item.movement_id: item for item in definition.movements}
    if len(movement_by_id) != len(definition.movements):
        issues.append(_issue(tls_id, "duplicate_movement", "Duplicate movement ID"))
    if definition.signal_count <= 0:
        issues.append(
            _issue(tls_id, "invalid_signal_count", "Signal count must be positive")
        )
    for movement in definition.movements:
        if not 0 <= movement.signal_index < definition.signal_count:
            issues.append(
                _issue(
                    tls_id,
                    "movement_signal_out_of_range",
                    f"Movement {movement.movement_id} uses signal index "
                    f"{movement.signal_index}, expected 0..{definition.signal_count - 1}",
                    movement_ids=(movement.movement_id,),
                )
            )

    conflict_pairs: set[tuple[str, str]] = set()
    valid_conflicts: list[MovementConflict] = []
    for conflict in definition.conflicts:
        pair = conflict.pair
        if pair[0] == pair[1]:
            issues.append(
                _issue(
                    tls_id,
                    "self_conflict",
                    "A movement cannot conflict with itself",
                    movement_ids=pair,
                )
            )
            continue
        missing = tuple(item for item in pair if item not in movement_by_id)
        if missing:
            issues.append(
                _issue(
                    tls_id,
                    "unknown_conflict_movement",
                    f"Conflict references unknown movement(s): {', '.join(missing)}",
                    movement_ids=pair,
                )
            )
            continue
        if pair in conflict_pairs:
            issues.append(
                _issue(
                    tls_id,
                    "duplicate_conflict",
                    "Duplicate conflict pair",
                    severity="warning",
                    movement_ids=pair,
                )
            )
            continue
        conflict_pairs.add(pair)
        valid_conflicts.append(conflict)

    program_ids = [plan.program_id for plan in definition.plans]
    if len(set(program_ids)) != len(program_ids):
        issues.append(_issue(tls_id, "duplicate_program", "Duplicate program ID"))
    active_matches = sum(
        plan.program_id == definition.active_program_id for plan in definition.plans
    )
    if active_matches != 1:
        issues.append(
            _issue(
                tls_id,
                "active_program_missing",
                f"Active program {definition.active_program_id!r} matched "
                f"{active_matches} plans",
            )
        )

    for plan in definition.plans:
        issues.extend(
            _validate_plan(
                definition,
                plan,
                movement_by_id,
                tuple(valid_conflicts),
            )
        )
    active_plan = next(
        (
            plan
            for plan in definition.plans
            if plan.program_id == definition.active_program_id
        ),
        None,
    )
    if active_plan is not None and not 0 <= definition.current_phase < len(
        active_plan.phases
    ):
        issues.append(
            _issue(
                tls_id,
                "current_phase_out_of_range",
                f"Current phase {definition.current_phase} is outside active plan",
                program_id=active_plan.program_id,
            )
        )
    if expected_active_plan is not None:
        if definition.active_program_id != expected_active_plan.program_id:
            issues.append(
                _issue(
                    tls_id,
                    "active_program_mismatch",
                    f"Runtime active program {definition.active_program_id!r} "
                    f"does not match controller model "
                    f"{expected_active_plan.program_id!r}",
                    program_id=definition.active_program_id,
                )
            )
        elif active_plan is not None:
            actual_states = tuple(phase.state for phase in active_plan.phases)
            if actual_states != expected_active_plan.phase_states:
                issues.append(
                    _issue(
                        tls_id,
                        "active_phase_states_mismatch",
                        "Runtime active phase states differ from controller model",
                        program_id=active_plan.program_id,
                    )
                )
    return issues


def _validate_plan(
    definition: TlsSafetyDefinition,
    plan: SignalPlan,
    movement_by_id: dict[str, Movement],
    conflicts: tuple[MovementConflict, ...],
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    tls_id = definition.tls_id
    if not plan.phases:
        return [
            _issue(
                tls_id,
                "empty_program",
                "Program has no phases",
                program_id=plan.program_id,
            )
        ]

    for phase_index, phase in enumerate(plan.phases):
        context = {
            "program_id": plan.program_id,
            "phase_index": phase_index,
        }
        if len(phase.state) != definition.signal_count:
            issues.append(
                _issue(
                    tls_id,
                    "state_length_mismatch",
                    f"State has {len(phase.state)} signals, expected "
                    f"{definition.signal_count}",
                    **context,
                )
            )
        else:
            invalid_states = sorted(set(phase.state) - VALID_SIGNAL_STATES)
            if invalid_states:
                issues.append(
                    _issue(
                        tls_id,
                        "invalid_signal_state",
                        f"Unsupported signal state(s): {''.join(invalid_states)}",
                        **context,
                    )
                )
        if not isfinite(float(phase.duration)) or phase.duration <= 0:
            issues.append(
                _issue(
                    tls_id,
                    "invalid_duration",
                    "Phase duration must be finite and positive",
                    **context,
                )
            )
        minimum = _valid_bound(phase.min_duration)
        maximum = _valid_bound(phase.max_duration)
        if phase.min_duration is not None and minimum is None:
            issues.append(
                _issue(
                    tls_id,
                    "invalid_min_duration",
                    "minDur must be finite and non-negative",
                    **context,
                )
            )
        if phase.max_duration is not None and maximum is None:
            issues.append(
                _issue(
                    tls_id,
                    "invalid_max_duration",
                    "maxDur must be finite and non-negative",
                    **context,
                )
            )
        if minimum is not None and maximum is not None and minimum > maximum:
            issues.append(
                _issue(
                    tls_id,
                    "invalid_duration_bounds",
                    f"minDur {minimum} exceeds maxDur {maximum}",
                    **context,
                )
            )
        for target in _phase_targets(plan, phase_index):
            if not 0 <= target < len(plan.phases):
                issues.append(
                    _issue(
                        tls_id,
                        "next_phase_out_of_range",
                        f"Transition targets missing phase {target}",
                        **context,
                    )
                )

        if len(phase.state) == definition.signal_count:
            issues.extend(
                _validate_protected_conflicts(
                    tls_id,
                    plan.program_id,
                    phase_index,
                    phase.state,
                    movement_by_id,
                    conflicts,
                )
            )

    issues.extend(
        _validate_transitions(definition, plan, movement_by_id, conflicts)
    )
    reachable = _reachable_phases(plan)
    if len(reachable) != len(plan.phases):
        unreachable = sorted(set(range(len(plan.phases))) - reachable)
        issues.append(
            _issue(
                tls_id,
                "unreachable_phases",
                f"Unreachable phases: {unreachable}",
                severity="warning",
                program_id=plan.program_id,
            )
        )
    if not any(any(signal in "Gg" for signal in phase.state) for phase in plan.phases):
        issues.append(
            _issue(
                tls_id,
                "program_without_green",
                "Program contains no green signal",
                program_id=plan.program_id,
            )
        )
    return issues


def _validate_protected_conflicts(
    tls_id: str,
    program_id: str,
    phase_index: int,
    state: str,
    movement_by_id: dict[str, Movement],
    conflicts: tuple[MovementConflict, ...],
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for conflict in conflicts:
        first_id, second_id = conflict.pair
        first = movement_by_id[first_id]
        second = movement_by_id[second_id]
        if state[first.signal_index] == "G" and state[second.signal_index] == "G":
            issues.append(
                _issue(
                    tls_id,
                    "conflicting_protected_greens",
                    "Conflicting movements simultaneously receive protected green",
                    program_id=program_id,
                    phase_index=phase_index,
                    movement_ids=(first_id, second_id),
                )
            )
    return issues


def _validate_transitions(
    definition: TlsSafetyDefinition,
    plan: SignalPlan,
    movement_by_id: dict[str, Movement],
    conflicts: tuple[MovementConflict, ...],
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for phase_index, phase in enumerate(plan.phases):
        if len(phase.state) != definition.signal_count:
            continue
        for target_index in _phase_targets(plan, phase_index):
            if not 0 <= target_index < len(plan.phases):
                continue
            target = plan.phases[target_index]
            if len(target.state) != definition.signal_count:
                continue
            for movement in definition.movements:
                before = phase.state[movement.signal_index]
                after = target.state[movement.signal_index]
                if before == "G" and after in "rR":
                    issues.append(
                        _issue(
                            definition.tls_id,
                            "protected_green_without_yellow",
                            f"Protected green changes directly to red in transition "
                            f"{phase_index}->{target_index}",
                            program_id=plan.program_id,
                            phase_index=phase_index,
                            movement_ids=(movement.movement_id,),
                        )
                    )
            for conflict in conflicts:
                first_id, second_id = conflict.pair
                first = movement_by_id[first_id]
                second = movement_by_id[second_id]
                if (
                    target.state[first.signal_index] in "yY"
                    and target.state[second.signal_index] in "Gg"
                    and phase.state[second.signal_index] not in "Gg"
                ) or (
                    target.state[second.signal_index] in "yY"
                    and target.state[first.signal_index] in "Gg"
                    and phase.state[first.signal_index] not in "Gg"
                ):
                    issues.append(
                        _issue(
                            definition.tls_id,
                            "green_during_conflicting_clearance",
                            f"A movement receives green while its foe is yellow "
                            f"in transition {phase_index}->{target_index}",
                            program_id=plan.program_id,
                            phase_index=phase_index,
                            movement_ids=(first_id, second_id),
                        )
                    )
    return issues


def _phase_targets(plan: SignalPlan, phase_index: int) -> tuple[int, ...]:
    explicit = plan.phases[phase_index].next_phases
    return explicit or ((phase_index + 1) % len(plan.phases),)


def _reachable_phases(plan: SignalPlan) -> set[int]:
    if not plan.phases:
        return set()
    visited: set[int] = set()
    pending = [0]
    while pending:
        current = pending.pop()
        if current in visited or not 0 <= current < len(plan.phases):
            continue
        visited.add(current)
        pending.extend(_phase_targets(plan, current))
    return visited


def _valid_bound(value: float | None) -> float | None:
    if value is None:
        return None
    converted = float(value)
    return converted if isfinite(converted) and converted >= 0 else None


def _issue(
    tls_id: str,
    code: str,
    message: str,
    *,
    severity: str = "error",
    program_id: str = "",
    phase_index: int | None = None,
    movement_ids: tuple[str, ...] = (),
) -> ValidationIssue:
    return ValidationIssue(
        severity=severity,
        code=code,
        message=message,
        tls_id=tls_id,
        program_id=program_id,
        phase_index=phase_index,
        movement_ids=movement_ids,
    )
