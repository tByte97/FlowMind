from __future__ import annotations

import csv
import json
import os
import xml.etree.ElementTree as ET
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
    SUMO_ACTUATED_MODE,
    RunConfig,
)
from .controller import AreaSignalController
from .corridor_manager import CorridorManager
from .decision_feed import DecisionEvent, merged_decision_log
from .dataset_quality import parse_missing_detector_links
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
from .provenance import run_provenance_summary
from .runtime_contract import (
    RuntimeContract,
    validate_runtime_contract,
    write_runtime_contract_audit,
)
from .sumo_tls_adapter import SumoTlsSafetyAdapter
from .sumo_corridor_adapter import SumoCorridorObservationAdapter
from .sumo_zone_graph_adapter import SumoZoneGraphAdapter
from .tls_programs import (
    ActiveTlsProgram,
    StaticProgramActivation,
    activate_static_fixed_programs,
    inspect_active_tls_programs,
    write_tls_program_startup_audit,
)
from .tls_safety import (
    ActivePlanExpectation,
    TlsSafetyReport,
    validate_tls_catalog,
)
from .tls_safety_audit import write_tls_safety_startup_audit
from .zone_graph import AreaGraph, load_zone_definition
from .zone_boundary import build_zone_boundary


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
        "--scale",
        str(config.demand_scale),
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
    profiled_route = _profiled_route_file(config)
    if profiled_route is not None:
        command.extend(["--route-files", str(profiled_route)])
    if config.gui:
        command.extend(["--delay", str(config.gui_delay_ms), "--start"])
    return command


