from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sumolib
import traci

from flowmind.config import PROJECT_ROOT, ControlConfig, RunConfig
from flowmind.counterfactual_dataset import (
    COUNTERFACTUAL_DATASET_SCHEMA_VERSION,
    counterfactual_dataset_schema_sha256,
)
from flowmind.experiment import configure_projection_data, load_area, start_sumo
from flowmind.queue_forecast import QueueForecastModel, file_sha256, movement_id_for
from flowmind.sumo_zone_graph_adapter import SumoZoneGraphAdapter
from flowmind.traffic_state import TrafficState, TrafficStateReader
from flowmind.zone_graph import (
    AreaDecisionSnapshot,
    build_area_decision_snapshot,
    load_zone_definition,
)


FORECAST_CONTRACT = "counterfactual"
SCENARIO_NAME = "rivne_counterfactual"
STAT_KEYS = (
    "stats.vehicles.inserted",
    "stats.teleports.total",
    "stats.safety.collisions",
    "stats.safety.emergencyBraking",
)


@dataclass(frozen=True)
class PlannedRun:
    number: int
    seed: int
    demand_profile: str
    demand_scale: float

    @property
    def run_id(self) -> str:
        return (
            f"{SCENARIO_NAME}_flowmind_{self.seed}_"
            f"{self.demand_profile}_{self.number:03d}"
        )


