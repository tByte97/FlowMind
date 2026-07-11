from __future__ import annotations

from dataclasses import dataclass
from math import isclose, isfinite

from sumolib.net import Phase
from traci import constants as tc
from traci._trafficlight import Logic


STATIC_FIXED_PROGRAM_ID = "flowmind_static_fixed"


@dataclass(frozen=True)
class StaticProgramActivation:
    """Evidence that a controlled TLS was moved to the fixed baseline."""

    tls_id: str
    source_program_id: str
    source_program_type: int
    program_id: str
    program_type: int
    current_phase: int
    phase_durations: tuple[float, ...]


@dataclass(frozen=True)
class _PreparedStaticProgram:
    activation: StaticProgramActivation
    logic: Logic


def activate_static_fixed_programs(
    traci_connection: object,
    tls_ids: tuple[str, ...],
    *,
    program_id: str = STATIC_FIXED_PROGRAM_ID,
) -> tuple[StaticProgramActivation, ...]:
    """Replace active TLS programs with deterministic static-time copies.

    The source program supplies the already validated phase order and signal
    states. Actuated phase durations are clamped to their SUMO min/max safety
    envelope, then frozen by setting ``minDur == duration == maxDur``.
    """

    if not program_id.strip():
        raise ValueError("Static fixed-time program ID cannot be empty")
    if len(set(tls_ids)) != len(tls_ids):
        raise ValueError("Traffic-light IDs for the static baseline must be unique")

    trafficlight = traci_connection.trafficlight
    prepared = tuple(
        _prepare_static_program(trafficlight, tls_id, program_id)
        for tls_id in tls_ids
    )

    for item in prepared:
        tls_id = item.activation.tls_id
        trafficlight.setProgramLogic(tls_id, item.logic)
        trafficlight.setProgram(tls_id, program_id)
        _verify_static_program(trafficlight, item)

    return tuple(item.activation for item in prepared)


def _prepare_static_program(
    trafficlight: object,
    tls_id: str,
    program_id: str,
) -> _PreparedStaticProgram:
    active_program_id = str(trafficlight.getProgram(tls_id))
    source_matches = tuple(
        logic
        for logic in trafficlight.getAllProgramLogics(tls_id)
        if str(logic.programID) == active_program_id
    )
    if len(source_matches) != 1:
        raise RuntimeError(
            f"TLS {tls_id}: active SUMO program {active_program_id!r} "
            f"matched {len(source_matches)} program definitions"
        )

    source = source_matches[0]
    source_phases = tuple(source.getPhases())
    if not source_phases:
        raise RuntimeError(f"TLS {tls_id}: active SUMO program has no phases")

    current_phase = int(trafficlight.getPhase(tls_id))
    if not 0 <= current_phase < len(source_phases):
        raise RuntimeError(
            f"TLS {tls_id}: active phase {current_phase} is outside "
            f"0..{len(source_phases) - 1}"
        )

    phases = tuple(
        _freeze_phase(tls_id, phase_index, phase)
        for phase_index, phase in enumerate(source_phases)
    )
    logic = Logic(
        program_id,
        tc.TRAFFICLIGHT_TYPE_STATIC,
        current_phase,
        phases,
        {
            "flowmind.baseline": "static_fixed",
            "flowmind.source_program": active_program_id,
            "flowmind.source_type": str(int(source.type)),
        },
    )
    activation = StaticProgramActivation(
        tls_id=tls_id,
        source_program_id=active_program_id,
        source_program_type=int(source.type),
        program_id=program_id,
        program_type=tc.TRAFFICLIGHT_TYPE_STATIC,
        current_phase=current_phase,
        phase_durations=tuple(float(phase.duration) for phase in phases),
    )
    return _PreparedStaticProgram(activation, logic)


def _freeze_phase(tls_id: str, phase_index: int, phase: object) -> Phase:
    duration = _positive_duration(tls_id, phase_index, phase.duration)
    minimum = _duration_bound(tls_id, phase_index, "minDur", phase.minDur)
    maximum = _duration_bound(tls_id, phase_index, "maxDur", phase.maxDur)
    if minimum is not None and maximum is not None and minimum > maximum:
        raise RuntimeError(
            f"TLS {tls_id} phase {phase_index}: minDur {minimum} exceeds "
            f"maxDur {maximum}"
        )
    if minimum is not None:
        duration = max(duration, minimum)
    if maximum is not None:
        duration = min(duration, maximum)
    if duration <= 0:
        raise RuntimeError(
            f"TLS {tls_id} phase {phase_index}: safety bounds produce a "
            f"non-positive duration {duration}"
        )

    state = str(phase.state)
    if not state:
        raise RuntimeError(f"TLS {tls_id} phase {phase_index}: signal state is empty")
    return Phase(
        duration,
        state,
        minDur=duration,
        maxDur=duration,
        next=(),
        name=str(getattr(phase, "name", "") or ""),
    )


def _positive_duration(tls_id: str, phase_index: int, value: object) -> float:
    try:
        duration = float(value)
    except (TypeError, ValueError) as error:
        raise RuntimeError(
            f"TLS {tls_id} phase {phase_index}: invalid duration {value!r}"
        ) from error
    if not isfinite(duration) or duration <= 0:
        raise RuntimeError(
            f"TLS {tls_id} phase {phase_index}: duration must be finite and positive"
        )
    return duration


def _duration_bound(
    tls_id: str,
    phase_index: int,
    field: str,
    value: object,
) -> float | None:
    if value is None:
        return None
    try:
        bound = float(value)
    except (TypeError, ValueError) as error:
        raise RuntimeError(
            f"TLS {tls_id} phase {phase_index}: invalid {field} {value!r}"
        ) from error
    if not isfinite(bound):
        raise RuntimeError(
            f"TLS {tls_id} phase {phase_index}: {field} must be finite"
        )
    return bound if bound >= 0 else None


def _verify_static_program(
    trafficlight: object,
    expected: _PreparedStaticProgram,
) -> None:
    activation = expected.activation
    active_program_id = str(trafficlight.getProgram(activation.tls_id))
    if active_program_id != activation.program_id:
        raise RuntimeError(
            f"TLS {activation.tls_id}: requested static program "
            f"{activation.program_id!r}, but SUMO activated {active_program_id!r}"
        )

    installed = tuple(
        logic
        for logic in trafficlight.getAllProgramLogics(activation.tls_id)
        if str(logic.programID) == activation.program_id
    )
    if len(installed) != 1:
        raise RuntimeError(
            f"TLS {activation.tls_id}: installed static program matched "
            f"{len(installed)} definitions"
        )
    logic = installed[0]
    if int(logic.type) != tc.TRAFFICLIGHT_TYPE_STATIC:
        raise RuntimeError(
            f"TLS {activation.tls_id}: installed program type is {logic.type}, "
            "not STATIC"
        )

    actual_phases = tuple(logic.getPhases())
    expected_phases = tuple(expected.logic.getPhases())
    if len(actual_phases) != len(expected_phases) or any(
        actual.state != wanted.state
        or not isclose(float(actual.duration), float(wanted.duration))
        for actual, wanted in zip(actual_phases, expected_phases)
    ):
        raise RuntimeError(
            f"TLS {activation.tls_id}: SUMO changed the fixed phase sequence"
        )
