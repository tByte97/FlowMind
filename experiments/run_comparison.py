from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flowmind.config import (
    DEFAULT_QUEUE_MODEL_PATHS,
    PROJECT_ROOT,
    ControlConfig,
    RunConfig,
)
from flowmind.experiment import run_experiment


def main() -> None:
    parser = argparse.ArgumentParser(description="Run all FlowMind control modes")
    parser.add_argument("--duration", type=int, default=900)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--zone-size", type=int, default=6)
    parser.add_argument(
        "--sensor-range",
        type=float,
        default=ControlConfig().sensor_range_meters,
        help="Intersection sensor/camera range in meters.",
    )
    parser.add_argument("--priority-vehicle")
    parser.add_argument("--websocket-port", type=int, default=8765)
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
    parser.add_argument("--results-dir", type=Path, default=PROJECT_ROOT / "results")
    parser.add_argument(
        "--queue-model",
        dest="queue_models",
        type=Path,
        nargs="+",
        action="extend",
        help=(
            "One or more trained queue forecast models used by flowmind mode. "
            "Defaults to the 30s/60s/90s current models."
        ),
    )
    parser.add_argument(
        "--no-queue-model",
        action="store_true",
        help="Disable ML queue forecast and run classic FlowMind scoring.",
    )
    args = parser.parse_args()

    summaries = [
        run_experiment(
            RunConfig(
                mode=mode,
                duration=args.duration,
                seed=args.seed,
                zone_size=args.zone_size,
                config_path=args.config,
                zone_path=args.zone,
                results_dir=args.results_dir,
                priority_vehicle=args.priority_vehicle,
                websocket_port=args.websocket_port,
                control=ControlConfig(sensor_range_meters=args.sensor_range),
                queue_model_paths=selected_queue_model_paths(args),
            )
        )
        for mode in ("static_fixed", "sumo_actuated", "local", "flowmind")
    ]
    print(json.dumps(summaries, ensure_ascii=False, indent=2))


def selected_queue_model_paths(args: argparse.Namespace) -> tuple[Path, ...]:
    if args.no_queue_model:
        return ()
    return tuple(args.queue_models or DEFAULT_QUEUE_MODEL_PATHS)


if __name__ == "__main__":
    main()
