from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flowmind.config import PROJECT_ROOT, RunConfig
from flowmind.experiment import run_experiment


def build_parser(default_mode: str | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a FlowMind SUMO experiment")
    if default_mode is None:
        parser.add_argument("mode", choices=("fixed", "local", "flowmind"))
    parser.add_argument("--duration", type=int, default=900)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--zone-size", type=int, default=6)
    parser.add_argument("--tls", nargs="*", default=())
    parser.add_argument("--priority-vehicle")
    parser.add_argument("--gui", action="store_true")
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
    parser.set_defaults(default_mode=default_mode)
    return parser


def main(default_mode: str | None = None) -> None:
    args = build_parser(default_mode).parse_args()
    mode = default_mode or args.mode
    summary = run_experiment(
        RunConfig(
            mode=mode,
            duration=args.duration,
            seed=args.seed,
            zone_size=args.zone_size,
            gui=args.gui,
            config_path=args.config,
            zone_path=args.zone,
            results_dir=args.results_dir,
            tls_ids=tuple(args.tls),
            priority_vehicle=args.priority_vehicle,
        )
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
