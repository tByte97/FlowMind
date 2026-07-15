from __future__ import annotations

import argparse
import csv
import json
import random
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flowmind.config import DEFAULT_QUEUE_MODEL_PATHS, PROJECT_ROOT, ControlConfig, RunConfig
from flowmind.experiment import run_experiment
from flowmind.emergency_vehicle import load_emergency_config
from flowmind.ml_dataset import ML_DATASET_SCHEMA_VERSION, ml_dataset_schema_sha256
from flowmind.provenance import (
    canonical_sha256,
    controller_source_sha256,
    file_sha256,
    scenario_provenance,
)


INDEX_COLUMNS = (
    "dataset_fingerprint",
    "run_id",
    "mode",
    "scenario",
    "seed",
    "duration",
    "sample_interval",
    "decision_interval",
    "sensor_range_meters",
    "min_green",
    "max_green",
    "use_default_phase_timing",
    "default_green_extension",
    "blocked_occupancy",
    "downstream_weight",
    "area_pressure_weight",
    "queue_forecast_weight",
    "empty_approach_penalty",
    "empty_phase_penalty",
    "congested_queue_threshold",
    "congested_occupancy_threshold",
    "congested_approach_bonus",
    "demand_timer_seconds",
    "demand_wait_weight",
    "max_demand_wait_bonus",
    "hysteresis",
    "priority_distance",
    "max_priority_override",
    "clearance_seconds",
    "demand_profile",
    "demand_scale",
    "emergency_active",
    "started_at",
    "finished_at",
    "elapsed_seconds",
    "status",
    "dataset_csv",
    "dataset_rows",
    "summary_dir",
    "simulated_duration",
    "average_travel_time",
    "average_waiting_time",
    "average_queue_length",
    "max_queue_length",
    "throughput",
    "departed_vehicles",
    "peak_active_vehicles",
    "stops_count",
    "blocked_outgoing_share",
    "controller_decisions",
    "phase_extensions",
    "phase_advances",
    "priority_decisions",
    "phase_out_of_range_skips",
    "clearance_phase_skips",
    "min_green_skips",
    "scoreless_skips",
    "throughput_fallback_activations",
    "movement_mask_updates",
    "movement_mask_active_decisions",
    "movement_mask_program_updates",
    "movement_mask_failures",
    "queue_forecast_enabled",
    "queue_forecast_model",
    "queue_forecast_target",
    "queue_forecast_feature_count",
    "queue_forecast_model_count",
    "queue_forecast_horizons",
    "queue_forecast_horizon_weights",
    "queue_forecast_predictions",
    "queue_forecast_failures",
    "queue_forecast_trace_samples",
    "error",
)


@dataclass(frozen=True)
class DatasetRun:
    mode: str
    seed: int
    duration: int
    sample_interval: int
    control: ControlConfig
    run_index: int | None = None
    demand_profile: str = "normal"
    demand_scale: float = 1.0
    emergency_active: bool = False


def default_scenario_file(name: str) -> Path:
    """Return the versioned Rivne production/demo scenario by default.

    ``simulation/new_area`` is retained as an optional fixture, but it must
    never silently replace the configured Rivne zone during dataset creation.
    Callers that need another map pass ``--config`` and ``--zone`` explicitly.
    """

    return PROJECT_ROOT / "simulation" / "rivne_area" / name


