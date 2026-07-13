from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flowmind.config import (
    DEFAULT_QUEUE_MODEL_PATHS,
    PROJECT_ROOT,
    ControlConfig,
    RunConfig,
)
from flowmind.emergency_vehicle import load_emergency_config
from flowmind.control_config_io import load_control_config
from flowmind.experiment import run_experiment
from flowmind.ml_approval import validate_queue_control_approval


def find_free_port(preferred: int, attempts: int = 50) -> int:
    for port in range(preferred, preferred + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError(
        f"Could not find a free dashboard port from {preferred} "
        f"to {preferred + attempts - 1}"
    )


def launch_dashboard(
    results_dir: Path,
    port: int,
    websocket_port: int = 8765,
) -> tuple[subprocess.Popen[bytes], int]:
    actual_port = find_free_port(port)
    environment = os.environ.copy()
    environment["FLOWMIND_RESULTS_DIR"] = str(results_dir.resolve())
    environment["FLOWMIND_LIVE_WS"] = f"ws://127.0.0.1:{websocket_port}"
    command = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(PROJECT_ROOT / "dashboard" / "app.py"),
        "--server.port",
        str(actual_port),
        "--server.headless",
        "false",
    ]
    return (
        subprocess.Popen(
            command,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        ),
        actual_port,
    )


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
    parser.add_argument("--websocket-port", type=int, default=8765)
    parser.add_argument(
        "--sensor-range",
        type=float,
        default=ControlConfig().sensor_range_meters,
        help="Intersection sensor/camera range in meters.",
    )
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
    parser.add_argument(
        "--no-baseline",
        action="store_true",
        help="Skip the automatic static fixed-time baseline used by the final comparison.",
    )
    parser.add_argument(
        "--queue-model",
        dest="queue_models",
        type=Path,
        nargs="+",
        action="extend",
        help=(
            "One or more trained queue forecast models used by FlowMind. "
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
        help="Enable ML influence only when a matching approval artifact exists.",
    )
    parser.add_argument(
        "--queue-control-approval",
        type=Path,
        default=PROJECT_ROOT / "models" / "queue_control_approval.json",
    )
    parser.add_argument(
        "--control-config",
        type=Path,
        help="Load a tuned ControlConfig JSON artifact.",
    )
    args = parser.parse_args()

    emergency = load_emergency_config(args.emergency_config).with_overrides(
        depart_time=args.emergency_depart,
        start_edge=args.emergency_from_edge,
        destination_edge=args.emergency_to_edge,
    )
    results_dir = args.results_dir
    model_paths = selected_queue_model_paths(args)
    base_control = (
        load_control_config(args.control_config)
        if args.control_config is not None
        else ControlConfig()
    )
    control = replace(
        base_control,
        sensor_range_meters=args.sensor_range,
        queue_forecast_shadow_mode=not args.enable_queue_control,
    )
    if args.enable_queue_control:
        validate_queue_control_approval(
            args.queue_control_approval,
            model_paths,
            args.config.resolve().parent / "osm.net.xml.gz",
            args.zone,
            control,
        )
    dashboard_process = None
    if not args.no_dashboard:
        results_dir.mkdir(parents=True, exist_ok=True)
        dashboard_process, port = launch_dashboard(
            results_dir,
            args.dashboard_port,
            args.websocket_port,
        )
        print(
            f"Streamlit started with PID {dashboard_process.pid}: "
            f"http://localhost:{port}"
        )

    try:
        if not args.no_baseline:
            print(
                "Collecting static fixed-time baseline with the same scenario and seed..."
            )
            run_experiment(
                RunConfig(
                    mode="static_fixed",
                    duration=args.duration,
                    seed=args.seed,
                    gui=False,
                    websocket_port=args.websocket_port,
                    results_dir=results_dir,
                    config_path=args.config,
                    zone_path=args.zone,
                    emergency=emergency,
                    control=control,
                    queue_model_paths=(),
                )
            )
            print("Static fixed-time baseline completed. Starting FlowMind demo...")
        summary = run_experiment(
            RunConfig(
                mode="flowmind",
                duration=args.duration,
                seed=args.seed,
                gui=not args.headless,
                gui_delay_ms=args.gui_delay,
                websocket_port=args.websocket_port,
                results_dir=results_dir,
                config_path=args.config,
                zone_path=args.zone,
                emergency=emergency,
                control=control,
                queue_model_paths=model_paths,
            )
        )
    except Exception:
        if dashboard_process is not None and dashboard_process.poll() is None:
            dashboard_process.terminate()
        raise
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def selected_queue_model_paths(args: argparse.Namespace) -> tuple[Path, ...]:
    if args.no_queue_model:
        return ()
    return tuple(args.queue_models or DEFAULT_QUEUE_MODEL_PATHS)


if __name__ == "__main__":
    main()
