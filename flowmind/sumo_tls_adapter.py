from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from pathlib import Path

import sumolib

from .tls_programs import PROGRAM_TYPE_NAMES
from .tls_safety import (
    Movement,
    MovementConflict,
    SignalPhase,
    SignalPlan,
    TlsSafetyCatalog,
    TlsSafetyDefinition,
)


@dataclass(frozen=True)
class _SumoMovement:
    movement: Movement
    node: object
    junction_index: int


class SumoTlsSafetyAdapter:
    """Translate SUMO topology/runtime data into the FlowMind safety model.

    No validation policy lives here. A production controller, GIS service or
    backend API can replace this adapter by returning the same neutral catalog.
    """

    def __init__(self, net_path: str | Path, traci_connection: object) -> None:
        self._net_path = Path(net_path)
        self._traci = traci_connection

    def load_catalog(self, tls_ids: tuple[str, ...]) -> TlsSafetyCatalog:
        network = sumolib.net.readNet(
            str(self._net_path),
            withPrograms=True,
            withConnections=True,
            withFoes=True,
        )
        topology_by_id = {
            str(tls.getID()): tls for tls in network.getTrafficLights()
        }
        definitions: list[TlsSafetyDefinition] = []
        for tls_id in tls_ids:
            tls = topology_by_id.get(tls_id)
            if tls is None:
                raise RuntimeError(
                    f"TLS {tls_id}: missing from safety topology {self._net_path}"
                )
            movements = self._load_movements(tls_id, tls)
            definitions.append(
                TlsSafetyDefinition(
                    tls_id=tls_id,
                    signal_count=max(
                        (item.movement.signal_index for item in movements),
                        default=-1,
                    )
                    + 1,
                    movements=tuple(item.movement for item in movements),
                    conflicts=self._load_conflicts(tls_id, movements),
                    plans=self._load_plans(tls_id),
                    active_program_id=str(
                        self._traci.trafficlight.getProgram(tls_id)
                    ),
                    current_phase=int(
                        self._traci.trafficlight.getPhase(tls_id)
                    ),
                )
            )
        return TlsSafetyCatalog(
            intersections=tuple(definitions),
            source=f"sumo:{self._net_path}",
        )

    @staticmethod
    def _load_movements(tls_id: str, tls: object) -> tuple[_SumoMovement, ...]:
        movements: list[_SumoMovement] = []
        occurrences: dict[tuple[int, str, str], int] = {}
        connections = tuple(tls.getConnections())
        for incoming, outgoing, signal_index_value in connections:
            signal_index = int(signal_index_value)
            incoming_lane = str(incoming.getID())
            outgoing_lane = str(outgoing.getID())
            key = (signal_index, incoming_lane, outgoing_lane)
            occurrence = occurrences.get(key, 0)
            occurrences[key] = occurrence + 1
            connection = _find_sumo_connection(
                tls_id,
                incoming,
                outgoing,
                signal_index,
            )
            movement_id = (
                f"{tls_id}:{signal_index}:{incoming_lane}>{outgoing_lane}"
                f":{occurrence}"
            )
            movements.append(
                _SumoMovement(
                    movement=Movement(
                        movement_id=movement_id,
                        signal_index=signal_index,
                        incoming_lane=incoming_lane,
                        outgoing_lane=outgoing_lane,
                        direction=str(connection.getDirection() or ""),
                    ),
                    node=incoming.getEdge().getToNode(),
                    junction_index=int(connection.getJunctionIndex()),
                )
            )
        return tuple(movements)

    @staticmethod
    def _load_conflicts(
        tls_id: str,
        movements: tuple[_SumoMovement, ...],
    ) -> tuple[MovementConflict, ...]:
        conflicts: list[MovementConflict] = []
        for left_index, left in enumerate(movements):
            for right in movements[left_index + 1 :]:
                if left.node.getID() != right.node.getID():
                    continue
                try:
                    are_foes = bool(
                        left.node.areFoes(
                            left.junction_index,
                            right.junction_index,
                        )
                        or left.node.areFoes(
                            right.junction_index,
                            left.junction_index,
                        )
                    )
                except (IndexError, KeyError) as error:
                    raise RuntimeError(
                        f"TLS {tls_id}: incomplete right-of-way data for "
                        f"junction indexes {left.junction_index}/"
                        f"{right.junction_index}"
                    ) from error
                if are_foes:
                    conflicts.append(
                        MovementConflict(
                            left.movement.movement_id,
                            right.movement.movement_id,
                            reason="sumo_right_of_way_foe",
                        )
                    )
        return tuple(conflicts)

    def _load_plans(self, tls_id: str) -> tuple[SignalPlan, ...]:
        plans: list[SignalPlan] = []
        for logic in self._traci.trafficlight.getAllProgramLogics(tls_id):
            program_type = int(logic.type)
            phases = tuple(
                SignalPhase(
                    state=str(phase.state),
                    duration=float(phase.duration),
                    min_duration=_optional_bound(phase.minDur),
                    max_duration=_optional_bound(phase.maxDur),
                    next_phases=tuple(int(item) for item in phase.next),
                    name=str(getattr(phase, "name", "") or ""),
                )
                for phase in logic.getPhases()
            )
            plans.append(
                SignalPlan(
                    program_id=str(logic.programID),
                    program_type=PROGRAM_TYPE_NAMES.get(
                        program_type,
                        f"unknown_{program_type}",
                    ),
                    phases=phases,
                )
            )
        return tuple(plans)


def _find_sumo_connection(
    tls_id: str,
    incoming: object,
    outgoing: object,
    signal_index: int,
) -> object:
    matches = tuple(
        connection
        for connection in incoming.getOutgoing()
        if connection.getToLane().getID() == outgoing.getID()
        and int(connection.getTLLinkIndex()) == signal_index
        and str(connection.getTLSID()) == tls_id
    )
    if len(matches) != 1:
        raise RuntimeError(
            f"TLS {tls_id}: movement {incoming.getID()}->{outgoing.getID()} "
            f"at signal {signal_index} matched {len(matches)} SUMO connections"
        )
    return matches[0]


def _optional_bound(value: object) -> float | None:
    if value is None:
        return None
    try:
        converted = float(value)
    except (TypeError, ValueError):
        return None
    return converted if isfinite(converted) and converted >= 0 else None
