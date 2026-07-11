from __future__ import annotations

import csv
import json
import os
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import sumolib
import traci


SUMO_START_RETRIES = 30
SUMO_GUI_START_RETRIES = 120


def start_sumo(command: list[str]) -> None:
    """Start SUMO with a bounded retry count to avoid hanging indefinitely."""
    try:
        traci.start(
            command,
            # Loading the Rivne map takes several seconds on a cold start.
            # sumo-gui also initializes fonts/OpenGL and can take much longer
            # before TraCI begins listening on a cold desktop session.
            numRetries=_sumo_start_retries(command),
            verbose=False,
        )
    except (traci.TraCIException, traci.FatalTraCIError) as error:
        details = _sumo_log_tail(command)
        suffix = f"\nSUMO log:\n{details}" if details else ""
        raise RuntimeError(f"SUMO failed to start: {error}{suffix}") from error


def _sumo_start_retries(command: list[str]) -> int:
    if command and Path(command[0]).name.startswith("sumo-gui"):
        return SUMO_GUI_START_RETRIES
    return SUMO_START_RETRIES


def _sumo_log_tail(command: list[str], max_lines: int = 20) -> str:
    try:
        log_index = command.index("--log") + 1
        log_path = Path(command[log_index])
    except (ValueError, IndexError):
        return ""
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-max_lines:])

from .area_model import (
    AreaModel,
    discover_area,
    load_zone_tls_ids,
)
from .config import (
    ADAPTIVE_CONTROL_MODES,
    STATIC_FIXED_MODE,
    RunConfig,
)
from .controller import AreaSignalController
from .corridor_manager import CorridorManager
from .decision_feed import DecisionEvent, merged_decision_log
from .emergency_router import EmergencyRouter
from .emergency_vehicle import EmergencyVehicleManager
from .live_transport import LiveTelemetryPublisher
from .metrics import MetricsCollector
from .ml_dataset import MLDatasetCollector, MLDatasetConfig
from .queue_forecast import (
    QueueForecastEnsemble,
    QueueForecastModel,
    QueueForecastStats,
    load_queue_forecast_models,
)
from .tls_programs import (
    StaticProgramActivation,
    activate_static_fixed_programs,
)


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
        "--log",
        str(raw_dir / f"{config.mode}_sumo.log"),
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
    write_live_run_status(config, "starting")
    connection = None
    try:
        area = load_area(config)
        net_path = config.config_path.resolve().parent / "osm.net.xml.gz"
        queue_forecast = load_queue_forecast(config)
        queue_forecast_stats = (
            queue_forecast.stats if queue_forecast is not None else None
        )
        configure_projection_data()
        start_sumo(_sumo_command(config))
        connection = traci.getConnection()
        emergency_details = None
        emergency_manager = None
        corridor_manager = None
        alternatives_log = []
        emergency_route_tls: tuple[str, ...] = ()
        emergency_controlled_tls: tuple[str, ...] = ()
        static_programs: tuple[StaticProgramActivation, ...] = ()
        dataset_collector = None
        session_events = [
            DecisionEvent(
                time=0.0,
                category="system",
                title="Симуляцію запущено",
                detail=f"Активний режим керування: {config.mode}.",
            )
        ]

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
            session_events.append(
                DecisionEvent(
                    time=0.0,
                    category="route",
                    title="Маршрут швидкої обрано",
                    detail=(
                        f"{emergency_details.route_edge_count} ділянок, "
                        f"прогнозований ETA "
                        f"{emergency_details.predicted_eta:.0f} с."
                    ),
                    level="success",
                )
            )
            if config.mode in ADAPTIVE_CONTROL_MODES:
                corridor_manager = CorridorManager(config.emergency.vehicle_id)

        if config.mode == STATIC_FIXED_MODE:
            static_programs = activate_static_fixed_programs(
                connection,
                area.tls_ids,
            )
            session_events.append(
                DecisionEvent(
                    time=0.0,
                    category="system",
                    title="Static fixed-time baseline активовано",
                    detail=(
                        f"SUMO STATIC-програму перевірено для "
                        f"{len(static_programs)} світлофорів."
                    ),
                    level="success",
                )
            )
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
                queue_forecast,
                config.dataset_sample_interval,
            )
            if config.mode in ADAPTIVE_CONTROL_MODES
            else None
        )
        if controller is not None and corridor_manager is not None:
            controller.set_corridor_manager(corridor_manager)
        publisher = LiveTelemetryPublisher(
            host="127.0.0.1",
            port=config.websocket_port,
        )
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
                if emergency_manager is not None:
                    emergency_manager.update_corridor_visualization(
                        corridor_manager.state,
                        corridor_manager.active_tls,
                    )

            if controller is not None:
                controller.step(simulated_time)
            metrics.collect(simulated_time)
            metrics.write_live_status(
                config.results_dir,
                config.mode,
                simulated_time,
                build_live_system_status(
                    config=config,
                    connection=connection,
                    controller=controller,
                    corridor_manager=corridor_manager,
                    publisher=publisher,
                    queue_forecast=queue_forecast,
                    running=True,
                ),
                decision_log=build_decision_log(
                    session_events,
                    controller,
                    corridor_manager,
                ),
            )
            if dataset_collector is not None:
                dataset_collector.collect(simulated_time)

        summary = metrics.summary(config.mode, simulated_time)
        summary.update(static_program_summary(static_programs))
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
                    "phase_out_of_range_skips": (
                        controller.stats.phase_out_of_range_skips
                    ),
                    "clearance_phase_skips": controller.stats.clearance_phase_skips,
                    "min_green_skips": controller.stats.min_green_skips,
                    "scoreless_skips": controller.stats.scoreless_skips,
                    "queue_forecast_enabled": queue_forecast is not None,
                    "queue_forecast_predictions": (
                        controller.stats.queue_forecast_predictions
                    ),
                    "queue_forecast_failures": controller.stats.queue_forecast_failures,
                    "queue_forecast_trace_samples": len(
                        controller.stats.queue_forecast_samples
                    ),
                }
            )
            write_queue_forecast_trace(
                config.results_dir,
                config.mode,
                controller.stats.queue_forecast_samples,
            )
        else:
            summary.update(
                {
                    "controller_decisions": 0,
                    "phase_extensions": 0,
                    "phase_advances": 0,
                    "priority_decisions": 0,
                    "phase_out_of_range_skips": 0,
                    "clearance_phase_skips": 0,
                    "min_green_skips": 0,
                    "scoreless_skips": 0,
                    "queue_forecast_enabled": False,
                    "queue_forecast_predictions": 0,
                    "queue_forecast_failures": 0,
                    "queue_forecast_trace_samples": 0,
                }
            )
        summary.update(queue_forecast_summary(queue_forecast_stats))
        if dataset_collector is not None:
            summary.update(dataset_collector.write())
        session_events.append(
            DecisionEvent(
                time=round(simulated_time, 3),
                category="system",
                title="Симуляцію завершено",
                detail=(
                    f"Завершили маршрут {summary.get('throughput', 0)} авто; "
                    f"середнє очікування "
                    f"{float(summary.get('average_waiting_time', 0.0)):.1f} с."
                ),
                level="success",
            )
        )
        metrics.write(config.results_dir, summary)
        metrics.write_live_status(
            config.results_dir,
            config.mode,
            simulated_time,
            build_live_system_status(
                config=config,
                connection=connection,
                controller=controller,
                corridor_manager=corridor_manager,
                publisher=publisher,
                queue_forecast=queue_forecast,
                running=False,
            ),
            decision_log=build_decision_log(
                session_events,
                controller,
                corridor_manager,
            ),
        )
        return summary
    except Exception as error:
        write_live_run_status(config, "failed", str(error))
        raise
    finally:
        if connection is not None:
            connection.close()
        if "publisher" in locals():
            publisher.stop()


