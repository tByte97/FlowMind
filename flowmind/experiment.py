from __future__ import annotations

import os
from pathlib import Path

import sumolib
import traci


def start_sumo(command: list[str]) -> None:
    """Start SUMO with a bounded retry count to avoid hanging indefinitely."""
    try:
        traci.start(
            command,
            numRetries=1,
            verbose=False,
        )
    except traci.TraCIException as error:
        raise RuntimeError(f"SUMO failed to start: {error}") from error

from .area_model import AreaModel, discover_area, load_zone_tls_ids
from .config import RunConfig
from .controller import AreaSignalController
from .corridor_manager import CorridorManager
from .emergency_router import EmergencyRouter
from .emergency_vehicle import EmergencyVehicleManager
from .live_transport import LiveTelemetryPublisher
from .metrics import MetricsCollector
from .ml_dataset import MLDatasetCollector, MLDatasetConfig


def configure_projection_data() -> Path | None:
    """Point packaged SUMO binaries at their bundled PROJ database."""

    try:
        import sumo
    except ImportError:
        return None
    projection_dir = Path(sumo.SUMO_HOME) / "data" / "proj"
    if not (projection_dir / "proj.db").is_file():
        return None
    os.environ.setdefault("PROJ_DATA", str(projection_dir))
    os.environ.setdefault("PROJ_LIB", str(projection_dir))
    return projection_dir


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
    net_path = config.config_path.resolve().parent / "osm.net.xml.gz"
    connection = None
    try:
        configure_projection_data()
        start_sumo(_sumo_command(config))
        connection = traci.getConnection()
        emergency_details = None
        corridor_manager = None
        alternatives_log = []
        emergency_route_tls: tuple[str, ...] = ()
        emergency_controlled_tls: tuple[str, ...] = ()
        dataset_collector = None

        if config.emergency is not None:
            router = EmergencyRouter(
                connection,
                area,
                net_path,
            )
            best_route, alternatives_log = router.find_alternatives(
                config.emergency.start.edge_id,
                config.emergency.destination.edge_id,
                config.emergency.base_vehicle_type_id,
                config.emergency.depart_time,
                num_alternatives=5,
            )
            edges = best_route.edge_ids if best_route else None
            emergency_route_tls = best_route.tls_sequence if best_route else ()
            if emergency_route_tls:
                requested_tls = _merge_tls_ids(area.tls_ids, emergency_route_tls)
                area = discover_area(
                    net_path,
                    requested_tls=requested_tls,
                    strict_requested=False,
                )
                emergency_controlled_tls = tuple(
                    tls_id for tls_id in emergency_route_tls if tls_id in area.tls_ids
                )

            emergency_manager = EmergencyVehicleManager(connection, config.emergency)
            emergency_details = emergency_manager.install(
                precalculated_edges=edges,
                route_length=best_route.length if best_route else None,
                expected_travel_time=(
                    best_route.base_travel_time if best_route else None
                ),
                predicted_eta=best_route.predicted_eta if best_route else None,
            )

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
        publisher = LiveTelemetryPublisher(host="127.0.0.1", port=8765)
        publisher.start()
        metrics = MetricsCollector(
            connection, area, config.control, priority_vehicle
        )
        metrics.set_live_publisher(publisher)
        if config.dataset_dir is not None:
            dataset_collector = MLDatasetCollector(
                connection,
                area,
                config.control,
                MLDatasetConfig(
                    output_dir=config.dataset_dir,
                    run_id=(
                        config.dataset_run_id
                        or f"{config.dataset_scenario}_{config.mode}_{config.seed}"
                    ),
                    scenario=config.dataset_scenario,
                    mode=config.mode,
                    seed=config.seed,
                    duration=config.duration,
                    sample_interval=config.dataset_sample_interval,
                    target_horizons=config.dataset_target_horizons,
                ),
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
                        next_tls_info = [
                            (str(tls_id), int(link_index), float(distance))
                            for tls_id, link_index, distance, *_state in next_tls_list
                        ]
                corridor_manager.step(simulated_time, in_network, next_tls_info)

            if controller is not None:
                controller.step(simulated_time)
            metrics.collect(simulated_time)
            metrics.write_live_status(config.results_dir, config.mode, simulated_time)
            if dataset_collector is not None:
                dataset_collector.collect(simulated_time)

        summary = metrics.summary(config.mode, simulated_time)
        if emergency_details is not None:
            summary.update(emergency_details.as_summary())
            summary["emergency_alternatives"] = alternatives_log
            summary["emergency_route_tls"] = emergency_route_tls
            summary["emergency_controlled_tls"] = emergency_controlled_tls
            if corridor_manager is not None:
                summary.update(corridor_manager.as_summary())
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
        if dataset_collector is not None:
            summary.update(dataset_collector.write())
        metrics.write(config.results_dir, summary)
        return summary
    finally:
        if connection is not None:
            connection.close()
        if "publisher" in locals():
            publisher.stop()


def _merge_tls_ids(
    base_tls: tuple[str, ...],
    route_tls: tuple[str, ...],
) -> tuple[str, ...]:
    merged = list(base_tls)
    for tls_id in route_tls:
        if tls_id not in merged:
            merged.append(tls_id)
    return tuple(merged)
