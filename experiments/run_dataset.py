from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flowmind.config import PROJECT_ROOT, RunConfig
from flowmind.experiment import run_experiment


INDEX_COLUMNS = (
    "run_id",
    "mode",
    "scenario",
    "seed",
    "duration",
    "sample_interval",
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
    "gridlock_risk",
    "controller_decisions",
    "phase_extensions",
    "phase_advances",
    "priority_decisions",
    "error",
)


def default_scenario_file(name: str) -> Path:
    new_area = PROJECT_ROOT / "simulation" / "new_area" / name
    if new_area.exists():
        return new_area
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
            "samples for queue prediction."
        )
    )
    parser.add_argument("--runs-per-mode", type=int, default=100)
    parser.add_argument(
        "--modes",
        nargs="+",
        choices=("local", "fixed", "flowmind"),
        default=("local", "fixed", "flowmind"),
    )
    parser.add_argument("--seed-start", type=int, default=42)
    parser.add_argument("--duration", type=int)
    parser.add_argument("--sample-interval", type=int, default=5)
    parser.add_argument("--scenario-name", default="rivne_focused")
    parser.add_argument(
        "--target-horizons",
        type=int,
        nargs="+",
        default=(30, 60, 90),
        help="Future horizons in seconds used for target columns.",
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
    if args.runs_per_mode <= 0:
        raise ValueError("--runs-per-mode must be positive")
    if args.sample_interval <= 0:
        raise ValueError("--sample-interval must be positive")

    duration = args.duration or infer_duration(args.config)
    output_dir = args.output_dir.resolve()
    samples_dir = output_dir / "samples"
    summaries_dir = output_dir / "summaries"
    index_path = output_dir / "dataset_index.csv"
    output_dir.mkdir(parents=True, exist_ok=True)
    samples_dir.mkdir(parents=True, exist_ok=True)
    summaries_dir.mkdir(parents=True, exist_ok=True)

    runs = list(planned_runs(args.modes, args.runs_per_mode, args.seed_start))
    print(
        f"Dataset plan: {len(runs)} runs, duration={duration}s, "
        f"sample_interval={args.sample_interval}s"
    )
    print(f"Config: {args.config}")
    print(f"Zone:   {args.zone}")
    print(f"Output: {output_dir}")

    if args.dry_run:
        for mode, seed in runs:
            print(run_id(args.scenario_name, mode, seed))
        return

    reset_index(index_path, append=args.resume)
    total = len(runs)
    for number, (mode, seed) in enumerate(runs, start=1):
        current_run_id = run_id(args.scenario_name, mode, seed)
        sample_path = samples_dir / f"{current_run_id}.csv"
        summary_dir = summaries_dir / current_run_id
        if args.resume and sample_path.exists():
            print(f"[{number}/{total}] skip existing {current_run_id}")
            continue

        print(f"[{number}/{total}] start {current_run_id}")
        started_at = datetime.now().isoformat(timespec="seconds")
        started = time.monotonic()
        failure: Exception | None = None
        row: dict[str, Any] = {
            "run_id": current_run_id,
            "mode": mode,
            "scenario": args.scenario_name,
            "seed": seed,
            "duration": duration,
            "sample_interval": args.sample_interval,
            "started_at": started_at,
            "summary_dir": str(summary_dir),
        }
        try:
            summary = run_experiment(
                RunConfig(
                    mode=mode,
                    duration=duration,
                    seed=seed,
                    gui=False,
                    config_path=args.config,
                    zone_path=args.zone,
                    results_dir=summary_dir,
                    dataset_dir=samples_dir,
                    dataset_run_id=current_run_id,
                    dataset_scenario=args.scenario_name,
                    dataset_sample_interval=args.sample_interval,
                    dataset_target_horizons=tuple(args.target_horizons),
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
            raise failure
        print(
            f"[{number}/{total}] done {current_run_id}: "
            f"rows={row.get('dataset_rows')} "
            f"throughput={row.get('throughput')} "
            f"avg_wait={row.get('average_waiting_time')}"
        )

    print(f"Dataset complete: {index_path}")


def planned_runs(
    modes: tuple[str, ...] | list[str],
    runs_per_mode: int,
    seed_start: int,
) -> tuple[tuple[str, int], ...]:
    return tuple(
        (mode, seed_start + offset)
        for mode in modes
        for offset in range(runs_per_mode)
    )


def run_id(scenario: str, mode: str, seed: int) -> str:
    return f"{scenario}_{mode}_seed_{seed:05d}"


def reset_index(index_path: Path, append: bool) -> None:
    if append and index_path.exists():
        return
    with index_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=INDEX_COLUMNS)
        writer.writeheader()


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
