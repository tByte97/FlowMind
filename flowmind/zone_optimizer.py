from __future__ import annotations

from dataclasses import dataclass

from .area_model import AreaModel, Intersection
from .config import ControlConfig
from .signal_policy import PhaseScore
from .zone_graph import AreaDecisionSnapshot, AreaGraph, RoadSegment


@dataclass(frozen=True)
class ZonePhaseChoice:
    tls_id: str
    phase_index: int
    joint_objective: float
    local_phase_index: int
    coordination_gain: float = 0.0
    local_score_loss: float = 0.0
    is_override: bool = False


def optimize_zone_phases(
    area: AreaModel,
    graph: AreaGraph,
    snapshot: AreaDecisionSnapshot,
    scores_by_tls: dict[str, tuple[PhaseScore, ...]],
    config: ControlConfig,
    *,
    current_phase_by_tls: dict[str, int] | None = None,
) -> dict[str, ZonePhaseChoice]:
    """Coordinate phase targets over the whole zone.

    This layer depends only on FlowMind domain objects. SUMO remains an input
    and actuation adapter, so a production sensor/backend can provide the same
    area snapshot without changing the optimizer.
    """

    intersections = {
        intersection.tls_id: intersection
        for intersection in area.intersections
    }
    candidates = {
        tls_id: tuple(item.phase_index for item in scores)
        for tls_id, scores in scores_by_tls.items()
        if scores
    }
    score_lookup = {
        (tls_id, item.phase_index): float(item.score)
        for tls_id, scores in scores_by_tls.items()
        for item in scores
    }
    local_assignment = {
        tls_id: max(
            phases,
            key=lambda phase: (score_lookup[(tls_id, phase)], -phase),
        )
        for tls_id, phases in candidates.items()
    }
    if current_phase_by_tls is not None:
        # The controller deliberately permits zone coordination to hold an
        # already-green phase, but never to request an earlier switch. Keep
        # the optimizer's feasible set identical to that actuation contract;
        # otherwise an ignored non-current phase can distort the joint
        # assignment or consume the per-step override budget.
        candidates = {
            tls_id: tuple(
                phase
                for phase in phases
                if phase
                in {
                    local_assignment[tls_id],
                    current_phase_by_tls.get(tls_id),
                }
            )
            for tls_id, phases in candidates.items()
        }
    assignment = dict(local_assignment)
    if not assignment:
        return {}

    fixed_assignment = {
        tls_id: int(phase_index)
        for tls_id, phase_index in (current_phase_by_tls or {}).items()
        if (
            tls_id not in assignment
            and (intersection := intersections.get(tls_id)) is not None
            and 0 <= int(phase_index) < len(intersection.phases)
        )
    }
    for tls_id, phase_index in fixed_assignment.items():
        score_lookup[(tls_id, phase_index)] = 0.0

    def objective(candidate_assignment: dict[str, int]) -> float:
        full_assignment = dict(fixed_assignment)
        full_assignment.update(candidate_assignment)
        return _joint_objective(
            full_assignment,
            intersections,
            graph,
            snapshot,
            score_lookup,
            config,
        )

    # Deterministic coordinate descent captures pairwise corridor coupling
    # without an exponential product of phase combinations for 20+ TLS.
    for _iteration in range(4):
        changed = False
        for tls_id in sorted(assignment):
            best_phase = assignment[tls_id]
            best_value = float("-inf")
            for phase_index in candidates[tls_id]:
                trial = dict(assignment)
                trial[tls_id] = phase_index
                value = objective(trial)
                if value > best_value + 1e-9 or (
                    abs(value - best_value) <= 1e-9
                    and phase_index < best_phase
                ):
                    best_phase = phase_index
                    best_value = value
            if best_phase != assignment[tls_id]:
                assignment[tls_id] = best_phase
                changed = True
        if not changed:
            break

    def validated_overrides() -> tuple[
        dict[str, tuple[float, float]],
        tuple[str, ...],
    ]:
        diagnostics: dict[str, tuple[float, float]] = {}
        rejected: list[str] = []
        current_objective = objective(assignment)
        for tls_id, phase_index in assignment.items():
            local_phase = local_assignment[tls_id]
            if phase_index == local_phase:
                continue
            local_trial = dict(assignment)
            local_trial[tls_id] = local_phase
            gain = current_objective - objective(local_trial)
            local_loss = max(
                score_lookup[(tls_id, local_phase)]
                - score_lookup[(tls_id, phase_index)],
                0.0,
            )
            if (
                gain + 1e-9 < float(config.zone_override_min_gain)
                or local_loss
                > float(config.zone_override_max_local_loss) + 1e-9
            ):
                rejected.append(tls_id)
                continue
            diagnostics[tls_id] = (gain, local_loss)
        return diagnostics, tuple(rejected)

    # Removing a weak override changes the joint objective seen by every
    # remaining override. Revalidate until the assignment is stable rather
    # than publishing stale coordination gains.
    while True:
        override_diagnostics, rejected_overrides = validated_overrides()
        if not rejected_overrides:
            break
        for tls_id in rejected_overrides:
            assignment[tls_id] = local_assignment[tls_id]

    allowed_overrides = max(int(config.max_zone_overrides_per_step), 0)
    if len(override_diagnostics) > allowed_overrides:
        keep = {
            tls_id
            for tls_id, _values in sorted(
                override_diagnostics.items(),
                key=lambda item: (-item[1][0], item[1][1], item[0]),
            )[:allowed_overrides]
        }
        for tls_id in tuple(override_diagnostics):
            if tls_id in keep:
                continue
            assignment[tls_id] = local_assignment[tls_id]

        # The cap can remove a complementary phase that supplied most of a
        # kept override's joint gain. Recompute against the final capped
        # assignment and conservatively discard any override that no longer
        # clears the configured threshold.
        while True:
            override_diagnostics, rejected_overrides = validated_overrides()
            if not rejected_overrides:
                break
            for tls_id in rejected_overrides:
                assignment[tls_id] = local_assignment[tls_id]

    joint_objective = objective(assignment)
    return {
        tls_id: ZonePhaseChoice(
            tls_id=tls_id,
            phase_index=phase_index,
            joint_objective=joint_objective,
            local_phase_index=local_assignment[tls_id],
            coordination_gain=override_diagnostics.get(tls_id, (0.0, 0.0))[0],
            local_score_loss=override_diagnostics.get(tls_id, (0.0, 0.0))[1],
            is_override=tls_id in override_diagnostics,
        )
        for tls_id, phase_index in assignment.items()
    }


