from __future__ import annotations

import csv
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from flowmind.area_model import AreaModel, ControlledLink, Intersection
from flowmind.config import CONTROL_MODES, ControlConfig
from flowmind.metrics import MetricSample, MetricsCollector
from flowmind.zone_boundary import ZoneBoundary


class FakeSimulation:
    departed: tuple[str, ...] = ()
    arrived: tuple[str, ...] = ()

    def getDepartedIDList(self) -> tuple[str, ...]:
        return self.departed

    def getArrivedIDList(self) -> tuple[str, ...]:
        return self.arrived


class FakeLane:
    def getLastStepHaltingNumber(self, _lane_id: str) -> int:
        return 0

    def getLastStepOccupancy(self, _lane_id: str) -> float:
        return 0.0

    def getLastStepVehicleIDs(self, _lane_id: str) -> tuple[str, ...]:
        return ()


class FakeVehicle:
    def __init__(self) -> None:
        self.active: set[str] = set()

    def getSpeed(self, _vehicle_id: str) -> float:
        return 0.0

    def getAccumulatedWaitingTime(self, _vehicle_id: str) -> float:
        return 0.0

    def getIDList(self) -> tuple[str, ...]:
        return tuple(self.active)

    def getRoute(self, _vehicle_id: str) -> tuple[str, ...]:
        return ("start", "middle", "hospital")

    def getRouteIndex(self, _vehicle_id: str) -> int:
        return 1

    def getPosition(self, _vehicle_id: str) -> tuple[float, float]:
        return (10.0, 20.0)

    def getRoadID(self, _vehicle_id: str) -> str:
        return "middle"

    def getLaneID(self, _vehicle_id: str) -> str:
        return "middle_0"

    def getLanePosition(self, _vehicle_id: str) -> float:
        return 42.0


class FakeEdge:
    def __init__(self) -> None:
        self.vehicles: dict[str, tuple[str, ...]] = {}

    def getLastStepVehicleIDs(self, edge_id: str) -> tuple[str, ...]:
        return self.vehicles.get(edge_id, ())


class FakeTraci:
    def __init__(self) -> None:
        self.simulation = FakeSimulation()
        self.lane = FakeLane()
        self.vehicle = FakeVehicle()
        self.edge = FakeEdge()


class ActiveFakeLane(FakeLane):
    def getLength(self, _lane_id: str) -> float:
        return 200.0

    def getLastStepVehicleIDs(self, lane_id: str) -> tuple[str, ...]:
        return ("veh1",) if lane_id == "in_0" else ()


class ActiveFakeVehicle(FakeVehicle):
    def getSpeed(self, _vehicle_id: str) -> float:
        return 0.0

    def getLanePosition(self, _vehicle_id: str) -> float:
        return 180.0

    def getPosition(self, _vehicle_id: str) -> tuple[float, float]:
        return (11.0, 22.0)

    def getLaneID(self, _vehicle_id: str) -> str:
        return "in_0"

    def getAngle(self, _vehicle_id: str) -> float:
        return 90.0

    def getColor(self, _vehicle_id: str) -> tuple[int, int, int, int]:
        return (12, 34, 56, 255)


class ActiveFakeTrafficLight:
    def getPhase(self, _tls_id: str) -> int:
        return 0

    def getSpentDuration(self, _tls_id: str) -> float:
        return 7.5

    def getRedYellowGreenState(self, _tls_id: str) -> str:
        return "Gr"


class ActiveFakeTraci(FakeTraci):
    def __init__(self) -> None:
        super().__init__()
        self.lane = ActiveFakeLane()
        self.vehicle = ActiveFakeVehicle()
        self.trafficlight = ActiveFakeTrafficLight()


