from __future__ import annotations

import logging
from enum import Enum, auto
from typing import Any

from .area_model import AreaModel

logger = logging.getLogger(__name__)

class CorridorState(Enum):
    NORMAL = auto()
    PREPARE = auto()
    GREEN_WINDOW = auto()
    CLEARANCE = auto()
    RECOVERY = auto()

class CorridorManager:
    """Manages the green corridor state machine for an emergency vehicle."""

    def __init__(
        self,
        vehicle_id: str,
        prepare_distance: float = 800.0,
        green_window_distance: float = 300.0,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.vehicle_id = vehicle_id
        self.state = CorridorState.NORMAL
        self.prepare_distance = prepare_distance
        self.green_window_distance = green_window_distance
        self.timeout_seconds = timeout_seconds
        
        self.active_tls: str | None = None
        self.active_link_index: int | None = None
        self._last_seen_time = -1.0
        self._clearance_start_time = -1.0

    def step(self, simulation_time: float, vehicle_in_network: bool, next_tls_info: tuple[str, int, float] | None = None) -> None:
        """Update the state machine.
        
        Args:
            simulation_time: Current simulation time in seconds.
            vehicle_in_network: True if the vehicle is currently in the simulation.
            next_tls_info: Tuple of (tls_id, link_index, distance) if approaching a TLS, else None.
        """
        if not vehicle_in_network:
            if self.state != CorridorState.NORMAL:
                time_since_seen = simulation_time - self._last_seen_time
                if time_since_seen > self.timeout_seconds:
                    # Timeout or vehicle finished route -> Fallback to NORMAL via RECOVERY
                    if self.state in {CorridorState.PREPARE, CorridorState.GREEN_WINDOW, CorridorState.CLEARANCE}:
                        self.state = CorridorState.RECOVERY
                    else:
                        self.state = CorridorState.NORMAL
                    self.active_tls = None
                    self.active_link_index = None
            return

        self._last_seen_time = simulation_time

        if next_tls_info is None:
            # Vehicle is in network but has no upcoming TLS
            if self.state == CorridorState.GREEN_WINDOW:
                self.state = CorridorState.CLEARANCE
                self._clearance_start_time = simulation_time
            elif self.state == CorridorState.CLEARANCE:
                if simulation_time - self._clearance_start_time > 5.0: # 5 seconds of clearance
                    self.state = CorridorState.RECOVERY
            elif self.state == CorridorState.RECOVERY:
                self.state = CorridorState.NORMAL
            return

        tls_id, link_index, distance = next_tls_info
        
        # If we reached a new TLS, we might want to clear the previous one, but for simplicity:
        self.active_tls = tls_id
        self.active_link_index = link_index

        if distance <= self.green_window_distance:
            self.state = CorridorState.GREEN_WINDOW
        elif distance <= self.prepare_distance:
            if self.state in {CorridorState.NORMAL, CorridorState.RECOVERY}:
                self.state = CorridorState.PREPARE
        else:
            if self.state in {CorridorState.PREPARE, CorridorState.RECOVERY}:
                self.state = CorridorState.NORMAL

    def get_priority_overrides(self) -> dict[str, int]:
        """Return the overrides for the controller based on the current state."""
        if self.state in {CorridorState.PREPARE, CorridorState.GREEN_WINDOW} and self.active_tls is not None and self.active_link_index is not None:
            return {self.active_tls: self.active_link_index}
        return {}