def write_live_run_status(
    config: RunConfig,
    status: str,
    error: str | None = None,
) -> Path:
    config.results_dir.mkdir(parents=True, exist_ok=True)
    output_path = config.results_dir / "live_status.json"
    payload: dict[str, object] = {}
    if status != "starting" and output_path.exists():
        try:
            loaded = json.loads(output_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                payload = loaded
        except (OSError, json.JSONDecodeError):
            pass

    payload.update(
        {
            "schema_version": 2,
            "mode": config.mode,
            "results_dir": str(config.results_dir.resolve()),
            "simulated_time": payload.get("simulated_time", 0.0),
            "emitted_at": datetime.now(timezone.utc).isoformat(),
            "summary": payload.get("summary", {}),
            "latest_sample": payload.get("latest_sample"),
            "metric_history": payload.get("metric_history", []),
            "traffic_flow": payload.get("traffic_flow", {}),
            "lanes": payload.get("lanes", []),
            "intersections": payload.get("intersections", []),
            "vehicles": payload.get("vehicles", []),
            "decision_log": payload.get("decision_log", []),
            "emergency_trace": payload.get("emergency_trace", []),
        }
    )
    system = payload.get("system", {})
    if not isinstance(system, dict):
        system = {}
    simulation = {
        "status": status,
        "expected_vehicles": 0,
        "mode": config.mode,
        "duration": config.duration,
    }
    if error:
        simulation["error"] = error
    system["simulation"] = simulation
    system.setdefault(
        "websocket",
        {
            "status": "waiting",
            "clients": 0,
            "host": "127.0.0.1",
            "port": config.websocket_port,
        },
    )
    system.setdefault("controller", {"status": "waiting"})
    system.setdefault("queue_forecast", {"status": "waiting"})
    system.setdefault("corridor", {"corridor_state": "waiting"})
    system.setdefault(
        "metrics",
        {
            "status": "waiting",
            "sensor_model": "intersection_camera_detector",
            "sensor_range_meters": config.control.sensor_range_meters,
        },
    )
    payload["system"] = system
    zone_simulation = payload.get("zone_simulation")
    if isinstance(zone_simulation, dict):
        zone_simulation["status"] = status
        zone_simulation["active"] = False
        payload["zone_simulation"] = zone_simulation

    temporary_path = output_path.with_suffix(".json.tmp")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_path.replace(output_path)
    return output_path


def build_live_system_status(
    *,
    config: RunConfig,
    connection: object,
    controller: AreaSignalController | None,
    corridor_manager: CorridorManager | None,
    publisher: LiveTelemetryPublisher,
    queue_forecast: QueueForecastModel | QueueForecastEnsemble | None,
    running: bool,
) -> dict[str, object]:
    controller_stats = controller.stats if controller is not None else None
    corridor_summary = (
        corridor_manager.as_summary()
        if corridor_manager is not None
        else {
            "corridor_state": "DISABLED",
            "corridor_active_tls": None,
            "corridor_completed_tls": (),
        }
    )
    try:
        expected_vehicles = int(connection.simulation.getMinExpectedNumber())
    except Exception:
        expected_vehicles = 0
    return {
        "simulation": {
            "status": "running" if running else "completed",
            "expected_vehicles": expected_vehicles,
            "mode": config.mode,
            "duration": config.duration,
        },
        "websocket": {
            "status": "online",
            "clients": publisher.client_count,
            "host": "127.0.0.1",
            "port": config.websocket_port,
        },
        "controller": {
            "status": "active" if controller is not None else config.mode,
            "mode": config.mode,
            "decisions": controller_stats.decisions if controller_stats else 0,
            "extensions": controller_stats.extensions if controller_stats else 0,
            "advances": controller_stats.advances if controller_stats else 0,
            "priority_decisions": (
                controller_stats.priority_decisions if controller_stats else 0
            ),
            "safety_skips": (
                controller_stats.phase_out_of_range_skips
                + controller_stats.clearance_phase_skips
                + controller_stats.min_green_skips
                + controller_stats.scoreless_skips
                if controller_stats
                else 0
            ),
        },
        "queue_forecast": {
            "status": "active" if queue_forecast is not None else "disabled",
            "predictions": (
                controller_stats.queue_forecast_predictions
                if controller_stats
                else 0
            ),
            "failures": (
                controller_stats.queue_forecast_failures
                if controller_stats
                else 0
            ),
        },
        "corridor": corridor_summary,
        "metrics": {
            "status": "collecting" if running else "finalized",
            "sample_interval": config.control.decision_interval,
            "sensor_model": "intersection_camera_detector",
            "sensor_range_meters": config.control.sensor_range_meters,
            "coverage": "controlled_intersections_only",
        },
    }


def build_decision_log(
    session_events: list[DecisionEvent],
    controller: AreaSignalController | None,
    corridor_manager: CorridorManager | None,
) -> list[dict[str, object]]:
    controller_events = (
        controller.stats.decision_events if controller is not None else ()
    )
    corridor_events = (
        corridor_manager.decision_events if corridor_manager is not None else ()
    )
    return merged_decision_log(
        session_events,
        controller_events,
        corridor_events,
    )


def load_queue_forecast(
    config: RunConfig,
) -> QueueForecastModel | QueueForecastEnsemble | None:
    if config.mode != "flowmind":
        return None
    if config.queue_model_path is not None:
        model_paths = (config.queue_model_path,)
    else:
        model_paths = tuple(config.queue_model_paths)
    if not model_paths:
        return None
    return load_queue_forecast_models(
        model_paths,
        config.control.queue_forecast_horizon_weights,
    )


def queue_forecast_summary(stats: QueueForecastStats | None) -> dict[str, object]:
    if stats is None:
        return {
            "queue_forecast_model": "",
            "queue_forecast_target": "",
            "queue_forecast_feature_count": 0,
            "queue_forecast_model_count": 0,
            "queue_forecast_horizons": "",
            "queue_forecast_horizon_weights": "",
        }
    return {
        "queue_forecast_model": stats.model_path,
        "queue_forecast_target": stats.target,
        "queue_forecast_feature_count": stats.feature_count,
        "queue_forecast_model_count": stats.model_count,
        "queue_forecast_horizons": stats.horizons,
        "queue_forecast_horizon_weights": stats.horizon_weights,
    }


def static_program_summary(
    activations: tuple[StaticProgramActivation, ...],
) -> dict[str, object]:
    return {
        "static_fixed_program_count": len(activations),
        "static_fixed_program_id": (
            activations[0].program_id if activations else ""
        ),
        "static_fixed_source_programs": ",".join(
            f"{item.tls_id}:{item.source_program_id}" for item in activations
        ),
    }


def write_queue_forecast_trace(
    results_dir: Path,
    mode: str,
    samples: object,
) -> None:
    samples = list(samples)
    if not samples:
        return
    results_dir.mkdir(parents=True, exist_ok=True)
    trace_path = results_dir / f"{mode}_queue_forecast.csv"
    with trace_path.open("w", newline="", encoding="utf-8") as handle:
        fieldnames = list(asdict(samples[0]).keys())
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(asdict(sample) for sample in samples)


def _merge_tls_ids(
    base_tls: tuple[str, ...],
    route_tls: tuple[str, ...],
) -> tuple[str, ...]:
    merged = list(base_tls)
    for tls_id in route_tls:
        if tls_id not in merged:
            merged.append(tls_id)
    return tuple(merged)
