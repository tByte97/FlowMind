from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flowmind.config import PROJECT_ROOT, ControlConfig, RunConfig
from flowmind.emergency_vehicle import load_emergency_config
from flowmind.experiment import run_experiment


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Tune FlowMind's zonal objective with resumable Optuna search."
    )
    parser.add_argument("--trials", type=int, default=60)
    parser.add_argument("--duration", type=int, default=900)
    parser.add_argument("--seeds-per-trial", type=int, default=3)
    parser.add_argument("--seed-start", type=int, default=9000)
    parser.add_argument("--study-name", default="flowmind_rivne_zone20")
    parser.add_argument(
        "--storage",
        default="sqlite:///results/tuning/optuna.db",
    )
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=PROJECT_ROOT / "results" / "tuning",
    )
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
        "--emergency-config",
        type=Path,
        default=PROJECT_ROOT / "simulation" / "rivne_area" / "emergency.json",
    )
    parser.add_argument("--without-emergency", action="store_true")
    return parser


def control_from_trial(trial: Any) -> ControlConfig:
    min_green = trial.suggest_int("min_green", 8, 16)
    return ControlConfig(
        decision_interval=trial.suggest_int("decision_interval", 2, 5),
        min_green=min_green,
        max_green=trial.suggest_int("max_green", max(30, min_green + 12), 65),
        blocked_occupancy=trial.suggest_float("blocked_occupancy", 0.72, 0.90),
        downstream_weight=trial.suggest_float("downstream_weight", 6.0, 16.0),
        downstream_graph_weight=trial.suggest_float(
            "downstream_graph_weight", 12.0, 40.0
        ),
        area_pressure_weight=trial.suggest_float(
            "area_pressure_weight", 0.10, 0.80
        ),
        objective_delay_weight=trial.suggest_float(
            "objective_delay_weight", 0.25, 2.5
        ),
        objective_queue_growth_weight=trial.suggest_float(
            "objective_queue_growth_weight", 0.5, 4.0
        ),
        objective_spillback_weight=trial.suggest_float(
            "objective_spillback_weight", 15.0, 55.0
        ),
        objective_stops_weight=trial.suggest_float(
            "objective_stops_weight", 0.1, 1.5
        ),
        objective_throughput_weight=trial.suggest_float(
            "objective_throughput_weight", 0.25, 2.5
        ),
        platoon_arrival_weight=trial.suggest_float(
            "platoon_arrival_weight", 0.1, 2.0
        ),
        zone_coordination_weight=trial.suggest_float(
            "zone_coordination_weight", 0.25, 4.0
        ),
        saturation_flow_vph_per_lane=trial.suggest_float(
            "saturation_flow_vph_per_lane", 1500.0, 2100.0
        ),
        coordination_horizon_seconds=trial.suggest_int(
            "coordination_horizon_seconds", 30, 90, step=15
        ),
        hysteresis=trial.suggest_float("hysteresis", 0.4, 3.0),
        queue_forecast_shadow_mode=True,
    )


def objective_score(summary: dict[str, object]) -> float:
    waiting = _metric(summary, "average_waiting_time", 1_000.0)
    queue = _metric(summary, "average_queue_length", 1_000.0)
    spillback = _metric(summary, "blocked_outgoing_share", 1.0)
    stops = _metric(summary, "stops_count", 100_000.0)
    outflow = _metric(summary, "zone_outflow", 0.0)
    emergency_eta = _metric(summary, "emergency_eta", 0.0)
    departed = max(_metric(summary, "departed_vehicles", 1.0), 1.0)
    return (
        waiting
        + queue * 2.0
        + spillback * 120.0
        + stops / departed * 10.0
        + emergency_eta * 0.10
        - outflow * 0.03
    )


def main() -> None:
    args = build_parser().parse_args()
    if args.trials <= 0 or args.duration <= 0 or args.seeds_per_trial <= 0:
        raise ValueError("trials, duration and seeds-per-trial must be positive")
    try:
        import optuna
    except ImportError as error:
        raise SystemExit(
            "Optuna is required: install requirements.runtime.txt or `pip install optuna`."
        ) from error
    args.results_dir.mkdir(parents=True, exist_ok=True)
    emergency = (
        None
        if args.without_emergency
        else load_emergency_config(args.emergency_config)
    )

    def objective(trial: Any) -> float:
        control = control_from_trial(trial)
        scores: list[float] = []
        for offset in range(args.seeds_per_trial):
            seed = args.seed_start + trial.number * args.seeds_per_trial + offset
            summary = run_experiment(
                RunConfig(
                    mode="flowmind",
                    duration=args.duration,
                    seed=seed,
                    config_path=args.config,
                    zone_path=args.zone,
                    results_dir=(
                        args.results_dir
                        / f"trial_{trial.number:04d}"
                        / f"seed_{seed}"
                    ),
                    emergency=emergency,
                    control=control,
                    queue_model_paths=(),
                    enable_live_telemetry=False,
                )
            )
            scores.append(objective_score(summary))
            trial.report(fmean(scores), step=offset)
            if trial.should_prune():
                raise optuna.TrialPruned()
        return fmean(scores)

    study = optuna.create_study(
        study_name=args.study_name,
        storage=args.storage,
        direction="minimize",
        load_if_exists=True,
        sampler=optuna.samplers.TPESampler(seed=args.seed_start, multivariate=True),
        pruner=optuna.pruners.MedianPruner(n_warmup_steps=1),
    )
    study.optimize(objective, n_trials=args.trials)
    best_control = control_from_trial(study.best_trial)
    payload = {
        "schema_version": 1,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "study_name": args.study_name,
        "best_value": study.best_value,
        "best_trial": study.best_trial.number,
        "best_params": study.best_params,
        "control_config": asdict(best_control),
        "completed_trials": len(study.trials),
    }
    output = args.results_dir / "best_control_config.json"
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Best FlowMind control config: {output}")


def _metric(summary: dict[str, object], key: str, default: float) -> float:
    value = summary.get(key)
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


if __name__ == "__main__":
    main()
