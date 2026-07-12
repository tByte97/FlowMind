from __future__ import annotations

import unittest

from flowmind.area_model import AreaModel, ControlledLink, Intersection
from flowmind.traffic_state import TrafficStateReader


class FakeLane:
    def __init__(self) -> None:
        self.fail = False
        self.vehicle_ids = {
            "in_0": ("near_stop", "far_approach"),
            "out_0": ("near_exit", "far_exit"),
        }

    def getLength(self, _lane_id: str) -> float:
        if self.fail:
            raise RuntimeError("camera offline")
        return 200.0

    def getLastStepVehicleIDs(self, lane_id: str) -> tuple[str, ...]:
        return self.vehicle_ids[lane_id]

    def getLastStepVehicleNumber(self, lane_id: str) -> int:
        return len(self.vehicle_ids[lane_id])

    def getLastStepHaltingNumber(self, _lane_id: str) -> int:
        return 0

    def getLastStepOccupancy(self, _lane_id: str) -> float:
        return 0.0

    def getLastStepMeanSpeed(self, _lane_id: str) -> float:
        return 10.0


class FakeVehicle:
    positions = {
        "near_stop": 170.0,
        "far_approach": 20.0,
        "near_exit": 30.0,
        "far_exit": 170.0,
    }
    speeds = {
        "near_stop": 0.0,
        "far_approach": 8.0,
        "near_exit": 6.0,
        "far_exit": 9.0,
    }

    def getLanePosition(self, vehicle_id: str) -> float:
        return self.positions[vehicle_id]

    def getSpeed(self, vehicle_id: str) -> float:
        return self.speeds[vehicle_id]


class FakeTraci:
    def __init__(self) -> None:
        self.lane = FakeLane()
        self.vehicle = FakeVehicle()


class TrafficStateReaderTest(unittest.TestCase):
    def test_reads_only_intersection_sensor_window(self) -> None:
        area = AreaModel(
            (
                Intersection(
                    tls_id="tls",
                    position=(0.0, 0.0),
                    phases=("G", "y"),
                    links=(ControlledLink("in_0", "out_0", 0),),
                ),
            )
        )
        state = TrafficStateReader(
            FakeTraci(),
            area,
            sensor_range_meters=60.0,
        ).read()

        incoming = state.lane("in_0")
        outgoing = state.lane("out_0")

        self.assertEqual(incoming.vehicle_ids, ("near_stop",))
        self.assertEqual(incoming.vehicle_count, 1)
        self.assertEqual(incoming.queue, 1)
        self.assertEqual(outgoing.vehicle_ids, ("near_exit",))
        self.assertEqual(outgoing.vehicle_count, 1)

    def test_reads_full_intermediate_graph_lane(self) -> None:
        area = AreaModel(
            (
                Intersection(
                    tls_id="tls",
                    position=(0.0, 0.0),
                    phases=("G", "y"),
                    links=(ControlledLink("in_0", "out_0", 0),),
                ),
            )
        )
        traci = FakeTraci()
        traci.lane.vehicle_ids["middle_0"] = ("far_exit",)
        state = TrafficStateReader(
            traci,
            area,
            sensor_range_meters=60.0,
            monitored_lane_ids=("middle_0",),
        ).read()

        self.assertEqual(state.lane("middle_0").vehicle_ids, ("far_exit",))
        self.assertEqual(state.lane("middle_0").vehicle_count, 1)

    def test_last_known_good_expires_after_ttl(self) -> None:
        area = AreaModel(
            (
                Intersection(
                    tls_id="tls",
                    position=(0.0, 0.0),
                    phases=("G", "y"),
                    links=(ControlledLink("in_0", "out_0", 0),),
                ),
            )
        )
        traci = FakeTraci()
        reader = TrafficStateReader(
            traci,
            area,
            sensor_range_meters=60.0,
            last_known_good_ttl=5.0,
        )

        fresh = reader.read(1.0)
        traci.lane.fail = True
        stale = reader.read(4.0)
        expired = reader.read(7.0)

        self.assertTrue(fresh.usable)
        self.assertTrue(stale.usable)
        self.assertTrue(stale.lane("in_0").stale)
        self.assertEqual(stale.lane("in_0").sample_time, 1.0)
        self.assertEqual(stale.lane("in_0").error, "camera offline")
        self.assertFalse(expired.usable)
        self.assertIn("in_0", expired.invalid_lane_ids)


if __name__ == "__main__":
    unittest.main()