def _joint_objective(
    assignment: dict[str, int],
    intersections: dict[str, Intersection],
    graph: AreaGraph,
    snapshot: AreaDecisionSnapshot,
    score_lookup: dict[tuple[str, int], float],
    config: ControlConfig,
) -> float:
    value = sum(
        score_lookup[(tls_id, phase_index)]
        for tls_id, phase_index in assignment.items()
    )
    for segment in graph.segments:
        upstream_phase = assignment.get(segment.upstream_tls)
        downstream_phase = assignment.get(segment.downstream_tls)
        upstream = intersections.get(segment.upstream_tls)
        downstream = intersections.get(segment.downstream_tls)
        if (
            upstream_phase is None
            or downstream_phase is None
            or upstream is None
            or downstream is None
        ):
            continue
        value += _segment_coordination_value(
            segment,
            upstream,
            upstream_phase,
            downstream,
            downstream_phase,
            snapshot,
            config,
        )
    return value


def _segment_coordination_value(
    segment: RoadSegment,
    upstream: Intersection,
    upstream_phase: int,
    downstream: Intersection,
    downstream_phase: int,
    snapshot: AreaDecisionSnapshot,
    config: ControlConfig,
) -> float:
    releases = _phase_serves_outgoing(
        upstream,
        upstream_phase,
        set(segment.upstream_outgoing_lanes),
    )
    receives = _phase_serves_incoming(
        downstream,
        downstream_phase,
        set(segment.downstream_incoming_lanes),
    )
    state = snapshot.segment_states.get(segment.segment_id)
    if state is None:
        return 0.0
    incoming_platoon = sum(
        snapshot.platoon_arrival_by_incoming_lane.get(lane_id, 0.0)
        for lane_id in segment.downstream_incoming_lanes
    )
    risk = max(min(state.spillback_probability, 1.0), 0.0)
    platoon_strength = max(
        min(
            incoming_platoon
            / max(state.capacity_slots * 0.25, 1.0),
            1.0,
        ),
        0.0,
    )
    safe_receiving = 1.0 - risk
    weight = float(config.zone_coordination_weight)

    if receives:
        # The platoon estimate describes vehicles already travelling on this
        # segment. Keeping its downstream green is useful even when the
        # upstream TLS is not releasing more vehicles in the same decision
        # step; discounting that case made useful green-wave holds too rare.
        return weight * platoon_strength * safe_receiving
    if (
        releases
        and risk >= float(config.spillback_hard_gate_probability)
    ):
        # Do not suppress an upstream discharge merely because vehicles are
        # already travelling along the segment: that platoon belongs to the
        # downstream receiving decision and may be tens of seconds away.
        # A release penalty is reserved for physically severe storage risk;
        # ordinary probabilistic risk remains a soft diagnostic and the
        # movement-mask layer retains the final safety authority.
        return -weight * risk
    return 0.0


def _phase_serves_outgoing(
    intersection: Intersection,
    phase_index: int,
    lane_ids: set[str],
) -> bool:
    return any(
        link.outgoing_lane in lane_ids
        for link in _green_links(intersection, phase_index)
    )


def _phase_serves_incoming(
    intersection: Intersection,
    phase_index: int,
    lane_ids: set[str],
) -> bool:
    return any(
        link.incoming_lane in lane_ids
        for link in _green_links(intersection, phase_index)
    )


def _green_links(
    intersection: Intersection,
    phase_index: int,
) -> tuple[object, ...]:
    if not 0 <= phase_index < len(intersection.phases):
        return ()
    state = intersection.phases[phase_index]
    return tuple(
        link
        for link in intersection.links
        if link.signal_index < len(state) and state[link.signal_index] in "Gg"
    )


__all__ = ["ZonePhaseChoice", "optimize_zone_phases"]
