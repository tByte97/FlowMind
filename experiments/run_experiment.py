from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flowmind.config import (
    CONTROL_MODES,
    DEFAULT_QUEUE_MODEL_PATHS,
    LEGACY_CONTROL_MODE_ALIASES,
    PROJECT_ROOT,
    ControlConfig,
    RunConfig,
)
from flowmind.emergency_vehicle import load_emergency_config
from flowmind.experiment import run_experiment


def build_parser(default_mode: str | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a FlowMind SUMO experiment")
    if default_mode is None:
        parser.add_argument(
            "mode",
            choices=(*CONTROL_MODES, *LEGACY_CONTROL_MODE_ALIASES),
        )
    parser.add_argument("--duration", type=int, default=900)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--zone-size", type=int, default=6)
    parser.add_argument(
        "--sensor-range",
        type=float,
        default=ControlConfig().sensor_range_meters,
        help="Intersection sensor/camera range in meters.",
    )
    parser.add_argument("--tls", nargs="*", default=())
    parser.add_argument("--priority-vehicle")
    parser.add_argument(
        "--emergency",
        action="store_true",
        help="Automatically create and prioritize an ambulance",
    )
    parser.add_argument(
        "--emergency-config",
        type=Path,
        default=PROJECT_ROOT / "simulation" / "rivne_area" / "emergency.json",
    )
    parser.add_argument("--emergency-depart", type=float)
    parser.add_argument("--emergency-from-edge")
    parser.add_argument("--emergency-to-edge")
    parser.add_argument("--gui", action="store_true")
    parser.add_argument("--websocket-port", type=int, default=8765)
    parser.add_argument(
        "--gui-delay",
        type=int,
        default=50,
        help="Delay between GUI simulation steps in milliseconds",
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
    parser.add_argument(
        "--enable-queue-control",
        action="store_true",
        help=(
            "Allow in-domain, high-confidence ML predictions to affect control. "
            "The safe default is shadow-only evaluation."
        ),
    )
    parser.set_defaults(default_mode=default_mode)
    return parser


def main(default_mode: str | None = None) -> None:
    args = build_parser(default_mode).parse_args()
    mode = default_mode or args.mode
    emergency = None
    if args.emergency:
        emergency = load_emergency_config(args.emergency_config).with_overrides(
            depart_time=args.emergency_depart,
            start_edge=args.emergency_from_edge,
            destination_edge=args.emergency_to_edge,
        )
    summary = run_experiment(
        RunConfig(
            mode=mode,
            duration=args.duration,
            seed=args.seed,
            zone_size=args.zone_size,
            gui=args.gui,
            gui_delay_ms=args.gui_delay,
            websocket_port=args.websocket_port,
            config_path=args.config,
            zone_path=args.zone,
            results_dir=args.results_dir,
            tls_ids=tuple(args.tls),
            priority_vehicle=args.priority_vehicle,
            emergency=emergency,
            control=ControlConfig(
                sensor_range_meters=args.sensor_range,
                queue_forecast_shadow_mode=not args.enable_queue_control,
            ),
            queue_model_paths=selected_queue_model_paths(args),
        )
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def selected_queue_model_paths(args: argparse.Namespace) -> tuple[Path, ...]:
    if args.no_queue_model:
        return ()
    return tuple(args.queue_models or DEFAULT_QUEUE_MODEL_PATHS)


if __name__ == "__main__":
    main()