class HistoryContext:
    def __init__(self) -> None:
        self._lane_history: dict[str, list[tuple[float, int, int]]] = {}

    def features(
        self,
        traffic: TrafficState,
        simulation_time: float,
    ) -> dict[str, dict[str, float]]:
        result: dict[str, dict[str, float]] = {}
        for lane_id, lane in traffic.lanes.items():
            values: dict[str, float] = {}
            history = self._lane_history.get(lane_id, ())
            for window in (15, 30):
                previous = next(
                    (
                        item
                        for item in reversed(history)
                        if item[0] <= simulation_time - window + 1e-9
                    ),
                    None,
                )
                previous_queue = previous[1] if previous is not None else lane.queue
                previous_count = (
                    previous[2] if previous is not None else lane.vehicle_count
                )
                elapsed = max(
                    simulation_time - previous[0]
                    if previous is not None
                    else float(window),
                    1.0,
                )
                delta = lane.vehicle_count - previous_count
                values[f"incoming_queue_growth_{window}s"] = float(
                    lane.queue - previous_queue
                )
                values[f"arrival_rate_{window}s"] = max(delta, 0) / elapsed
                values[f"discharge_rate_{window}s"] = max(-delta, 0) / elapsed
            result[lane_id] = values
        return result

    def remember(self, traffic: TrafficState, simulation_time: float) -> None:
        for lane_id, lane in traffic.lanes.items():
            history = self._lane_history.setdefault(lane_id, [])
            history.append((simulation_time, lane.queue, lane.vehicle_count))
            while len(history) > 1 and history[1][0] < simulation_time - 35.0:
                history.pop(0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Collect real alternative-action outcomes by reloading deterministic "
            "SUMO snapshots and branching every safe green phase."
        )
    )
    parser.add_argument("--runs", type=int, default=12)
    parser.add_argument("--seed-start", type=int, default=3_000_001)
    parser.add_argument("--duration", type=int, default=1600)
    parser.add_argument("--warmup", type=int, default=240)
    parser.add_argument("--snapshots-per-run", type=int, default=10)
    parser.add_argument("--snapshot-interval", type=int, default=120)
    parser.add_argument("--tls-per-snapshot", type=int, default=4)
    parser.add_argument("--horizons", type=int, nargs="+", default=(30, 60, 90))
    parser.add_argument("--yellow-seconds", type=int, default=3)
    parser.add_argument("--all-red-seconds", type=int, default=2)
    parser.add_argument("--action-green-seconds", type=int, default=10)
    parser.add_argument(
        "--config",
        type=Path,
        default=PROJECT_ROOT / "simulation" / "rivne_area" / "focused.sumocfg",
    )
    parser.add_argument(
        "--zone",
        type=Path,
        default=PROJECT_ROOT / "simulation" / "rivne_area" / "central_zone.json",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "results" / "counterfactual_dataset",
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--collect-only",
        action="store_true",
        help=(
            "Write partial artifacts without failing the final "
            "training-readiness gate."
        ),
    )
    parser.add_argument("--max-teleport-rate", type=float, default=0.05)
    parser.add_argument("--max-collisions", type=int, default=0)
    parser.add_argument(
        "--max-emergency-brakes-per-1000", type=float, default=20.0
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    validate_args(args)
    configure_projection_data()

    output_dir = args.output_dir.resolve()
    samples_dir = output_dir / "samples"
    states_dir = output_dir / "states"
    samples_dir.mkdir(parents=True, exist_ok=True)
    states_dir.mkdir(parents=True, exist_ok=True)
    runs = planned_runs(args)
    fingerprint = dataset_fingerprint(args, runs)
    write_plan(output_dir / "dataset_plan.json", args, runs, fingerprint)

    audit_rows: list[dict[str, Any]] = load_existing_audit(output_dir, fingerprint)
    completed_names = {
        Path(str(item.get("sample_file", ""))).name
        for item in audit_rows
        if item.get("status") == "ok"
    }
    try:
        for run in runs:
            sample_path = samples_dir / f"{run.run_id}.csv"
            if (
                args.resume
                and sample_path.name in completed_names
                and sample_path.exists()
            ):
                print(f"[{run.number}/{len(runs)}] skip {run.run_id}", flush=True)
                continue
            print(f"[{run.number}/{len(runs)}] collect {run.run_id}", flush=True)
            audit = collect_run(args, run, fingerprint, sample_path, states_dir)
            audit_rows = [
                item
                for item in audit_rows
                if Path(str(item.get("sample_file", ""))).name != sample_path.name
            ]
            audit_rows.append(audit)
            write_quality_report(output_dir, args, runs, fingerprint, audit_rows)
            print(
                f"[{run.number}/{len(runs)}] rows={audit['row_count']} "
                f"branches={audit['branch_count']} accepted={audit['accepted']}",
                flush=True,
            )
    finally:
        for path in states_dir.glob("*.xml.gz"):
            path.unlink(missing_ok=True)

    report = write_quality_report(output_dir, args, runs, fingerprint, audit_rows)
    if report["status"] != "completed" and not args.collect_only:
        raise SystemExit(
            "Counterfactual dataset is not trainable: "
            + "; ".join(str(value) for value in report["blocking_reasons"])
        )
    print(f"Counterfactual dataset ready: {output_dir}", flush=True)


def validate_args(args: argparse.Namespace) -> None:
    positive = {
        "runs": args.runs,
        "duration": args.duration,
        "warmup": args.warmup,
        "snapshots-per-run": args.snapshots_per_run,
        "snapshot-interval": args.snapshot_interval,
        "tls-per-snapshot": args.tls_per_snapshot,
        "yellow-seconds": args.yellow_seconds,
        "all-red-seconds": args.all_red_seconds,
        "action-green-seconds": args.action_green_seconds,
    }
    if any(int(value) <= 0 for value in positive.values()):
        raise ValueError("All run/interval/transition values must be positive")
    horizons = tuple(sorted(set(int(value) for value in args.horizons)))
    if not horizons or any(value <= 0 for value in horizons):
        raise ValueError("--horizons must contain positive values")
    last_snapshot = args.warmup + (args.snapshots_per_run - 1) * args.snapshot_interval
    if last_snapshot + max(horizons) >= args.duration:
        raise ValueError("Duration must extend beyond the final snapshot and horizon")
    if not 0.0 <= args.max_teleport_rate <= 1.0:
        raise ValueError("--max-teleport-rate must be between 0 and 1")


def planned_runs(args: argparse.Namespace) -> list[PlannedRun]:
    profiles = (("normal", 1.0), ("off_peak", 0.8), ("oversaturated", 1.2))
    return [
        PlannedRun(
            number=index + 1,
            seed=args.seed_start + index,
            demand_profile=profiles[index % len(profiles)][0],
            demand_scale=profiles[index % len(profiles)][1],
        )
        for index in range(args.runs)
    ]


def dataset_fingerprint(
    args: argparse.Namespace,
    runs: list[PlannedRun],
) -> str:
    payload = {
        "schema_version": COUNTERFACTUAL_DATASET_SCHEMA_VERSION,
        "schema_sha256": counterfactual_dataset_schema_sha256(),
        "config_sha256": file_sha256(args.config),
        "zone_sha256": file_sha256(args.zone),
        "runs": [run.__dict__ for run in runs],
        "duration": args.duration,
        "warmup": args.warmup,
        "snapshots_per_run": args.snapshots_per_run,
        "snapshot_interval": args.snapshot_interval,
        "tls_per_snapshot": args.tls_per_snapshot,
        "horizons": sorted(set(args.horizons)),
        "yellow_seconds": args.yellow_seconds,
        "all_red_seconds": args.all_red_seconds,
        "action_green_seconds": args.action_green_seconds,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def write_plan(
    path: Path,
    args: argparse.Namespace,
    runs: list[PlannedRun],
    fingerprint: str,
) -> None:
    payload = {
        "status": "planned",
        "forecast_contract": FORECAST_CONTRACT,
        "dataset_fingerprint": fingerprint,
        "dataset_schema_version": COUNTERFACTUAL_DATASET_SCHEMA_VERSION,
        "dataset_schema_sha256": counterfactual_dataset_schema_sha256(),
        "config": str(args.config.resolve()),
        "zone": str(args.zone.resolve()),
        "horizons": sorted(set(args.horizons)),
        "runs": [run.__dict__ | {"run_id": run.run_id} for run in runs],
    }
    write_json_atomic(path, payload)


def collect_run(
    args: argparse.Namespace,
    run: PlannedRun,
    fingerprint: str,
    sample_path: Path,
    states_dir: Path,
) -> dict[str, Any]:
    control = ControlConfig()
    config = RunConfig(
        mode="sumo_actuated",
        duration=args.duration,
        seed=run.seed,
        config_path=args.config,
        zone_path=args.zone,
        results_dir=args.output_dir / "raw" / run.run_id,
        control=control,
        queue_model_paths=(),
        demand_profile=run.demand_profile,
        demand_scale=run.demand_scale,
        enable_live_telemetry=False,
    )
    area = load_area(config)
    zone = load_zone_definition(args.zone)
    network_path = args.config.resolve().parent / "osm.net.xml.gz"
    graph = SumoZoneGraphAdapter(
        network_path,
        control.sensor_range_meters,
    ).build_graph(zone, area)
    reader: TrafficStateReader | None = None
    connection: Any = None
    rows: list[dict[str, Any]] = []
    branch_records: list[dict[str, Any]] = []
    history = HistoryContext()
    base_args = sumo_args(args, run)
    try:
        start_sumo([sumolib.checkBinary("sumo"), *base_args])
        connection = traci.getConnection()
        reader = TrafficStateReader(
            connection,
            area,
            control.sensor_range_meters,
            graph.monitored_lane_ids,
        )
        snapshot_times = [
            args.warmup + index * args.snapshot_interval
            for index in range(args.snapshots_per_run)
        ]
        for snapshot_index, snapshot_time in enumerate(snapshot_times):
            advance_to(connection, snapshot_time, reader, history)
            actual_time = float(connection.simulation.getTime())
            base_traffic = reader.read(actual_time)
            history_context = history.features(base_traffic, actual_time)
            history.remember(base_traffic, actual_time)
            decision_snapshot = build_area_decision_snapshot(
                area,
                graph,
                base_traffic,
                actual_time,
                control,
            )
            area_context = area_model_context(decision_snapshot)
            state_path = states_dir / f"{run.run_id}_{snapshot_index:03d}.xml.gz"
            connection.simulation.saveState(str(state_path))
            selected = selected_intersections(
                area.intersections,
                snapshot_index,
                args.tls_per_snapshot,
            )
            for intersection in selected:
                current_phase = safe_int(
                    connection.trafficlight.getPhase,
                    intersection.tls_id,
                    default=-1,
                )
                current_state = str(
                    connection.trafficlight.getRedYellowGreenState(
                        intersection.tls_id
                    )
                )
                phase_elapsed = safe_float(
                    connection.trafficlight.getSpentDuration,
                    intersection.tls_id,
                    default=0.0,
                )
                original_program = str(
                    connection.trafficlight.getProgram(intersection.tls_id)
                )
                for candidate_phase in intersection.green_phase_indices:
                    reload_snapshot(
                        connection,
                        base_args,
                        state_path,
                        actual_time,
                    )
                    feature_rows = candidate_feature_rows(
                        run,
                        args,
                        fingerprint,
                        intersection,
                        candidate_phase,
                        current_phase,
                        current_state,
                        phase_elapsed,
                        base_traffic,
                        control,
                        history_context,
                        area_context,
                        snapshot_index,
                    )
                    branch = run_branch(
                        connection,
                        reader,
                        intersection,
                        candidate_phase,
                        current_phase,
                        current_state,
                        original_program,
                        actual_time,
                        tuple(sorted(set(args.horizons))),
                        args.yellow_seconds,
                        args.all_red_seconds,
                        args.action_green_seconds,
                    )
                    enrich_targets(feature_rows, branch["future_states"], args.horizons)
                    for row in feature_rows:
                        row.update(
                            {
                                "branch_teleports": branch["teleports"],
                                "branch_collisions": branch["collisions"],
                                "branch_emergency_brakes": branch[
                                    "emergency_brakes"
                                ],
                            }
                        )
                    rows.extend(feature_rows)
                    branch_records.append(
                        {
                            key: value
                            for key, value in branch.items()
                            if key != "future_states"
                        }
                        | {
                            "tls_id": intersection.tls_id,
                            "candidate_phase": candidate_phase,
                            "snapshot_index": snapshot_index,
                            "row_count": len(feature_rows),
                        }
                    )
            reload_snapshot(connection, base_args, state_path, actual_time)
            state_path.unlink(missing_ok=True)
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass

    write_csv_atomic(sample_path, rows)
    teleports = sum(int(item["teleports"]) for item in branch_records)
    collisions = sum(int(item["collisions"]) for item in branch_records)
    emergency_brakes = sum(
        int(item["emergency_brakes"]) for item in branch_records
    )
    inserted = sum(int(item["inserted"]) for item in branch_records)
    exposure = max(inserted, len(branch_records))
    teleport_rate = teleports / exposure
    emergency_brakes_per_1000 = emergency_brakes * 1000.0 / exposure
    accepted = (
        bool(rows)
        and collisions <= args.max_collisions
        and teleport_rate <= args.max_teleport_rate
        and emergency_brakes_per_1000 <= args.max_emergency_brakes_per_1000
    )
    return {
        "status": "ok",
        "run_id": run.run_id,
        "seed": run.seed,
        "sample_file": str(sample_path),
        "row_count": len(rows),
        "branch_count": len(branch_records),
        "tls_ids": sorted({str(item["tls_id"]) for item in branch_records}),
        "actions": sorted(
            {
                f"{item['tls_id']}:{item['candidate_phase']}"
                for item in branch_records
            }
        ),
        "inserted_vehicle_exposure": inserted,
        "teleports": teleports,
        "teleport_rate": round(teleport_rate, 8),
        "collisions": collisions,
        "emergency_brakes": emergency_brakes,
        "emergency_brakes_per_1000": round(emergency_brakes_per_1000, 5),
        "accepted": accepted,
        "branches": branch_records,
    }


def sumo_args(args: argparse.Namespace, run: PlannedRun) -> list[str]:
    return [
        "-c",
        str(args.config.resolve()),
        "--seed",
        str(run.seed),
        "--scale",
        str(run.demand_scale),
        "--end",
        str(args.duration),
        "--no-step-log",
        "true",
        "--duration-log.disable",
        "true",
        "--ignore-route-errors",
        "true",
        "--quit-on-end",
        "true",
        "--save-state.rng",
        "true",
        "--collision.action",
        "warn",
        "--collision.check-junctions",
        "true",
    ]


def reload_snapshot(
    connection: Any,
    base_args: list[str],
    state_path: Path,
    snapshot_time: float,
) -> None:
    connection.load(
        [
            *base_args,
            "--load-state",
            str(state_path),
            "--begin",
            str(snapshot_time),
        ]
    )


def advance_to(
    connection: Any,
    target_time: float,
    reader: TrafficStateReader | None = None,
    history: HistoryContext | None = None,
) -> None:
    next_history_at = float(connection.simulation.getTime())
    while float(connection.simulation.getTime()) + 1e-9 < target_time:
        connection.simulationStep()
        now = float(connection.simulation.getTime())
        if reader is not None and history is not None and now + 1e-9 >= next_history_at:
            traffic = reader.read(now)
            history.remember(traffic, now)
            next_history_at = now + 5.0


def selected_intersections(
    intersections: tuple[Any, ...],
    snapshot_index: int,
    count: int,
) -> tuple[Any, ...]:
    if not intersections:
        return ()
    start = (snapshot_index * count) % len(intersections)
    return tuple(
        intersections[(start + offset) % len(intersections)]
        for offset in range(min(count, len(intersections)))
    )


def candidate_feature_rows(
    run: PlannedRun,
    args: argparse.Namespace,
    fingerprint: str,
    intersection: Any,
    candidate_phase: int,
    current_phase: int,
    current_state: str,
    phase_elapsed: float,
    traffic: TrafficState,
    control: ControlConfig,
    history_context: dict[str, dict[str, float]],
    area_context: dict[str, dict[str, float]],
    snapshot_index: int,
) -> list[dict[str, Any]]:
    candidate_state = intersection.phases[candidate_phase]
    rows: list[dict[str, Any]] = []
    for link in intersection.links:
        if link.signal_index >= len(candidate_state):
            continue
        signal_state = candidate_state[link.signal_index]
        if signal_state not in "Gg":
            continue
        row = QueueForecastModel._feature_row(
            mode="flowmind",
            simulation_time=traffic.sample_time,
            intersection=intersection,
            link=link,
            signal_state=signal_state,
            current_phase=current_phase,
            phase_state=current_state,
            phase_elapsed=phase_elapsed,
            candidate_phase=candidate_phase,
            state=traffic,
            control=control,
            sample_interval=5,
            demand_profile=run.demand_profile,
            context_by_lane=history_context,
            area_context_by_lane=area_context,
        )
        row.update(
            {
                "dataset_schema_version": COUNTERFACTUAL_DATASET_SCHEMA_VERSION,
                "dataset_schema_sha256": counterfactual_dataset_schema_sha256(),
                "dataset_fingerprint": fingerprint,
                "forecast_contract": FORECAST_CONTRACT,
                "run_id": run.run_id,
                "scenario": SCENARIO_NAME,
                "seed": run.seed,
                "duration": args.duration,
                "demand_scale": run.demand_scale,
                "emergency_active": 0,
                "snapshot_index": snapshot_index,
                "branch_tls_id": intersection.tls_id,
                "branch_action_phase": candidate_phase,
                "branch_yellow_seconds": args.yellow_seconds,
                "branch_all_red_seconds": args.all_red_seconds,
                "branch_action_green_seconds": args.action_green_seconds,
            }
        )
        rows.append(row)
    return rows


def run_branch(
    connection: Any,
    reader: TrafficStateReader,
    intersection: Any,
    candidate_phase: int,
    current_phase: int,
    current_state: str,
    original_program: str,
    start_time: float,
    horizons: tuple[int, ...],
    yellow_seconds: int,
    all_red_seconds: int,
    action_green_seconds: int,
) -> dict[str, Any]:
    before = simulation_stats(connection)
    tls_id = intersection.tls_id
    candidate_state = intersection.phases[candidate_phase]
    switching = candidate_phase != current_phase
    if switching:
        yellow_state = "".join(
            "y" if signal in "Gg" else "r" for signal in current_state
        )
        connection.trafficlight.setRedYellowGreenState(tls_id, yellow_state)
        advance_to(connection, start_time + yellow_seconds)
        connection.trafficlight.setRedYellowGreenState(
            tls_id, "r" * len(candidate_state)
        )
        advance_to(connection, start_time + yellow_seconds + all_red_seconds)
    connection.trafficlight.setRedYellowGreenState(tls_id, candidate_state)
    green_start = float(connection.simulation.getTime())
    advance_to(connection, green_start + action_green_seconds)
    connection.trafficlight.setProgram(tls_id, original_program)
    connection.trafficlight.setPhase(tls_id, candidate_phase)
    connection.trafficlight.setPhaseDuration(tls_id, 1.0)

    future_states: dict[int, TrafficState] = {}
    for horizon in horizons:
        advance_to(connection, start_time + horizon)
        future_states[horizon] = reader.read(float(connection.simulation.getTime()))
    after = simulation_stats(connection)
    deltas = {key: max(after[key] - before[key], 0) for key in STAT_KEYS}
    return {
        "switching": switching,
        "inserted": deltas["stats.vehicles.inserted"],
        "teleports": deltas["stats.teleports.total"],
        "collisions": deltas["stats.safety.collisions"],
        "emergency_brakes": deltas["stats.safety.emergencyBraking"],
        "future_states": future_states,
    }


def enrich_targets(
    rows: list[dict[str, Any]],
    future_states: dict[int, TrafficState],
    horizons: list[int] | tuple[int, ...],
) -> None:
    for row in rows:
        for horizon in sorted(set(horizons)):
            future = future_states[horizon]
            incoming = future.lane(str(row["incoming_lane"]))
            outgoing = future.lane(str(row["outgoing_lane"]))
            suffix = f"{horizon}s"
            initial_queue = int(row["incoming_queue"])
            initial_count = int(row["incoming_vehicle_count"])
            row[f"target_incoming_queue_{suffix}"] = incoming.queue
            row[f"target_incoming_occupancy_{suffix}"] = round(
                incoming.occupancy, 5
            )
            row[f"target_outgoing_occupancy_{suffix}"] = round(
                outgoing.occupancy, 5
            )
            row[f"target_downstream_blocked_{suffix}"] = int(
                outgoing.occupancy >= float(row["blocked_occupancy"])
            )
            row[f"target_delta_queue_{suffix}"] = incoming.queue - initial_queue
            row[f"target_queue_reduction_{suffix}"] = initial_queue - incoming.queue
            row[f"target_future_waiting_{suffix}"] = incoming.queue
            row[f"target_discharged_vehicles_{suffix}"] = max(
                initial_count - incoming.vehicle_count,
                0,
            )


def simulation_stats(connection: Any) -> dict[str, int]:
    return {
        key: safe_int(connection.simulation.getParameter, "", key, default=0)
        for key in STAT_KEYS
    }


def area_model_context(
    snapshot: AreaDecisionSnapshot,
) -> dict[str, dict[str, float]]:
    lane_ids = (
        set(snapshot.platoon_arrival_by_incoming_lane)
        | set(snapshot.downstream_storage_by_outgoing_lane)
        | set(snapshot.upstream_queue_by_incoming_lane)
        | set(snapshot.downstream_occupancy_by_outgoing_lane)
    )
    return {
        lane_id: {
            "platoon_arrival_30s": snapshot.platoon_arrival_by_incoming_lane.get(
                lane_id, 0.0
            ),
            "downstream_storage_slots": (
                snapshot.downstream_storage_by_outgoing_lane.get(lane_id, 0.0)
            ),
            "upstream_neighbour_queue": snapshot.upstream_queue_by_incoming_lane.get(
                lane_id, 0.0
            ),
            "downstream_neighbour_occupancy": (
                snapshot.downstream_occupancy_by_outgoing_lane.get(lane_id, 0.0)
            ),
        }
        for lane_id in lane_ids
    }


def load_existing_audit(output_dir: Path, fingerprint: str) -> list[dict[str, Any]]:
    path = output_dir / "quality_report.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if payload.get("dataset_fingerprint") != fingerprint:
        raise SystemExit("Existing counterfactual dataset fingerprint does not match")
    runs = payload.get("runs", [])
    return [item for item in runs if isinstance(item, dict)]


def write_quality_report(
    output_dir: Path,
    args: argparse.Namespace,
    planned: list[PlannedRun],
    fingerprint: str,
    audit_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    successful = [item for item in audit_rows if item.get("status") == "ok"]
    completed_ids = {str(item.get("run_id", "")) for item in successful}
    missing = [run.run_id for run in planned if run.run_id not in completed_ids]
    accepted = [item for item in successful if item.get("accepted") is True]
    area = load_area(
        RunConfig(
            mode="sumo_actuated",
            config_path=args.config,
            zone_path=args.zone,
            queue_model_paths=(),
        )
    )
    expected_actions = {
        f"{intersection.tls_id}:{phase}"
        for intersection in area.intersections
        for phase in intersection.green_phase_indices
    }
    observed_actions = {
        str(action)
        for item in accepted
        for action in item.get("actions", [])
    }
    missing_actions = sorted(expected_actions - observed_actions)
    blocking: list[str] = []
    if missing:
        blocking.append(f"missing_runs={len(missing)}")
    if len(accepted) < 3:
        blocking.append("fewer_than_3_accepted_seeds")
    if missing_actions:
        blocking.append(f"missing_actions={len(missing_actions)}")
    report = {
        "status": "completed" if not blocking else "collecting",
        "ready_for_training": not blocking,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "forecast_contract": FORECAST_CONTRACT,
        "dataset_fingerprint": fingerprint,
        "dataset_schema_version": COUNTERFACTUAL_DATASET_SCHEMA_VERSION,
        "dataset_schema_sha256": counterfactual_dataset_schema_sha256(),
        "expected_runs": len(planned),
        "completed_runs": len(successful),
        "accepted_runs": len(accepted),
        "rejected_runs": len(successful) - len(accepted),
        "expected_action_count": len(expected_actions),
        "observed_action_count": len(observed_actions),
        "missing_actions": missing_actions,
        "missing_run_ids": missing,
        "blocking_reasons": blocking,
        "accepted_sample_files": [str(item["sample_file"]) for item in accepted],
        "runs": sorted(audit_rows, key=lambda item: int(item.get("seed", 0))),
    }
    write_json_atomic(output_dir / "quality_report.json", report)
    return report


def write_csv_atomic(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        raise RuntimeError(f"No counterfactual rows collected for {path.name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".csv.tmp")
    fieldnames = list(rows[0])
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def safe_int(function: Any, *args: object, default: int) -> int:
    try:
        return int(float(function(*args)))
    except Exception:
        return default


def safe_float(function: Any, *args: object, default: float) -> float:
    try:
        return float(function(*args))
    except Exception:
        return default


if __name__ == "__main__":
    main()