def _profiled_route_file(config: RunConfig) -> Path | None:
    if config.demand_profile not in {"morning_peak", "evening_peak"}:
        return None
    try:
        sumo_root = ET.parse(config.config_path).getroot()
        route_value = next(
            element.attrib["value"]
            for element in sumo_root.findall(".//route-files")
            if element.attrib.get("value")
        )
        source = config.config_path.resolve().parent / route_value.split(",")[0]
        tree = ET.parse(source)
    except (OSError, ET.ParseError, KeyError, StopIteration) as error:
        raise RuntimeError(f"Cannot prepare demand profile routes: {error}") from error
    flows = tree.getroot().findall("flow")
    midpoint = max(len(flows) // 2, 1)
    for index, flow in enumerate(flows):
        base = float(flow.attrib.get("vehsPerHour", "0"))
        favoured = index < midpoint
        if config.demand_profile == "evening_peak":
            favoured = not favoured
        factor = 1.35 if favoured else 0.70
        flow.set("vehsPerHour", f"{base * factor:.3f}")
    output = (
        config.results_dir.resolve()
        / "raw"
        / f"{config.mode}_{config.demand_profile}.rou.xml"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    tree.write(output, encoding="utf-8", xml_declaration=True)
    return output


def load_area(config: RunConfig) -> AreaModel:
    net_path = config.config_path.resolve().parent / "osm.net.xml.gz"
    tls_ids = config.tls_ids or load_zone_tls_ids(config.zone_path)
    return discover_area(net_path, config.zone_size, tls_ids)


def audit_actuated_detector_startup(
    config: RunConfig,
    area: AreaModel,
) -> dict[str, object]:
    log_path = config.results_dir.resolve() / "raw" / f"{config.mode}_sumo.log"
    try:
        log_text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        log_text = ""
    missing = parse_missing_detector_links(log_text)
    controlled_links = {
        (intersection.tls_id, link.signal_index)
        for intersection in area.intersections
        for link in intersection.links
    }
    relevant_missing = missing & controlled_links
    coverage = (
        1.0 - len(relevant_missing) / len(controlled_links)
        if controlled_links
        else 0.0
    )
    payload: dict[str, object] = {
        "status": "pass" if not relevant_missing else "fail",
        "coverage_complete": not relevant_missing,
        "coverage": round(coverage, 7),
        "controlled_link_count": len(controlled_links),
        "controlled_links": [
            {"tls_id": tls_id, "signal_index": signal_index}
            for tls_id, signal_index in sorted(controlled_links)
        ],
        "missing_link_count": len(relevant_missing),
        "missing_by_tls": {
            tls_id: sorted(
                index for item_tls, index in relevant_missing if item_tls == tls_id
            )
            for tls_id in sorted(
                {tls_id for tls_id, _index in relevant_missing}
            )
        },
        "sumo_log": str(log_path),
    }
    audit_path = config.results_dir.resolve() / "actuated_detector_startup_audit.json"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    payload["audit_path"] = str(audit_path)
    return payload


def run_experiment(config: RunConfig) -> dict[str, object]:
    if config.enable_live_telemetry:
        write_live_run_status(config, "starting")
    connection = None
    try:
        evaluation_area = load_area(config)
        area = evaluation_area
        zone_definition = load_zone_definition(config.zone_path)
        net_path = config.config_path.resolve().parent / "osm.net.xml.gz"
        queue_forecast = load_queue_forecast(config)
        queue_forecast_stats = (
            queue_forecast.stats if queue_forecast is not None else None
        )
        runtime_contract: RuntimeContract = validate_runtime_contract(
            config,
            area,
            net_path,
            queue_forecast,
        )
        runtime_contract_audit_path = write_runtime_contract_audit(
            config.results_dir,
            config.mode,
            runtime_contract,
        )
        print(
            "Runtime contract: "
            f"commit={runtime_contract.image_git_commit or runtime_contract.controller_git_commit} "
            f"tls={runtime_contract.controlled_tls_count} "
            f"network={runtime_contract.network_sha256[:12]} "
            f"zone={runtime_contract.zone_sha256[:12]} "
            f"model_tls={runtime_contract.covered_tls_count}/"
            f"{runtime_contract.controlled_tls_count} "
            f"model_lanes={runtime_contract.covered_lane_count}/"
            f"{runtime_contract.required_lane_count}",
            flush=True,
        )
        runtime_contract.raise_for_errors()
        configure_projection_data()
        start_sumo(_sumo_command(config))
        connection = traci.getConnection()
        if config.mode == SUMO_ACTUATED_MODE:
            detector_audit = audit_actuated_detector_startup(
                config,
                evaluation_area,
            )
            if (
                config.require_complete_actuated_detectors
                and not detector_audit["coverage_complete"]
            ):
                raise RuntimeError(
                    "SUMO Actuated detector startup validation failed: "
                    f"{detector_audit['missing_link_count']} controlled links "
                    "have no detector"
                )
        emergency_details = None
        emergency_manager = None
        corridor_manager = None
        corridor_observer = None
        router = None
        route_reassessment_done = (
            config.emergency is None
            or not config.allow_emergency_reroute
            or bool(config.fixed_emergency_route_edges)
        )
        emergency_route_changed = False
        alternatives_log = []
        emergency_route_tls: tuple[str, ...] = ()
        emergency_controlled_tls: tuple[str, ...] = ()
        emergency_rejected_tls: tuple[str, ...] = ()
        emergency_candidate_safety_audit: Path | None = None
        static_programs: tuple[StaticProgramActivation, ...] = ()
        active_tls_programs: tuple[ActiveTlsProgram, ...] = ()
        tls_program_audit_path: Path | None = None
        tls_safety_report: TlsSafetyReport | None = None
        tls_safety_audit_path: Path | None = None
        area_graph: AreaGraph | None = None
        telemetry_failures = 0
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
            if config.fixed_emergency_route_edges:
                best_route = router.route_option_from_edges(
                    config.fixed_emergency_route_edges
                )
                if best_route is None:
                    raise RuntimeError("Fixed emergency route is invalid for this map")
                alternatives_log = [
                    {
                        **best_route.as_dict(),
                        "reason": "Selected (Fixed Paired Route)",
                    }
                ]
            else:
                best_route, alternatives_log = router.find_alternatives(
                    config.emergency.start.edge_id,
                    config.emergency.destination.edge_id,
                    config.emergency.base_vehicle_type_id,
                    config.emergency.depart_time,
                    num_alternatives=5,
                )
            for alternative in alternatives_log:
                alternative["assessment"] = "initial"
            edges = best_route.edge_ids if best_route else None
            emergency_route_tls = best_route.tls_sequence if best_route else ()
            if emergency_route_tls:
                requested_tls = _merge_tls_ids(area.tls_ids, emergency_route_tls)
                candidate_area = discover_area(
                    net_path,
                    requested_tls=requested_tls,
                    strict_requested=False,
                )
                if config.mode in ADAPTIVE_CONTROL_MODES:
                    candidate_expectations = {
                        intersection.tls_id: ActivePlanExpectation(
                            program_id=intersection.program_id,
                            phase_states=intersection.phases,
                        )
                        for intersection in candidate_area.intersections
                    }
                    candidate_catalog = SumoTlsSafetyAdapter(
                        net_path,
                        connection,
                    ).load_catalog(candidate_area.tls_ids)
                    candidate_report = validate_tls_catalog(
                        candidate_catalog,
                        candidate_expectations,
                    )
                    emergency_candidate_safety_audit = (
                        write_tls_safety_startup_audit(
                            config.results_dir,
                            f"{config.mode}_emergency_candidates",
                            candidate_catalog,
                            candidate_report,
                        )
                    )
                    base_tls_ids = set(area.tls_ids)
                    unsafe_added = {
                        issue.tls_id
                        for issue in candidate_report.issues
                        if issue.severity == "error"
                        and issue.tls_id not in base_tls_ids
                    }
                    emergency_rejected_tls = tuple(sorted(unsafe_added))
                    area = AreaModel(
                        tuple(
                            intersection
                            for intersection in candidate_area.intersections
                            if intersection.tls_id not in unsafe_added
                        )
                    )
                    if emergency_rejected_tls:
                        session_events.append(
                            DecisionEvent(
                                time=0.0,
                                category="corridor",
                                title="Небезпечні TLS виключено з corridor control",
                                detail=(
                                    ", ".join(emergency_rejected_tls)
                                    + ": startup safety validation failed; "
                                    "вони залишаються під штатною програмою."
                                ),
                                level="warning",
                            )
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
                corridor_manager = CorridorManager(
                    config.emergency.vehicle_id,
                    prepare_tls_count=config.control.corridor_prepare_tls_count,
                )
                corridor_observer = SumoCorridorObservationAdapter(
                    connection,
                    config.emergency.vehicle_id,
                    config.control.corridor_pass_confirmation_distance,
                )

        if area.tls_ids != evaluation_area.tls_ids:
            # Emergency routing may safely expand the controlled area beyond
            # the configured 20-TLS zone. Re-run the model coverage contract
            # against the actual startup area before creating the controller.
            runtime_contract = validate_runtime_contract(
                config,
                area,
                net_path,
                queue_forecast,
            )
            runtime_contract_audit_path = write_runtime_contract_audit(
                config.results_dir,
                config.mode,
                runtime_contract,
            )
            print(
                "Runtime contract (emergency-expanded): "
                f"tls={runtime_contract.controlled_tls_count} "
                f"model_tls={runtime_contract.covered_tls_count}/"
                f"{runtime_contract.controlled_tls_count} "
                f"model_lanes={runtime_contract.covered_lane_count}/"
                f"{runtime_contract.required_lane_count}",
                flush=True,
            )
            runtime_contract.raise_for_errors()

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
        active_tls_programs = inspect_active_tls_programs(
            connection,
            area.tls_ids,
        )
        tls_program_audit_path = write_tls_program_startup_audit(
            config.results_dir,
            config.mode,
            active_tls_programs,
        )
        tls_safety_catalog = SumoTlsSafetyAdapter(
            net_path,
            connection,
        ).load_catalog(area.tls_ids)
        expected_active_plans = (
            {}
            if config.mode == STATIC_FIXED_MODE
            else {
                intersection.tls_id: ActivePlanExpectation(
                    program_id=intersection.program_id,
                    phase_states=intersection.phases,
                )
                for intersection in area.intersections
            }
        )
        tls_safety_report = validate_tls_catalog(
            tls_safety_catalog,
            expected_active_plans,
        )
        tls_safety_audit_path = write_tls_safety_startup_audit(
            config.results_dir,
            config.mode,
            tls_safety_catalog,
            tls_safety_report,
        )
        tls_safety_report.raise_for_errors()
        session_events.extend(
            DecisionEvent(
                time=0.0,
                category="system",
                title="Активну SUMO-програму перевірено",
                detail=(
                    f"{program.program_id}: {program.program_type_name} "
                    f"(type={program.program_type}), "
                    f"фаза {program.current_phase}/{program.phase_count - 1}."
                ),
                tls_id=program.tls_id,
            )
            for program in active_tls_programs
        )
        session_events.append(
            DecisionEvent(
                time=0.0,
                category="system",
                title="TLS safety-плани перевірено",
                detail=(
                    f"{tls_safety_report.plan_count} програм, "
                    f"{tls_safety_report.movement_count} рухів і "
                    f"{tls_safety_report.conflict_count} конфліктних пар; "
                    f"warnings: {tls_safety_report.warning_count}."
                ),
                level="success",
            )
        )
        evaluation_graph = SumoZoneGraphAdapter(
            net_path,
            config.control.sensor_range_meters,
        ).build_graph(
            zone_definition,
            evaluation_area,
        )
        zone_boundary = build_zone_boundary(evaluation_area, evaluation_graph)
        if not zone_boundary.valid:
            raise RuntimeError("Evaluation zone has no valid directed boundary")
        if config.mode in ADAPTIVE_CONTROL_MODES:
            area_graph = evaluation_graph
            session_events.append(
                DecisionEvent(
                    time=0.0,
                    category="system",
                    title="Зональний дорожній граф побудовано",
                    detail=(
                        f"{len(area_graph.segments)} directed segments, "
                        f"{len(area_graph.node_storage)} storage nodes і "
                        f"{len(area_graph.monitored_lane_ids)} monitored lanes."
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
                area_graph,
                demand_profile=config.demand_profile,
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
        publisher_error = (
            start_publisher_resilient(publisher)
            if config.enable_live_telemetry
            else None
        )
        if publisher_error is not None:
            telemetry_failures += 1
            session_events.append(
                DecisionEvent(
                    time=0.0,
                    category="system",
                    title="Live telemetry недоступна",
                    detail=publisher_error,
                    level="warning",
                )
            )
        metrics = MetricsCollector(
            connection,
            evaluation_area,
            config.control,
            priority_vehicle,
            zone_boundary,
        )
        metrics.set_live_publisher(
            publisher if config.enable_live_telemetry else None
        )
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
                    dataset_fingerprint=config.dataset_fingerprint,
                    demand_profile=config.demand_profile,
                    demand_scale=config.demand_scale,
                    emergency_active=config.dataset_emergency_active,
                ),
                area_graph,
            )

        disruption_lane_id = ""
        disruption_original_speed: float | None = None
        disruption_active = False
        disruption_restored = False
        if config.demand_profile in {"incident", "lane_closure"}:
            candidate_lanes = tuple(sorted(evaluation_area.outgoing_lanes))
            if candidate_lanes:
                disruption_lane_id = candidate_lanes[config.seed % len(candidate_lanes)]
                disruption_original_speed = float(
                    connection.lane.getMaxSpeed(disruption_lane_id)
                )

        simulated_time = 0.0
        while (
            connection.simulation.getMinExpectedNumber() > 0
            and simulated_time < config.duration
        ):
            connection.simulationStep()
            simulated_time = float(connection.simulation.getTime())
            if (
                disruption_lane_id
                and not disruption_active
                and simulated_time >= config.duration / 3.0
            ):
                connection.lane.setMaxSpeed(
                    disruption_lane_id,
                    0.5 if config.demand_profile == "lane_closure" else 3.0,
                )
                disruption_active = True
            if (
                disruption_active
                and not disruption_restored
                and simulated_time >= config.duration * 2.0 / 3.0
                and disruption_original_speed is not None
            ):
                connection.lane.setMaxSpeed(
                    disruption_lane_id,
                    disruption_original_speed,
                )
                disruption_restored = True
            
            if (
                not route_reassessment_done
                and config.allow_emergency_reroute
                and config.emergency is not None
                and router is not None
                and emergency_manager is not None
                and simulated_time
                >= max(
                    config.emergency.depart_time
                    - config.control.corridor_reroute_lead_seconds,
                    0.0,
                )
            ):
                route_reassessment_done = True
                in_network = (
                    config.emergency.vehicle_id in connection.vehicle.getIDList()
                )
                if not in_network:
                    reassessed_route, reassessment_log = router.find_alternatives(
                        config.emergency.start.edge_id,
                        config.emergency.destination.edge_id,
                        config.emergency.vehicle_type_id,
                        config.emergency.depart_time,
                        num_alternatives=5,
                        allowed_tls_ids=area.tls_ids,
                    )
                    for alternative in reassessment_log:
                        alternative["assessment"] = "predeparture"
                    alternatives_log.extend(reassessment_log)
                    if reassessed_route is not None:
                        try:
                            emergency_route_changed = (
                                emergency_manager.replace_scheduled_route(
                                    reassessed_route.edge_ids,
                                    route_length=reassessed_route.length,
                                    expected_travel_time=(
                                        reassessed_route.base_travel_time
                                    ),
                                    predicted_eta=reassessed_route.predicted_eta,
                                )
                            )
                        except Exception as error:
                            session_events.append(
                                DecisionEvent(
                                    time=round(simulated_time, 3),
                                    category="route",
                                    title="Не вдалося застосувати новий маршрут",
                                    detail=(
                                        f"{type(error).__name__}: {error}; "
                                        "залишено перевірений початковий маршрут."
                                    ),
                                    level="warning",
                                )
                            )
                        else:
                            emergency_details = emergency_manager.details
                            emergency_route_tls = reassessed_route.tls_sequence
                            emergency_controlled_tls = tuple(
                                tls_id
                                for tls_id in emergency_route_tls
                                if tls_id in area.tls_ids
                            )
                    session_events.append(
                        DecisionEvent(
                            time=round(simulated_time, 3),
                            category="route",
                            title=(
                                "Маршрут швидкої оновлено"
                                if emergency_route_changed
                                else "Маршрут швидкої повторно перевірено"
                            ),
                            detail=(
                                "Маршрут переоцінено за актуальними чергами "
                                "безпосередньо перед departure."
                            ),
                            level="success",
                        )
                    )

            if corridor_manager is not None and corridor_observer is not None:
                observation = corridor_observer.observe()
                corridor_manager.step(
                    simulated_time,
                    observation.vehicle_in_network,
                    list(observation.upcoming_tls),
                    observation.passed_tls_ids,
                )
                if emergency_manager is not None:
                    emergency_manager.update_corridor_visualization(
                        corridor_manager.state,
                        corridor_manager.active_tls,
                    )

            if controller is not None:
                controller.step(simulated_time)
            if corridor_manager is not None:
                metrics.set_corridor_state(corridor_manager.state)
            metrics.collect(simulated_time)
            if config.enable_live_telemetry and not write_live_status_resilient(
                metrics,
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
                    active_tls_programs=active_tls_programs,
                    tls_safety_report=tls_safety_report,
                    area_graph=area_graph,
                    running=True,
                ),
                decision_log=build_decision_log(
                    session_events,
                    controller,
                    corridor_manager,
                ),
            ):
                telemetry_failures += 1
            if dataset_collector is not None:
                dataset_collector.collect(simulated_time)

        summary = metrics.summary(config.mode, simulated_time)
        summary.update(static_program_summary(static_programs))
        summary.update(
            active_tls_program_summary(
                active_tls_programs,
                tls_program_audit_path,
            )
        )
        summary.update(area_graph_summary(area_graph))
        summary.update(
            tls_safety_summary(
                tls_safety_report,
                tls_safety_audit_path,
            )
        )
        if emergency_details is not None:
            summary.update(emergency_details.as_summary())
            summary["emergency_alternatives"] = alternatives_log
            summary["emergency_route_tls"] = emergency_route_tls
            summary["emergency_controlled_tls"] = emergency_controlled_tls
            summary["emergency_rejected_tls"] = emergency_rejected_tls
            summary["emergency_candidate_safety_audit"] = (
                str(emergency_candidate_safety_audit)
                if emergency_candidate_safety_audit is not None
                else ""
            )
            summary["emergency_route_reassessed"] = route_reassessment_done
            summary["emergency_route_changed"] = emergency_route_changed
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
                    "queue_forecast_shadow_mode": (
                        config.control.queue_forecast_shadow_mode
                    ),
                    "queue_forecast_shadow_predictions": (
                        controller.stats.queue_forecast_shadow_predictions
                    ),
                    "queue_forecast_control_predictions": (
                        controller.stats.queue_forecast_control_predictions
                    ),
                    "queue_forecast_ood_predictions": (
                        controller.stats.queue_forecast_ood_predictions
                    ),
                    "queue_forecast_shadow_evaluations": (
                        controller.stats.queue_forecast_shadow_evaluations
                    ),
                    "queue_forecast_shadow_mae": (
                        controller.stats.queue_forecast_shadow_mae
                    ),
                    "queue_forecast_rejection_reasons": (
                        controller.stats.queue_forecast_rejection_reasons
                    ),
                    "queue_forecast_trace_samples": len(
                        controller.stats.queue_forecast_samples
                    ),
                    "sensor_failures": controller.stats.sensor_failures,
                    "stale_lane_samples": controller.stats.stale_lane_samples,
                    "invalid_state_skips": controller.stats.invalid_state_skips,
                    "fallback_activations": controller.stats.fallback_activations,
                    "throughput_fallback_activations": (
                        controller.stats.throughput_fallback_activations
                    ),
                    "zone_coordination_candidates": (
                        controller.stats.zone_coordination_candidates
                    ),
                    "zone_coordination_overrides": (
                        controller.stats.zone_coordination_overrides
                    ),
                    "zone_coordination_gain_total": round(
                        controller.stats.zone_coordination_gain_total,
                        5,
                    ),
                    "zone_coordination_guarded_switches": (
                        controller.stats.zone_coordination_guarded_switches
                    ),
                    "zone_coordination_extra_holds": (
                        controller.stats.zone_coordination_extra_holds
                    ),
                    "movement_mask_updates": (
                        controller.stats.movement_mask_updates
                    ),
                    "movement_mask_active_decisions": (
                        controller.stats.movement_mask_active_decisions
                    ),
                    "movement_mask_program_updates": (
                        controller.stats.movement_mask_program_updates
                    ),
                    "movement_mask_failures": (
                        controller.stats.movement_mask_failures
                    ),
                    "safety_rejections": controller.stats.safety_rejections,
                    "safety_rejection_reasons": (
                        controller.stats.safety_rejection_reasons
                    ),
                    "corridor_preparation_targets": (
                        controller.stats.corridor_preparation_targets
                    ),
                    "corridor_downstream_blocks": (
                        controller.stats.corridor_downstream_blocks
                    ),
                    "corridor_recovery_actions": (
                        controller.stats.corridor_recovery_actions
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
                    "queue_forecast_shadow_mode": (
                        config.control.queue_forecast_shadow_mode
                    ),
                    "queue_forecast_shadow_predictions": 0,
                    "queue_forecast_control_predictions": 0,
                    "queue_forecast_ood_predictions": 0,
                    "queue_forecast_shadow_evaluations": 0,
                    "queue_forecast_shadow_mae": None,
                    "queue_forecast_rejection_reasons": {},
                    "queue_forecast_trace_samples": 0,
                    "sensor_failures": 0,
                    "stale_lane_samples": 0,
                    "invalid_state_skips": 0,
                    "fallback_activations": 0,
                    "throughput_fallback_activations": 0,
                    "zone_coordination_candidates": 0,
                    "zone_coordination_overrides": 0,
                    "zone_coordination_gain_total": 0.0,
                    "zone_coordination_guarded_switches": 0,
                    "zone_coordination_extra_holds": 0,
                    "movement_mask_updates": 0,
                    "movement_mask_active_decisions": 0,
                    "movement_mask_program_updates": 0,
                    "movement_mask_failures": 0,
                    "safety_rejections": 0,
                    "safety_rejection_reasons": {},
                    "corridor_preparation_targets": 0,
                    "corridor_downstream_blocks": 0,
                    "corridor_recovery_actions": 0,
                }
            )
        duration_value = float(summary.get("simulated_duration") or 0.0)
        outflow_value = summary.get("zone_outflow")
        if outflow_value is None:
            outflow_value = summary.get("throughput")
        outflow_number = (
            float(outflow_value) if outflow_value is not None else None
        )
        decision_count = int(summary.get("controller_decisions") or 0)
        phase_advances = int(summary.get("phase_advances") or 0)
        zone_candidates = int(summary.get("zone_coordination_candidates") or 0)
        zone_overrides = int(summary.get("zone_coordination_overrides") or 0)
        zone_gain_total = float(summary.get("zone_coordination_gain_total") or 0.0)
        summary.update(
            {
                "zone_throughput_per_minute": (
                    round(outflow_number * 60.0 / duration_value, 5)
                    if outflow_number is not None and duration_value > 0
                    else None
                ),
                "outflow_per_control_action": (
                    round(outflow_number / decision_count, 5)
                    if outflow_number is not None and decision_count > 0
                    else None
                ),
                "clearance_action_share": (
                    round(phase_advances / decision_count, 5)
                    if decision_count > 0
                    else None
                ),
                "zone_coordination_override_rate": (
                    round(zone_overrides / zone_candidates, 5)
                    if zone_candidates > 0
                    else None
                ),
                "zone_coordination_mean_gain": (
                    round(zone_gain_total / zone_overrides, 5)
                    if zone_overrides > 0
                    else None
                ),
            }
        )
        summary["telemetry_failures"] = telemetry_failures
        summary.update(
            {
                "demand_profile": config.demand_profile,
                "demand_scale": config.demand_scale,
                "dataset_emergency_active": config.dataset_emergency_active,
                "disruption_lane_id": disruption_lane_id,
                "disruption_applied": disruption_active,
                "disruption_restored": disruption_restored,
            }
        )
        summary.update(queue_forecast_summary(queue_forecast_stats))
        summary.update(
            {
                "runtime_contract_valid": runtime_contract.valid,
                "runtime_contract_warnings": list(runtime_contract.warnings),
                "runtime_contract_startup_audit": str(
                    runtime_contract_audit_path
                ),
            }
        )
        actual_emergency_route = (
            emergency_details.route_edges if emergency_details is not None else ()
        )
        summary.update(
            run_provenance_summary(
                config,
                net_path,
                queue_forecast_stats,
                actual_emergency_route,
            )
        )
        if dataset_collector is not None:
            summary.update(dataset_collector.write())
        waiting_value = summary.get("average_waiting_time")
        waiting_label = (
            f"{float(waiting_value):.1f} с"
            if waiting_value is not None
            else "немає даних"
        )
        session_events.append(
            DecisionEvent(
                time=round(simulated_time, 3),
                category="system",
                title="Симуляцію завершено",
                detail=(
                    f"Завершили маршрут {summary.get('throughput', 0)} авто; "
                    f"середнє очікування {waiting_label}."
                ),
                level="success",
            )
        )
        if config.enable_live_telemetry and not write_live_status_resilient(
            metrics,
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
                active_tls_programs=active_tls_programs,
                tls_safety_report=tls_safety_report,
                area_graph=area_graph,
                running=False,
            ),
            decision_log=build_decision_log(
                session_events,
                controller,
                corridor_manager,
            ),
        ):
            telemetry_failures += 1
            summary["telemetry_failures"] = telemetry_failures
        metrics.write(config.results_dir, summary)
        return summary
    except Exception as error:
        if config.enable_live_telemetry:
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
    if status == "starting":
        system["tls_programs"] = {
            "status": "waiting",
            "count": 0,
            "items": [],
        }
        system["tls_safety"] = {
            "status": "waiting",
            "plans": 0,
            "movements": 0,
            "conflicts": 0,
            "errors": 0,
            "warnings": 0,
        }
        system["area_graph"] = {
            "status": "waiting",
            "segments": 0,
            "storage_nodes": 0,
            "monitored_lanes": 0,
        }
    else:
        system.setdefault(
            "tls_programs",
            {"status": "waiting", "count": 0, "items": []},
        )
        system.setdefault(
            "tls_safety",
            {
                "status": "waiting",
                "plans": 0,
                "movements": 0,
                "conflicts": 0,
                "errors": 0,
                "warnings": 0,
            },
        )
        system.setdefault(
            "area_graph",
            {
                "status": "waiting",
                "segments": 0,
                "storage_nodes": 0,
                "monitored_lanes": 0,
            },
        )
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
    active_tls_programs: tuple[ActiveTlsProgram, ...] = (),
    tls_safety_report: TlsSafetyReport | None = None,
    area_graph: AreaGraph | None = None,
) -> dict[str, object]:
    controller_stats = controller.stats if controller is not None else None
    corridor_summary = (
        corridor_manager.as_summary()
        if corridor_manager is not None
        else {
            "corridor_state": "DISABLED",
            "corridor_active_tls": None,
            "corridor_prepared_tls": (),
            "corridor_completed_tls": (),
            "corridor_unconfirmed_tls": (),
            "corridor_affected_tls": (),
            "corridor_recovery_tls": (),
            "corridor_restored_tls": (),
            "corridor_downstream_blocks": 0,
            "corridor_downstream_block_reasons": {},
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
            "sensor_failures": (
                controller_stats.sensor_failures if controller_stats else 0
            ),
            "stale_lane_samples": (
                controller_stats.stale_lane_samples if controller_stats else 0
            ),
            "fallback_activations": (
                controller_stats.fallback_activations if controller_stats else 0
            ),
            "throughput_fallback_activations": (
                controller_stats.throughput_fallback_activations
                if controller_stats
                else 0
            ),
            "zone_coordination_candidates": (
                controller_stats.zone_coordination_candidates
                if controller_stats
                else 0
            ),
            "zone_coordination_overrides": (
                controller_stats.zone_coordination_overrides
                if controller_stats
                else 0
            ),
            "zone_coordination_gain_total": (
                round(controller_stats.zone_coordination_gain_total, 5)
                if controller_stats
                else 0.0
            ),
            "zone_coordination_guarded_switches": (
                controller_stats.zone_coordination_guarded_switches
                if controller_stats
                else 0
            ),
            "zone_coordination_extra_holds": (
                controller_stats.zone_coordination_extra_holds
                if controller_stats
                else 0
            ),
            "corridor_preparation_targets": (
                controller_stats.corridor_preparation_targets
                if controller_stats
                else 0
            ),
            "corridor_downstream_blocks": (
                controller_stats.corridor_downstream_blocks
                if controller_stats
                else 0
            ),
            "corridor_recovery_actions": (
                controller_stats.corridor_recovery_actions
                if controller_stats
                else 0
            ),
        },
        "tls_programs": {
            "status": "audited" if active_tls_programs else "waiting",
            "count": len(active_tls_programs),
            "items": [
                program.as_payload() for program in active_tls_programs
            ],
        },
        "tls_safety": {
            "status": (
                "valid"
                if tls_safety_report is not None and tls_safety_report.valid
                else "waiting"
            ),
            "plans": tls_safety_report.plan_count if tls_safety_report else 0,
            "movements": (
                tls_safety_report.movement_count if tls_safety_report else 0
            ),
            "conflicts": (
                tls_safety_report.conflict_count if tls_safety_report else 0
            ),
            "errors": tls_safety_report.error_count if tls_safety_report else 0,
            "warnings": (
                tls_safety_report.warning_count if tls_safety_report else 0
            ),
        },
        "area_graph": {
            "status": "active" if area_graph is not None else "disabled",
            "segments": len(area_graph.segments) if area_graph is not None else 0,
            "storage_nodes": (
                len(area_graph.node_storage) if area_graph is not None else 0
            ),
            "monitored_lanes": (
                len(area_graph.monitored_lane_ids) if area_graph is not None else 0
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
            "shadow_mode": config.control.queue_forecast_shadow_mode,
            "shadow_predictions": (
                controller_stats.queue_forecast_shadow_predictions
                if controller_stats
                else 0
            ),
            "control_predictions": (
                controller_stats.queue_forecast_control_predictions
                if controller_stats
                else 0
            ),
            "ood_predictions": (
                controller_stats.queue_forecast_ood_predictions
                if controller_stats
                else 0
            ),
            "shadow_evaluations": (
                controller_stats.queue_forecast_shadow_evaluations
                if controller_stats
                else 0
            ),
            "shadow_mae": (
                controller_stats.queue_forecast_shadow_mae
                if controller_stats
                else None
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


def start_publisher_resilient(publisher: LiveTelemetryPublisher) -> str | None:
    try:
        publisher.start()
    except Exception as error:
        return str(error) or type(error).__name__
    return None


def write_live_status_resilient(
    metrics: MetricsCollector,
    *args: object,
    **kwargs: object,
) -> bool:
    try:
        metrics.write_live_status(*args, **kwargs)
    except Exception:
        return False
    return True


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
            "queue_forecast_contract": "",
            "queue_forecast_artifact_sha256": "",
            "queue_forecast_dataset_sha256": "",
            "queue_forecast_network_sha256": "",
            "queue_forecast_feature_schema_sha256": "",
            "queue_forecast_zone_sha256": "",
            "queue_forecast_dataset_fingerprint": "",
            "queue_forecast_known_tls_count": 0,
            "queue_forecast_known_lane_count": 0,
        }
    return {
        "queue_forecast_model": stats.model_path,
        "queue_forecast_target": stats.target,
        "queue_forecast_feature_count": stats.feature_count,
        "queue_forecast_model_count": stats.model_count,
        "queue_forecast_horizons": stats.horizons,
        "queue_forecast_horizon_weights": stats.horizon_weights,
        "queue_forecast_contract": stats.forecast_contract,
        "queue_forecast_artifact_sha256": stats.artifact_sha256,
        "queue_forecast_dataset_sha256": stats.dataset_sha256,
        "queue_forecast_network_sha256": stats.network_sha256,
        "queue_forecast_feature_schema_sha256": stats.feature_schema_sha256,
        "queue_forecast_zone_sha256": stats.zone_sha256,
        "queue_forecast_dataset_fingerprint": stats.dataset_fingerprint,
        "queue_forecast_known_tls_count": stats.known_tls_count,
        "queue_forecast_known_lane_count": stats.known_lane_count,
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


def active_tls_program_summary(
    programs: tuple[ActiveTlsProgram, ...],
    audit_path: Path | None,
) -> dict[str, object]:
    return {
        "active_tls_program_count": len(programs),
        "active_tls_programs": [program.as_payload() for program in programs],
        "active_tls_program_ids": ",".join(
            f"{program.tls_id}:{program.program_id}" for program in programs
        ),
        "active_tls_program_types": ",".join(
            f"{program.tls_id}:{program.program_type_name}"
            for program in programs
        ),
        "tls_program_startup_audit": str(audit_path) if audit_path else "",
    }


def tls_safety_summary(
    report: TlsSafetyReport | None,
    audit_path: Path | None,
) -> dict[str, object]:
    return {
        "tls_safety_valid": report.valid if report is not None else False,
        "tls_safety_plan_count": report.plan_count if report is not None else 0,
        "tls_safety_movement_count": (
            report.movement_count if report is not None else 0
        ),
        "tls_safety_conflict_count": (
            report.conflict_count if report is not None else 0
        ),
        "tls_safety_warning_count": (
            report.warning_count if report is not None else 0
        ),
        "tls_safety_startup_audit": str(audit_path) if audit_path else "",
    }


def area_graph_summary(graph: AreaGraph | None) -> dict[str, object]:
    return {
        "area_graph_enabled": graph is not None,
        "area_graph_segment_count": len(graph.segments) if graph else 0,
        "area_graph_storage_node_count": len(graph.node_storage) if graph else 0,
        "area_graph_monitored_lane_count": (
            len(graph.monitored_lane_ids) if graph else 0
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
