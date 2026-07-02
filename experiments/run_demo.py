from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flowmind.config import PROJECT_ROOT, RunConfig
from flowmind.emergency_vehicle import load_emergency_config
from flowmind.experiment import run_experiment


def launch_dashboard(results_dir: Path, port: int) -> subprocess.Popen[bytes]:
    environment = os.environ.copy()
    environment["FLOWMIND_RESULTS_DIR"] = str(results_dir.resolve())
    command = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(PROJECT_ROOT / "dashboard" / "app.py"),
        "--server.port",
        str(port),
        "--server.headless",
        "false",
    ]
    return subprocess.Popen(command, env=environment)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the FlowMind ambulance demo, then open its dashboard"
    )
    parser.add_argument("--duration", type=int, default=600)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--emergency-depart", type=float, default=180.0)
    parser.add_argument("--emergency-from-edge")
    parser.add_argument("--emergency-to-edge")
    parser.add_argument("--dashboard-port", type=int, default=8501)
    parser.add_argument("--gui-delay", type=int, default=75)
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
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=PROJECT_ROOT / "results" / "demo",
    )
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--no-dashboard", action="store_true")
    args = parser.parse_args()

    emergency = load_emergency_config(args.emergency_config).with_overrides(
        depart_time=args.emergency_depart,
        start_edge=args.emergency_from_edge,
        destination_edge=args.emergency_to_edge,
    )
    results_dir = args.results_dir
    summary = run_experiment(
        RunConfig(
            mode="flowmind",
            duration=args.duration,
            seed=args.seed,
            gui=not args.headless,
            gui_delay_ms=args.gui_delay,
            results_dir=results_dir,
            config_path=args.config,
            zone_path=args.zone,
            emergency=emergency,
        )
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if not args.no_dashboard:
        process = launch_dashboard(results_dir, args.dashboard_port)
        print(
            f"Streamlit started with PID {process.pid}: "
            f"http://localhost:{args.dashboard_port}"
        )


if __name__ == "__main__":
    main()