def infer_duration(config_path: Path) -> int:
    manifest_path = config_path.with_name("focused.manifest.json")
    if manifest_path.exists():
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        duration = int(payload.get("duration", 0))
        if duration > 0:
            return duration

    root = ET.parse(config_path).getroot()
    for end in root.findall(".//end"):
        value = end.attrib.get("value")
        if value is not None:
            return int(float(value))
    return 1800


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run many headless FlowMind simulations and write ML-ready CSV "
            "samples for queue prediction. Use --full-real for the complete "
            "100-cycle static_fixed/sumo_actuated/local/flowmind training plan."
        )
    )
    parser.add_argument("--runs-per-mode", type=int, default=100)
    parser.add_argument(
        "--modes",
        nargs="+",
        choices=("static_fixed", "sumo_actuated", "local", "flowmind"),
        default=("static_fixed", "sumo_actuated", "local", "flowmind"),
    )
    parser.add_argument(
        "--full-real",
        action="store_true",
        help=(
            "Use the full realistic training profile: "
            "static_fixed/sumo_actuated/local/flowmind, "
            "randomized sensor-window control settings, paired seeds, and "
            "100 cycles by default. This produces 400 runs unless "
            "--runs-per-mode is changed."
        ),
    )
    parser.add_argument(
        "--independent-random-seeds",
        action="store_true",
        help=(
            "With --randomize, generate an independent seed/settings tuple for "
            "each mode instead of pairing the same seed across all modes."
        ),
    )
    parser.add_argument(
        "--keep-going",
        action="store_true",
        help="Continue collecting the dataset after a failed simulation.",
    )
    parser.add_argument("--seed-start", type=int, default=42)
    parser.add_argument("--duration", type=int)
    parser.add_argument(
        "--randomize",
        action="store_true",
        help=(
            "Generate random seeds, durations, sample intervals and control "
            "parameters while keeping the same configured zone."
        ),
    )
    parser.add_argument(
        "--plan-seed",
        type=int,
        default=20260705,
        help="Seed for the dataset plan generator; use another value for a new plan.",
    )
    parser.add_argument("--seed-min", type=int, default=1_000)
    parser.add_argument("--seed-max", type=int, default=2_000_000_000)
    parser.add_argument(
        "--duration-min",
        type=int,
        default=600,
        help="Minimum randomized simulation duration in seconds.",
    )
    parser.add_argument(
        "--duration-max",
        type=int,
        default=1800,
        help="Maximum randomized simulation duration in seconds.",
    )
    parser.add_argument("--sample-interval", type=int, default=5)
    parser.add_argument(
        "--random-sample-intervals",
        type=int,
        nargs="+",
        default=(3, 5, 6, 10),
        help=(
            "Allowed sample intervals for --randomize. Each value must divide "
            "every target horizon."
        ),
    )
    parser.add_argument("--scenario-name", default="rivne_focused")
    parser.add_argument(
        "--target-horizons",
        type=int,
        nargs="+",
        default=(30, 60, 90),
        help="Future horizons in seconds used for target columns.",
    )
    parser.add_argument(
        "--emergency-config",
        type=Path,
        default=PROJECT_ROOT / "simulation" / "rivne_area" / "emergency.json",
    )
    parser.add_argument(
        "--emergency-share",
        type=float,
        default=0.15,
        help="Fraction of randomized seed cycles that include an emergency vehicle.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=default_scenario_file("focused.sumocfg"),
    )
    parser.add_argument(
        "--zone",
        type=Path,
        default=default_scenario_file("central_zone.json"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "results" / "dataset",
    )
    parser.add_argument(
        "--queue-model",
        dest="queue_models",
        type=Path,
        nargs="+",
        action="extend",
        help=(
            "One or more trained queue forecast models used by flowmind runs. "
            "Defaults to the 30s/60s/90s current models."
        ),
    )
    parser.add_argument(
        "--no-queue-model",
        action="store_true",
        help="Disable ML queue forecast for dataset generation.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip runs whose sample CSV already exists.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned runs without starting SUMO.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    apply_full_real_profile(args)
    if args.runs_per_mode <= 0:
        raise ValueError("--runs-per-mode must be positive")
    if args.sample_interval <= 0:
        raise ValueError("--sample-interval must be positive")
    if not 0.0 <= args.emergency_share <= 1.0:
        raise ValueError("--emergency-share must be between 0 and 1")
    if args.seed_min <= 0 or args.seed_max <= 0 or args.seed_min > args.seed_max:
        raise ValueError("--seed-min/--seed-max must be positive and ordered")

    default_duration = args.duration or infer_duration(args.config)
    if args.duration_min <= 0 or args.duration_max <= 0:
        raise ValueError("--duration-min/--duration-max must be positive")
    if args.duration_min > args.duration_max:
        raise ValueError("--duration-min cannot be greater than --duration-max")
    if args.randomize:
        validate_random_sample_intervals(
            tuple(args.random_sample_intervals),
            tuple(args.target_horizons),
        )

    output_dir = args.output_dir.resolve()
    samples_dir = output_dir / "samples"
    summaries_dir = output_dir / "summaries"
    index_path = output_dir / "dataset_index.csv"
    output_dir.mkdir(parents=True, exist_ok=True)
    samples_dir.mkdir(parents=True, exist_ok=True)
    summaries_dir.mkdir(parents=True, exist_ok=True)

    runs = list(planned_runs(args, default_duration))
    manifest_path = output_dir / "dataset_plan.json"
    fingerprint = dataset_fingerprint(args, runs)
    if args.resume and _dataset_artifacts_exist(output_dir):
        validate_resume_fingerprint(manifest_path, fingerprint)
    write_plan_manifest(manifest_path, args, runs, fingerprint)
    print(
        f"Dataset plan: {len(runs)} runs, "
        f"randomized={'yes' if args.randomize else 'no'}"
    )
    if args.randomize and not args.independent_random_seeds:
        print(
            f"Paired seed cycles: {args.runs_per_mode}; "
            f"modes per cycle: {', '.join(args.modes)}"
        )
    if args.randomize:
        durations = [run.duration for run in runs]
        print(
            f"Duration range: {min(durations)}..{max(durations)}s, "
            f"plan_seed={args.plan_seed}"
        )
        print(
            "Sample intervals: "
            + ", ".join(str(value) for value in sorted({run.sample_interval for run in runs}))
        )
    else:
        print(
            f"Duration: {default_duration}s, "
            f"sample_interval={args.sample_interval}s"
        )
    print(f"Config: {args.config}")
    print(f"Zone:   {args.zone}")
    print(f"Output: {output_dir}")
    print(f"Fingerprint: {fingerprint}")

    if args.dry_run:
        for run in runs:
            print(describe_run(args.scenario_name, run))
        return

    reset_index(index_path, append=args.resume)
    completed_run_ids = (
        successful_run_ids(index_path) if args.resume else set()
    )
    total = len(runs)
    for number, run in enumerate(runs, start=1):
        current_run_id = run_id(args.scenario_name, run)
        sample_path = samples_dir / f"{current_run_id}.csv"
        summary_dir = summaries_dir / current_run_id
        if (
            args.resume
            and current_run_id in completed_run_ids
            and sample_path.exists()
        ):
            print(f"[{number}/{total}] skip existing {current_run_id}")
            continue

        print(f"[{number}/{total}] start {current_run_id}")
        started_at = datetime.now().isoformat(timespec="seconds")
        started = time.monotonic()
        failure: Exception | None = None
        row: dict[str, Any] = {
            "dataset_fingerprint": fingerprint,
            "run_id": current_run_id,
            "mode": run.mode,
            "scenario": args.scenario_name,
            "seed": run.seed,
            "duration": run.duration,
            "sample_interval": run.sample_interval,
            "decision_interval": run.control.decision_interval,
            "sensor_range_meters": run.control.sensor_range_meters,
            "min_green": run.control.min_green,
            "max_green": run.control.max_green,
            "use_default_phase_timing": int(run.control.use_default_phase_timing),
            "default_green_extension": run.control.default_green_extension,
            "blocked_occupancy": run.control.blocked_occupancy,
            "downstream_weight": run.control.downstream_weight,
            "area_pressure_weight": run.control.area_pressure_weight,
            "queue_forecast_weight": run.control.queue_forecast_weight,
            "empty_approach_penalty": run.control.empty_approach_penalty,
            "empty_phase_penalty": run.control.empty_phase_penalty,
            "congested_queue_threshold": run.control.congested_queue_threshold,
            "congested_occupancy_threshold": (
                run.control.congested_occupancy_threshold
            ),
            "congested_approach_bonus": run.control.congested_approach_bonus,
            "demand_timer_seconds": run.control.demand_timer_seconds,
            "demand_wait_weight": run.control.demand_wait_weight,
            "max_demand_wait_bonus": run.control.max_demand_wait_bonus,
            "hysteresis": run.control.hysteresis,
            "priority_distance": run.control.priority_distance,
            "max_priority_override": run.control.max_priority_override,
            "clearance_seconds": run.control.clearance_seconds,
            "demand_profile": run.demand_profile,
            "demand_scale": run.demand_scale,
            "emergency_active": int(run.emergency_active),
            "started_at": started_at,
            "summary_dir": str(summary_dir),
        }
        try:
            summary = run_experiment(
                RunConfig(
                    mode=run.mode,
                    duration=run.duration,
                    seed=run.seed,
                    gui=False,
                    config_path=args.config,
                    zone_path=args.zone,
                    results_dir=summary_dir,
                    control=run.control,
                    queue_model_paths=selected_queue_model_paths(args),
                    dataset_dir=samples_dir,
                    dataset_run_id=current_run_id,
                    dataset_scenario=args.scenario_name,
                    dataset_sample_interval=run.sample_interval,
                    dataset_target_horizons=tuple(args.target_horizons),
                    dataset_fingerprint=fingerprint,
                    demand_profile=run.demand_profile,
                    demand_scale=run.demand_scale,
                    dataset_emergency_active=run.emergency_active,
                    emergency=(
                        load_emergency_config(args.emergency_config)
                        if run.emergency_active
                        else None
                    ),
                    enable_live_telemetry=False,
                )
            )
            row.update(summary)
            row["status"] = "ok"
        except Exception as exc:
            row["status"] = "failed"
            row["error"] = repr(exc)
            failure = exc
        finally:
            row["finished_at"] = datetime.now().isoformat(timespec="seconds")
            row["elapsed_seconds"] = round(time.monotonic() - started, 3)

        append_index_row(index_path, row)
        if failure is not None:
            print(f"[{number}/{total}] failed {current_run_id}: {failure!r}")
            if not args.keep_going:
                raise failure
            continue
        print(
            f"[{number}/{total}] done {current_run_id}: "
            f"rows={row.get('dataset_rows')} "
            f"throughput={row.get('throughput')} "
            f"avg_wait={row.get('average_waiting_time')}"
        )

    print(f"Dataset complete: {index_path}")


def apply_full_real_profile(args: argparse.Namespace) -> None:
    if not args.full_real:
        return
    args.randomize = True
    args.modes = ("static_fixed", "sumo_actuated", "local", "flowmind")
    args.independent_random_seeds = False


def planned_runs(
    args: argparse.Namespace,
    default_duration: int,
) -> tuple[DatasetRun, ...]:
    if args.randomize:
        return randomized_runs(args)

    return tuple(
        DatasetRun(
            mode=mode,
            seed=args.seed_start + offset,
            duration=default_duration,
            sample_interval=args.sample_interval,
            control=ControlConfig(),
            demand_profile="normal",
            demand_scale=1.0,
            emergency_active=False,
        )
        for mode in args.modes
        for offset in range(args.runs_per_mode)
    )


def selected_queue_model_paths(args: argparse.Namespace) -> tuple[Path, ...]:
    if args.no_queue_model:
        return ()
    return tuple(args.queue_models or DEFAULT_QUEUE_MODEL_PATHS)


def randomized_runs(args: argparse.Namespace) -> tuple[DatasetRun, ...]:
    rng = random.Random(args.plan_seed)
    used_seeds: set[int] = set()
    runs: list[DatasetRun] = []

    if not args.independent_random_seeds:
        for offset in range(args.runs_per_mode):
            seed = unique_random_seed(rng, args.seed_min, args.seed_max, used_seeds)
            duration = rng.randint(args.duration_min, args.duration_max)
            sample_interval = rng.choice(tuple(args.random_sample_intervals))
            control = random_control_config(rng)
            demand_profile, demand_scale = random_demand_profile(rng)
            emergency_active = rng.random() < args.emergency_share
            for mode in args.modes:
                runs.append(
                    DatasetRun(
                        mode=mode,
                        seed=seed,
                        duration=duration,
                        sample_interval=sample_interval,
                        control=control,
                        run_index=offset + 1,
                        demand_profile=demand_profile,
                        demand_scale=demand_scale,
                        emergency_active=emergency_active,
                    )
                )
        rng.shuffle(runs)
        return tuple(runs)

    for mode in args.modes:
        for offset in range(args.runs_per_mode):
            seed = unique_random_seed(rng, args.seed_min, args.seed_max, used_seeds)
            control = random_control_config(rng)
            demand_profile, demand_scale = random_demand_profile(rng)
            emergency_active = rng.random() < args.emergency_share
            runs.append(
                DatasetRun(
                    mode=mode,
                    seed=seed,
                    duration=rng.randint(args.duration_min, args.duration_max),
                    sample_interval=rng.choice(tuple(args.random_sample_intervals)),
                    control=control,
                    run_index=offset + 1,
                    demand_profile=demand_profile,
                    demand_scale=demand_scale,
                    emergency_active=emergency_active,
                )
            )

    rng.shuffle(runs)
    return tuple(runs)


def unique_random_seed(
    rng: random.Random,
    minimum: int,
    maximum: int,
    used: set[int],
) -> int:
    if len(used) >= maximum - minimum + 1:
        raise ValueError("Random seed range is too small for the requested runs")
    while True:
        seed = rng.randint(minimum, maximum)
        if seed not in used:
            used.add(seed)
            return seed


def random_control_config(rng: random.Random) -> ControlConfig:
    min_green = rng.randint(8, 16)
    max_green = rng.randint(max(min_green + 15, 30), 65)
    return ControlConfig(
        decision_interval=rng.choice((2, 3, 4, 5)),
        sensor_range_meters=round(rng.uniform(80.0, 180.0), 3),
        min_green=min_green,
        max_green=max_green,
        use_default_phase_timing=True,
        default_green_extension=round(rng.uniform(5.0, 12.0), 3),
        blocked_occupancy=round(rng.uniform(0.72, 0.90), 3),
        downstream_weight=round(rng.uniform(6.0, 15.0), 3),
        area_pressure_weight=round(rng.uniform(0.15, 0.65), 3),
        queue_forecast_weight=round(rng.uniform(0.35, 1.25), 3),
        empty_approach_penalty=round(rng.uniform(5.0, 12.0), 3),
        empty_phase_penalty=round(rng.uniform(20.0, 45.0), 3),
        congested_queue_threshold=rng.randint(6, 12),
        congested_occupancy_threshold=round(rng.uniform(0.55, 0.78), 3),
        congested_approach_bonus=round(rng.uniform(8.0, 18.0), 3),
        demand_timer_seconds=rng.randint(4, 10),
        demand_wait_weight=round(rng.uniform(0.25, 0.75), 3),
        max_demand_wait_bonus=round(rng.uniform(12.0, 24.0), 3),
        hysteresis=round(rng.uniform(0.4, 2.5), 3),
        priority_distance=round(rng.uniform(350.0, 700.0), 3),
        max_priority_override=rng.randint(25, 50),
        clearance_seconds=rng.randint(3, 8),
    )


def random_demand_profile(rng: random.Random) -> tuple[str, float]:
    """Choose a real SUMO demand scale, not a cosmetic dataset label."""

    profiles = (
        ("off_peak", 0.65, 0.85),
        ("normal", 0.90, 1.10),
        ("morning_peak", 1.10, 1.35),
        ("evening_peak", 1.20, 1.50),
        ("oversaturated", 1.45, 1.80),
        ("incident", 1.00, 1.30),
        ("lane_closure", 0.90, 1.20),
    )
    name, minimum, maximum = rng.choice(profiles)
    return name, round(rng.uniform(minimum, maximum), 3)


def validate_random_sample_intervals(
    sample_intervals: tuple[int, ...],
    target_horizons: tuple[int, ...],
) -> None:
    if not sample_intervals:
        raise ValueError("--random-sample-intervals cannot be empty")
    invalid = [value for value in sample_intervals if value <= 0]
    if invalid:
        raise ValueError("--random-sample-intervals values must be positive")
    incompatible = [
        value
        for value in sample_intervals
        if any(horizon % value for horizon in target_horizons)
    ]
    if incompatible:
        values = ", ".join(str(value) for value in incompatible)
        horizons = ", ".join(str(value) for value in target_horizons)
        raise ValueError(
            "Random sample intervals must divide every target horizon. "
            f"Invalid: {values}; horizons: {horizons}"
        )


def run_id(scenario: str, run: DatasetRun) -> str:
    prefix = f"{scenario}_{run.mode}"
    if run.run_index is None:
        return f"{prefix}_seed_{run.seed:05d}"
    return f"{prefix}_r{run.run_index:04d}_seed_{run.seed:010d}"


def describe_run(scenario: str, run: DatasetRun) -> str:
    return (
        f"{run_id(scenario, run)} "
        f"duration={run.duration}s "
        f"sample_interval={run.sample_interval}s "
        f"decision_interval={run.control.decision_interval}s "
        f"sensor_range={run.control.sensor_range_meters}m "
        f"green={run.control.min_green}-{run.control.max_green}s "
        f"blocked={run.control.blocked_occupancy} "
        f"downstream_weight={run.control.downstream_weight} "
        f"area_weight={run.control.area_pressure_weight} "
        f"queue_weight={run.control.queue_forecast_weight}"
        f" demand={run.demand_profile}@{run.demand_scale}"
        f" emergency={'yes' if run.emergency_active else 'no'}"
    )


def write_plan_manifest(
    path: Path,
    args: argparse.Namespace,
    runs: list[DatasetRun],
    fingerprint: str,
) -> None:
    payload = {
        "schema_version": 2,
        "dataset_schema_version": ML_DATASET_SCHEMA_VERSION,
        "dataset_schema_sha256": ml_dataset_schema_sha256(),
        "controller_source_sha256": controller_source_sha256(),
        "dataset_fingerprint": fingerprint,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "scenario": args.scenario_name,
        "config": str(args.config),
        "zone": str(args.zone),
        "output_dir": str(args.output_dir),
        "modes": list(args.modes),
        "runs_per_mode": args.runs_per_mode,
        "total_runs": len(runs),
        "randomized": bool(args.randomize),
        "full_real": bool(args.full_real),
        "paired_seed_cycles": bool(
            args.randomize and not args.independent_random_seeds
        ),
        "plan_seed": args.plan_seed,
        "target_horizons": list(args.target_horizons),
        "sample_intervals": sorted({run.sample_interval for run in runs}),
        "duration_range": [
            min((run.duration for run in runs), default=0),
            max((run.duration for run in runs), default=0),
        ],
        "sensor_range_meters": [
            min((run.control.sensor_range_meters for run in runs), default=0),
            max((run.control.sensor_range_meters for run in runs), default=0),
        ],
        "first_runs": [
            {
                "run_id": run_id(args.scenario_name, run),
                "mode": run.mode,
                "seed": run.seed,
                "duration": run.duration,
                "sample_interval": run.sample_interval,
                "sensor_range_meters": run.control.sensor_range_meters,
                "demand_profile": run.demand_profile,
                "demand_scale": run.demand_scale,
                "emergency_active": run.emergency_active,
            }
            for run in runs[:20]
        ],
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def dataset_fingerprint(args: argparse.Namespace, runs: list[DatasetRun]) -> str:
    config_path = args.config.resolve()
    zone_path = args.zone.resolve()
    network_path = config_path.parent / "osm.net.xml.gz"
    provenance = scenario_provenance(
        config_path,
        zone_path,
        network_path,
        args.scenario_name,
    )
    scenario_contract = asdict(provenance)
    # Deployment paths differ between a checkout and /app in the container;
    # fingerprints identify content, never a host-specific absolute path.
    scenario_contract.pop("manifest_path", None)
    payload = {
        "dataset_schema_version": ML_DATASET_SCHEMA_VERSION,
        "dataset_schema_sha256": ml_dataset_schema_sha256(),
        "scenario": scenario_contract,
        "scenario_name": args.scenario_name,
        "modes": list(args.modes),
        "plan_seed": args.plan_seed,
        "target_horizons": list(args.target_horizons),
        "emergency_config_sha256": file_sha256(args.emergency_config),
        "emergency_share": args.emergency_share,
        "runs": [
            {
                "mode": run.mode,
                "seed": run.seed,
                "duration": run.duration,
                "sample_interval": run.sample_interval,
                "run_index": run.run_index,
                "demand_profile": run.demand_profile,
                "demand_scale": run.demand_scale,
                "emergency_active": run.emergency_active,
                "control": asdict(run.control),
            }
            for run in runs
        ],
    }
    return canonical_sha256(payload)


def validate_resume_fingerprint(path: Path, requested: str) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError(
            "Cannot --resume: dataset_plan.json is missing; use a new output "
            "directory or start without --resume"
        ) from error
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Cannot --resume invalid dataset manifest: {error}") from error
    existing = str(payload.get("dataset_fingerprint", ""))
    if not existing:
        raise ValueError(
            "Cannot --resume legacy dataset without dataset_fingerprint; "
            "archive it and start a clean dataset"
        )
    if existing != requested:
        raise ValueError(
            "Cannot --resume: dataset fingerprint mismatch "
            f"(existing={existing}, requested={requested})"
        )


def _dataset_artifacts_exist(output_dir: Path) -> bool:
    return (
        (output_dir / "dataset_plan.json").exists()
        or (output_dir / "dataset_index.csv").exists()
        or any((output_dir / "samples").glob("*.csv"))
    )


def reset_index(index_path: Path, append: bool) -> None:
    if append and index_path.exists():
        return
    with index_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=INDEX_COLUMNS)
        writer.writeheader()


def successful_run_ids(index_path: Path) -> set[str]:
    if not index_path.exists():
        return set()
    with index_path.open(newline="", encoding="utf-8") as handle:
        return {
            str(row.get("run_id"))
            for row in csv.DictReader(handle)
            if row.get("run_id") and row.get("status") == "ok"
        }


def append_index_row(index_path: Path, row: dict[str, Any]) -> None:
    with index_path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=INDEX_COLUMNS,
            extrasaction="ignore",
        )
        writer.writerow({key: row.get(key, "") for key in INDEX_COLUMNS})


if __name__ == "__main__":
    main()
