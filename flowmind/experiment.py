from __future__ import annotations

from pathlib import Path

import sumolib
import traci

from .area_model import AreaModel, discover_area, load_zone_tls_ids
from .config import RunConfig
from .controller import AreaSignalController
from .corridor_manager import CorridorManager
from .emergency_router import EmergencyRouter
from .emergency_vehicle import EmergencyVehicleManager
from .metrics import MetricsCollector


def _sumo_command(config: RunConfig) -> list[str]:
    scenario_dir = config.config_path.resolve().parent
    binary = sumolib.checkBinary("sumo-gui" if config.gui else "sumo")
    raw_dir = config.results_dir.resolve() / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    command = [
        binary,
        "-c",
        str(config.config_path.resolve()),
        "--additional-files",
        str(scenario_dir / "osm.poly.xml.gz"),
        "--tripinfo-output",
        str(raw_dir / f"{config.mode}_tripinfos.xml"),
        "--statistic-output",
        str(raw_dir / f"{config.mode}_stats.xml"),
        "--seed",
        str(config.seed),
        "--end",
        str(config.duration),
        "--no-step-log",
        "true",
        "--duration-log.disable",
        "true",
        "--ignore-route-errors",
        "true",
        "--quit-on-end",
        "true",
    ]
    if config.gui:
        command.extend(["--delay", str(config.gui_delay_ms), "--start"])
    return command


def load_area(config: RunConfig) -> AreaModel:
    net_path = config.config_path.resolve().parent / "osm.net.xml.gz"
    tls_ids = config.tls_ids or load_zone_tls_ids(config.zone_path)
    return discover_area(net_path, config.zone_size, tls_ids)


def run_experiment(config: RunConfig) -> dict[str, object]:
    area = load_area(config)
    connection = None
    try:
        traci.start(_sumo_command(config))
        connection = traci.getConnection()
        emergency_details = None
        corridor_manager = None
        alternatives_log = []

        if config.emergency is not None:
            router = EmergencyRouter(connection, area)
            best_route, alternatives_log = router.find_alternatives(
                config.emergency.start.edge_id,
                config.emergency.destination.edge_id,
                config.emergency.base_vehicle_type_id,
                config.emergency.depart_time,
            )
            edges = best_route.edge_ids if best_route else None
            
            emergency_manager = EmergencyVehicleManager(connection, config.emergency)
            emergency_details = emergency_manager.install(precalculated_edges=edges)
            
            corridor_manager = CorridorManager(config.emergency.vehicle_id)
        priority_vehicle = (
            config.emergency.vehicle_id
            if config.emergency is not None
            else config.priority_vehicle
        )
        controller = (
            AreaSignalController(
                connection,
                area,
                config.mode,
                config.control,
                priority_vehicle,
            )
            if config.mode != "fixed"
            else None
        )
        if controller is not None and corridor_manager is not None:
            controller.set_corridor_manager(corridor_manager)
        metrics = MetricsCollector(
            connection, area, config.control, priority_vehicle
        )

        simulated_time = 0.0
        while (
            connection.simulation.getMinExpectedNumber() > 0
            and simulated_time < config.duration
        ):
            connection.simulationStep()
            simulated_time = float(connection.simulation.getTime())
            
            if corridor_manager is not None:
                veh_id = corridor_manager.vehicle_id
                in_network = veh_id in connection.vehicle.getIDList()
                next_tls_info = None
                if in_network:
                    next_tls_list = connection.vehicle.getNextTLS(veh_id)
                    if next_tls_list:
                        next_tls_info = (str(next_tls_list[0][0]), int(next_tls_list[0][1]), float(next_tls_list[0][2]))
                corridor_manager.step(simulated_time, in_network, next_tls_info)

            if controller is not None:
                controller.step(simulated_time)
            metrics.collect(simulated_time)

        summary = metrics.summary(config.mode, simulated_time)
        if emergency_details is not None:
            summary.update(emergency_details.as_summary())
            summary["emergency_alternatives"] = alternatives_log
        if controller is not None:
            summary.update(
                {
                    "controller_decisions": controller.stats.decisions,
                    "phase_extensions": controller.stats.extensions,
                    "phase_advances": controller.stats.advances,
                    "priority_decisions": controller.stats.priority_decisions,
                }
            )
        else:
            summary.update(
                {
                    "controller_decisions": 0,
                    "phase_extensions": 0,
                    "phase_advances": 0,
                    "priority_decisions": 0,
                }
            )
        metrics.write(config.results_dir, summary)
        return summary
    finally:
        if connection is not None:
            connection.close()