class MetricsCollectorTest(unittest.TestCase):
    @staticmethod
    def active_area() -> AreaModel:
        return AreaModel(
            (
                Intersection(
                    tls_id="tls_1",
                    position=(10.0, 20.0),
                    phases=("Gr",),
                    links=(ControlledLink("in_0", "out_0", 0),),
                ),
            )
        )

    @staticmethod
    def metric_sample(
        time: float,
        blocked_outgoing_share: float | None,
    ) -> MetricSample:
        return MetricSample(
            time=time,
            active_vehicles=0,
            departed=0,
            arrived=0,
            zone_inflow=None,
            zone_outflow=None,
            zone_inflow_per_minute=None,
            zone_outflow_per_minute=None,
            mean_speed=None,
            waiting_time=None,
            queue_length=None,
            max_queue_length=None,
            throughput=0,
            stops_count=0,
            blocked_outgoing_share=blocked_outgoing_share,
        )

    def test_summary_csv_keeps_all_four_control_modes_in_canonical_order(self) -> None:
        collector = MetricsCollector(
            FakeTraci(),
            AreaModel(()),
            ControlConfig(),
        )
        with TemporaryDirectory() as temp_dir:
            results_dir = Path(temp_dir)
            for index, mode in enumerate(reversed(CONTROL_MODES)):
                collector.write(results_dir, {"mode": mode, "value": index})

            with (results_dir / "summary.csv").open(
                newline="",
                encoding="utf-8",
            ) as handle:
                modes = [row["mode"] for row in csv.DictReader(handle)]

        self.assertEqual(tuple(modes), CONTROL_MODES)

    def test_lifecycle_events_are_counted_between_metric_samples(self) -> None:
        traci = FakeTraci()
        collector = MetricsCollector(
            traci,
            AreaModel(()),
            ControlConfig(decision_interval=5),
            priority_vehicle="veh1",
        )

        traci.simulation.departed = ("veh1",)
        traci.vehicle.active = {"veh1"}
        collector.collect(1.0)
        traci.simulation.departed = ()
        traci.simulation.arrived = ("veh1",)
        collector.collect(3.0)
        traci.vehicle.active = set()
        traci.simulation.arrived = ()
        collector.collect(5.0)

        summary = collector.summary("fixed", 5.0)
        self.assertEqual(summary["throughput"], 1)
        self.assertEqual(summary["departed_vehicles"], 1)
        self.assertEqual(summary["peak_active_vehicles"], 1)
        self.assertEqual(summary["average_travel_time"], 2.0)
        self.assertEqual(summary["emergency_departure_time"], 1.0)
        self.assertEqual(summary["emergency_arrival_time"], 3.0)
        self.assertEqual(summary["emergency_eta"], 2.0)
        self.assertEqual(summary["emergency_trace_samples"], 2)
        self.assertEqual(collector.emergency_trace[0].edge_id, "middle")
        self.assertEqual(collector.emergency_trace[0].remaining_edges, 1)
        self.assertEqual(len(collector.samples), 1)

    def test_live_status_snapshot_is_written(self) -> None:
        traci = FakeTraci()
        collector = MetricsCollector(
            traci,
            AreaModel(()),
            ControlConfig(decision_interval=3),
            priority_vehicle=None,
        )

        collector.collect(3.0)

        with TemporaryDirectory() as temp_dir:
            output_path = collector.write_live_status(
                Path(temp_dir),
                "flowmind",
                3.0,
                decision_log=[
                    {
                        "time": 3.0,
                        "category": "controller",
                        "title": "Продовжено зелену фазу",
                        "detail": "Тестове рішення",
                    }
                ],
            )
            self.assertEqual(output_path, Path(temp_dir) / "live_status.json")
            self.assertTrue(output_path.exists())
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["mode"], "flowmind")
            self.assertEqual(payload["simulated_time"], 3.0)
            self.assertEqual(payload["latest_sample"]["time"], 3.0)
            self.assertEqual(payload["summary"]["throughput"], 0)
            self.assertEqual(payload["schema_version"], 2)
            self.assertEqual(len(payload["metric_history"]), 1)
            self.assertEqual(payload["traffic_flow"]["active_network"], 0)
            self.assertIsNone(payload["traffic_flow"]["inflow_per_minute"])
            self.assertEqual(payload["intersections"], [])
            self.assertEqual(payload["vehicles"], [])
            self.assertFalse(payload["zone_simulation"]["active"])
            self.assertEqual(
                payload["decision_log"][0]["title"],
                "Продовжено зелену фазу",
            )
            self.assertFalse(output_path.with_suffix(".json.tmp").exists())

    def test_live_status_contains_active_sumo_map_snapshot(self) -> None:
        area = AreaModel(
            (
                Intersection(
                    tls_id="tls_1",
                    position=(10.0, 20.0),
                    phases=("Gr",),
                    links=(
                        ControlledLink(
                            "in_0",
                            "out_0",
                            0,
                            incoming_shape=((0.0, 20.0), (10.0, 20.0)),
                            outgoing_shape=((10.0, 20.0), (20.0, 20.0)),
                        ),
                    ),
                    phase_durations=(39.0,),
                    program_id="0",
                    program_type="actuated",
                    phase_min_durations=(13.0,),
                    phase_max_durations=(50.0,),
                ),
            )
        )
        collector = MetricsCollector(
            ActiveFakeTraci(),
            area,
            ControlConfig(decision_interval=3),
        )
        collector.collect(3.0)

        with TemporaryDirectory() as temp_dir:
            output_path = collector.write_live_status(
                Path(temp_dir),
                "flowmind",
                3.0,
                system_status={"simulation": {"status": "running"}},
            )
            payload = json.loads(output_path.read_text(encoding="utf-8"))

        intersection = payload["intersections"][0]
        self.assertEqual((intersection["x"], intersection["y"]), (10.0, 20.0))
        self.assertTrue(intersection["active_now"])
        self.assertEqual(intersection["movements"][0]["state"], "G")
        self.assertEqual(intersection["program_id"], "0")
        self.assertEqual(intersection["program_type"], "actuated")
        self.assertEqual(intersection["phase_duration"], 39.0)
        self.assertEqual(intersection["phase_min_duration"], 13.0)
        self.assertEqual(intersection["phase_max_duration"], 50.0)
        vehicle = payload["vehicles"][0]
        self.assertEqual(vehicle["lane_id"], "in_0")
        self.assertEqual(vehicle["angle"], 90.0)
        self.assertEqual(vehicle["color"], "#0c2238")
        zone = payload["zone_simulation"]
        self.assertTrue(zone["active"])
        self.assertEqual(zone["source"], "sumo")
        self.assertEqual(zone["intersections"][0]["tls_id"], "tls_1")
        self.assertEqual(zone["lanes"][0]["shape"], [[0.0, 20.0], [10.0, 20.0]])
        self.assertEqual(zone["lanes"][0]["vehicle_count"], 1)
        self.assertEqual(zone["lanes"][0]["queue"], 1)
        self.assertEqual(zone["lanes"][0]["occupancy"], 0.0625)

    def test_civilian_impact_uses_observed_corridor_state_samples(self) -> None:
        collector = MetricsCollector(
            ActiveFakeTraci(),
            self.active_area(),
            ControlConfig(decision_interval=3),
            priority_vehicle="ambulance",
        )

        collector.set_corridor_state("NORMAL")
        collector.collect(3.0)
        collector.set_corridor_state("GREEN_WINDOW")
        collector.collect(6.0)
        summary = collector.summary("flowmind", 6.0)

        self.assertEqual(summary["civilian_priority_samples"], 1)
        self.assertEqual(summary["civilian_normal_mean_waiting_time"], 0.0)
        self.assertEqual(summary["civilian_priority_mean_waiting_time"], 0.0)
        self.assertEqual(summary["civilian_priority_waiting_delta"], 0.0)

    def test_live_flow_rates_use_changes_between_samples(self) -> None:
        traci = FakeTraci()
        boundary = ZoneBoundary(
            incoming_lane_ids=("entry_0",),
            outgoing_lane_ids=("exit_0",),
            incoming_edge_ids=("entry",),
            outgoing_edge_ids=("exit",),
        )
        collector = MetricsCollector(
            traci,
            AreaModel(()),
            ControlConfig(decision_interval=3),
            zone_boundary=boundary,
        )
        traci.simulation.departed = ("veh1",)
        traci.edge.vehicles = {"entry": ("veh1",), "exit": ()}
        collector.collect(3.0)
        traci.simulation.departed = ("veh2", "veh3")
        traci.simulation.arrived = ("veh1",)
        traci.edge.vehicles = {
            "entry": ("veh2", "veh3"),
            "exit": ("veh1",),
        }
        collector.collect(6.0)

        latest = collector.samples[-1]
        self.assertEqual(latest.departed, 3)
        self.assertEqual(latest.arrived, 1)
        self.assertEqual(latest.zone_inflow, 3)
        self.assertEqual(latest.zone_outflow, 1)
        self.assertEqual(latest.zone_inflow_per_minute, 40.0)
        self.assertEqual(latest.zone_outflow_per_minute, 20.0)

    def test_unfinished_travel_times_are_reported_as_censored(self) -> None:
        traci = FakeTraci()
        collector = MetricsCollector(
            traci,
            AreaModel(()),
            ControlConfig(decision_interval=5),
        )
        traci.simulation.departed = ("veh1",)
        collector.collect(1.0)
        traci.simulation.departed = ()
        collector.collect(5.0)

        summary = collector.summary("flowmind", 5.0)

        self.assertIsNone(summary["average_travel_time"])
        self.assertEqual(summary["completed_trips"], 0)
        self.assertEqual(summary["unfinished_trips"], 1)
        self.assertEqual(summary["unfinished_travel_time_lower_bound_mean"], 4.0)
        outcome = collector.trip_outcomes(5.0)[0]
        self.assertEqual(outcome.status, "unfinished_censored")
        self.assertIsNone(outcome.travel_time)

    def test_summary_reports_time_without_spillback_and_episode_count(self) -> None:
        collector = MetricsCollector(
            FakeTraci(),
            AreaModel(()),
            ControlConfig(decision_interval=3),
        )
        collector.samples = [
            self.metric_sample(3.0, 0.0),
            self.metric_sample(6.0, 0.05),
            self.metric_sample(9.0, 0.10),
            self.metric_sample(12.0, 0.0),
            self.metric_sample(15.0, 0.08),
        ]

        summary = collector.summary("flowmind", 15.0)

        self.assertEqual(summary["spillback_free_time_share"], 0.4)
        self.assertEqual(summary["spillback_episode_count"], 2)

    def test_spillback_summary_is_unavailable_without_outgoing_observations(
        self,
    ) -> None:
        collector = MetricsCollector(
            FakeTraci(),
            AreaModel(()),
            ControlConfig(decision_interval=3),
        )
        collector.samples = [
            self.metric_sample(3.0, None),
            self.metric_sample(6.0, None),
        ]

        summary = collector.summary("local", 6.0)

        self.assertIsNone(summary["spillback_free_time_share"])
        self.assertIsNone(summary["spillback_episode_count"])

    def test_fractional_steps_do_not_duplicate_metric_samples(self) -> None:
        collector = MetricsCollector(
            FakeTraci(),
            AreaModel(()),
            ControlConfig(decision_interval=3),
        )

        for simulation_time in (3.1, 3.5, 3.9, 6.2):
            collector.collect(simulation_time)

        self.assertEqual([sample.time for sample in collector.samples], [3.1, 6.2])


if __name__ == "__main__":
    unittest.main()
