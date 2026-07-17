from __future__ import annotations

import unittest

from flowmind.corridor_manager import CorridorManager, CorridorState


class CorridorManagerTest(unittest.TestCase):
    def test_initial_state(self) -> None:
        manager = CorridorManager("amb_1")
        self.assertEqual(manager.state, CorridorState.NORMAL)
        self.assertEqual(manager.get_priority_overrides(), {})

    def test_transitions_with_vehicle(self) -> None:
        manager = CorridorManager(
            "amb_1", prepare_distance=800.0, green_window_distance=300.0
        )

        manager.step(0.0, vehicle_in_network=False)
        self.assertEqual(manager.state, CorridorState.NORMAL)

        manager.step(
            10.0, vehicle_in_network=True, next_tls_info=("tls_0", 1, 1000.0)
        )
        self.assertEqual(manager.state, CorridorState.NORMAL)

        manager.step(
            20.0, vehicle_in_network=True, next_tls_info=("tls_0", 1, 700.0)
        )
        self.assertEqual(manager.state, CorridorState.PREPARE)
        self.assertEqual(manager.get_priority_overrides(), {})
        self.assertEqual(manager.get_preparation_overrides(), {"tls_0": 1})

        manager.step(
            30.0, vehicle_in_network=True, next_tls_info=("tls_0", 1, 200.0)
        )
        self.assertEqual(manager.state, CorridorState.GREEN_WINDOW)
        self.assertEqual(manager.get_priority_overrides(), {"tls_0": 1})
        self.assertEqual(
            manager.decision_events[-1].title,
            "Зелений коридор активовано",
        )
        self.assertEqual(manager.decision_events[-1].tls_id, "tls_0")

        manager.step(
            40.0,
            vehicle_in_network=True,
            next_tls_info=None,
            passed_tls_ids=("tls_0",),
        )
        self.assertEqual(manager.state, CorridorState.CLEARANCE)
        self.assertEqual(manager.get_priority_overrides(), {})

        manager.step(46.0, vehicle_in_network=True, next_tls_info=None)
        self.assertEqual(manager.state, CorridorState.RECOVERY)

        manager.confirm_recovery("tls_0", 47.0)
        self.assertEqual(manager.state, CorridorState.NORMAL)
        self.assertEqual(manager.completed_tls, ["tls_0"])
        self.assertEqual(
            manager.decision_events[-1].title,
            "Рух повернувся до нормального режиму",
        )

    def test_timeout_fallback(self) -> None:
        manager = CorridorManager(
            "amb_1", timeout_seconds=10.0, recovery_seconds=5.0
        )

        manager.step(
            10.0, vehicle_in_network=True, next_tls_info=("tls_0", 1, 200.0)
        )
        self.assertEqual(manager.state, CorridorState.GREEN_WINDOW)

        manager.step(15.0, vehicle_in_network=False)
        self.assertEqual(manager.state, CorridorState.GREEN_WINDOW)

        manager.step(26.0, vehicle_in_network=False)
        self.assertEqual(manager.state, CorridorState.RECOVERY)

        manager.step(32.0, vehicle_in_network=False)
        self.assertEqual(manager.state, CorridorState.NORMAL)
        self.assertEqual(manager.completed_tls, [])
        self.assertEqual(manager.unconfirmed_tls, ["tls_0"])

    def test_uses_closest_tls_from_upcoming_list(self) -> None:
        manager = CorridorManager("amb_1")

        manager.step(
            10.0,
            vehicle_in_network=True,
            next_tls_info=[("far", 3, 700.0), ("near", 2, 250.0)],
        )

        self.assertEqual(manager.state, CorridorState.GREEN_WINDOW)
        self.assertEqual(manager.get_priority_overrides(), {"near": 2})
        self.assertEqual(manager.get_preparation_overrides(), {"far": 3})
        self.assertEqual(
            tuple(item[0] for item in manager.prepared_tls),
            ("near", "far"),
        )

    def test_tls_change_does_not_mark_passage_without_confirmation(self) -> None:
        manager = CorridorManager("amb_1")
        manager.step(
            10.0,
            vehicle_in_network=True,
            next_tls_info=[("tls-a", 1, 20.0), ("tls-b", 2, 400.0)],
        )

        manager.step(
            11.0,
            vehicle_in_network=True,
            next_tls_info=[("tls-b", 2, 390.0)],
        )

        self.assertEqual(manager.completed_tls, [])
        self.assertEqual(manager.unconfirmed_tls, ["tls-a"])
        self.assertEqual(manager.active_tls, "tls-b")

    def test_explicit_passage_is_the_only_completion_signal(self) -> None:
        manager = CorridorManager("amb_1")
        manager.step(
            10.0,
            vehicle_in_network=True,
            next_tls_info=("tls-a", 1, 20.0),
        )

        manager.step(
            11.0,
            vehicle_in_network=True,
            next_tls_info=("tls-b", 2, 400.0),
            passed_tls_ids=("tls-a",),
        )

        self.assertEqual(manager.completed_tls, ["tls-a"])
        self.assertNotIn("tls-a", manager.unconfirmed_tls)


if __name__ == "__main__":
    unittest.main()
