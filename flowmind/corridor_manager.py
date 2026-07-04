from __future__ import annotations

import logging
from enum import Enum, auto

logger = logging.getLogger(__name__)


class CorridorState(Enum):
    NORMAL = auto()
    PREPARE = auto()
    GREEN_WINDOW = auto()
    CLEARANCE = auto()
    RECOVERY = auto()


NextTlsInfo = tuple[str, int, float]


class CorridorManager:
    """Manages the green corridor state machine for an emergency vehicle."""

    def __init__(
        self,
        vehicle_id: str,
        prepare_distance: float = 800.0,
        green_window_distance: float = 300.0,
        timeout_seconds: float = 10.0,
        clearance_seconds: float = 5.0,
        recovery_seconds: float = 5.0,
    ) -> None:
        self.vehicle_id = vehicle_id
        self.state = CorridorState.NORMAL
        self.prepare_distance = prepare_distance
        self.green_window_distance = green_window_distance
        self.timeout_seconds = timeout_seconds
        self.clearance_seconds = clearance_seconds
        self.recovery_seconds = recovery_seconds

        self.active_tls: str | None = None
        self.active_link_index: int | None = None
        self._last_seen_time = -1.0
        self._clearance_start_time = -1.0
        self._recovery_start_time = -1.0
        self.completed_tls: list[str] = []

    def step(
        self,
        simulation_time: float,
        vehicle_in_network: bool,
        next_tls_info: NextTlsInfo | list[NextTlsInfo] | None = None,
    ) -> None:
        """Update the state machine.
        
        Args:
            simulation_time: Current simulation time in seconds.
            vehicle_in_network: True if the vehicle is currently in the simulation.
            next_tls_info: Upcoming TLS tuple(s) as (tls_id, link_index, distance).
        """
        if not vehicle_in_network:
            self._handle_missing_vehicle(simulation_time)
            return

        self._last_seen_time = simulation_time
        selected = self._select_next_tls(next_tls_info)

        if selected is None:
            self._advance_without_upcoming_tls(simulation_time)
            return

        tls_id, link_index, distance = selected
        if self.active_tls and tls_id != self.active_tls:
            self._mark_completed(self.active_tls)

        self.active_tls = tls_id
        self.active_link_index = link_index

        if distance <= self.green_window_distance:
            self.state = CorridorState.GREEN_WINDOW
        elif distance <= self.prepare_distance:
            if self.state in {
                CorridorState.NORMAL,
                CorridorState.RECOVERY,
                CorridorState.CLEARANCE,
            }:
                self.state = CorridorState.PREPARE
        else:
            if self.state in {CorridorState.PREPARE, CorridorState.RECOVERY}:
                self.state = CorridorState.NORMAL
                self.active_tls = None
                self.active_link_index = None

    def get_priority_overrides(self) -> dict[str, int]:
        """Return the overrides for the controller based on the current state."""
        if (
            self.state in {CorridorState.PREPARE, CorridorState.GREEN_WINDOW}
            and self.active_tls is not None
            and self.active_link_index is not None
        ):
            return {self.active_tls: self.active_link_index}
        return {}

    def as_summary(self) -> dict[str, object]:
        return {
            "corridor_state": self.state.name,
            "corridor_active_tls": self.active_tls,
            "corridor_completed_tls": tuple(self.completed_tls),
        }

    def _handle_missing_vehicle(self, simulation_time: float) -> None:
        if self.state == CorridorState.NORMAL:
            return
        if self.state == CorridorState.RECOVERY:
            self._advance_recovery(simulation_time)
            return
        time_since_seen = simulation_time - self._last_seen_time
        if time_since_seen <= self.timeout_seconds:
            return
        if self.active_tls:
            self._mark_completed(self.active_tls)
        self.active_tls = None
        self.active_link_index = None
        self._enter_recovery(simulation_time)
        self._advance_recovery(simulation_time)

    def _advance_without_upcoming_tls(self, simulation_time: float) -> None:
        if self.state in {CorridorState.PREPARE, CorridorState.GREEN_WINDOW}:
            if self.active_tls:
                self._mark_completed(self.active_tls)
            self.active_tls = None
            self.active_link_index = None
            self.state = CorridorState.CLEARANCE
            self._clearance_start_time = simulation_time
        elif self.state == CorridorState.CLEARANCE:
            if (
                simulation_time - self._clearance_start_time
                >= self.clearance_seconds
            ):
                self._enter_recovery(simulation_time)
        elif self.state == CorridorState.RECOVERY:
            self._advance_recovery(simulation_time)

    def _enter_recovery(self, simulation_time: float) -> None:
        self.state = CorridorState.RECOVERY
        self._recovery_start_time = simulation_time

    def _advance_recovery(self, simulation_time: float) -> None:
        if simulation_time - self._recovery_start_time >= self.recovery_seconds:
            self.state = CorridorState.NORMAL

    def _mark_completed(self, tls_id: str) -> None:
        if tls_id not in self.completed_tls:
            self.completed_tls.append(tls_id)

    def _select_next_tls(
        self,
        next_tls_info: NextTlsInfo | list[NextTlsInfo] | None,
    ) -> NextTlsInfo | None:
        if next_tls_info is None:
            return None
        if isinstance(next_tls_info, tuple):
            candidates = [next_tls_info]
        else:
            candidates = list(next_tls_info)
        usable = [
            (str(tls_id), int(link_index), float(distance))
            for tls_id, link_index, distance in candidates
            if float(distance) <= self.prepare_distance
        ]
        if not usable:
            return None
        return min(usable, key=lambda item: item[2])
