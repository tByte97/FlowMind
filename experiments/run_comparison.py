from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flowmind.config import PROJECT_ROOT, RunConfig
from flowmind.experiment import run_experiment


def main() -> None:
    parser = argparse.ArgumentParser(description="Run all FlowMind control modes")
    parser.add_argument("--duration", type=int, default=900)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--zone-size", type=int, default=6)
    parser.add_argument("--priority-vehicle")
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
            )
        )
        for mode in ("fixed", "local", "flowmind")
    ]
    print(json.dumps(summaries, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
