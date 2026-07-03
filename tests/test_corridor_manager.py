from __future__ import annotations

import pytest
from flowmind.corridor_manager import CorridorManager, CorridorState

def test_initial_state():
    manager = CorridorManager("amb_1")
    assert manager.state == CorridorState.NORMAL
    assert manager.get_priority_overrides() == {}

def test_transitions_with_vehicle():
    manager = CorridorManager("amb_1", prepare_distance=800.0, green_window_distance=300.0)
    
    # Vehicle not in network
    manager.step(0.0, vehicle_in_network=False)
    assert manager.state == CorridorState.NORMAL
    
    # Vehicle far away
    manager.step(10.0, vehicle_in_network=True, next_tls_info=("tls_0", 1, 1000.0))
    assert manager.state == CorridorState.NORMAL
    
    # Vehicle enters prepare distance
    manager.step(20.0, vehicle_in_network=True, next_tls_info=("tls_0", 1, 700.0))
    assert manager.state == CorridorState.PREPARE
    assert manager.get_priority_overrides() == {"tls_0": 1}
    
    # Vehicle enters green window
    manager.step(30.0, vehicle_in_network=True, next_tls_info=("tls_0", 1, 200.0))
    assert manager.state == CorridorState.GREEN_WINDOW
    assert manager.get_priority_overrides() == {"tls_0": 1}
    
    # Vehicle passes TLS, no next TLS
    manager.step(40.0, vehicle_in_network=True, next_tls_info=None)
    assert manager.state == CorridorState.CLEARANCE
    assert manager.get_priority_overrides() == {}
    
    # 5 seconds clearance
    manager.step(46.0, vehicle_in_network=True, next_tls_info=None)
    assert manager.state == CorridorState.RECOVERY
    
    # Recovery returns to normal
    manager.step(50.0, vehicle_in_network=True, next_tls_info=None)
    assert manager.state == CorridorState.NORMAL

def test_timeout_fallback():
    manager = CorridorManager("amb_1", timeout_seconds=10.0)
    
    # Enter GREEN_WINDOW
    manager.step(10.0, vehicle_in_network=True, next_tls_info=("tls_0", 1, 200.0))
    assert manager.state == CorridorState.GREEN_WINDOW
    
    # Vehicle disappears (e.g. error, or arrived)
    manager.step(15.0, vehicle_in_network=False)
    # Less than timeout, state remains
    assert manager.state == CorridorState.GREEN_WINDOW
    
    # Exceed timeout
    manager.step(26.0, vehicle_in_network=False)
    assert manager.state == CorridorState.RECOVERY
    
    # Next step without vehicle -> normal
    manager.step(27.0, vehicle_in_network=False)
    assert manager.state == CorridorState.NORMAL
