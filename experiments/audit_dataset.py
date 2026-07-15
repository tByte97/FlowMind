from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flowmind.config import PROJECT_ROOT
from flowmind.dataset_quality import DatasetQualityThresholds, audit_dataset


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit completed SUMO dataset runs before ML training."
    )
    parser.add_argument(
        "--dataset-dir",
        type=Path,
        default=PROJECT_ROOT / "results" / "dataset",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--max-teleport-rate", type=float, default=0.05)
    parser.add_argument(
        "--max-emergency-brakes-per-1000", type=float, default=20.0
    )
    parser.add_argument("--max-collisions", type=int, default=0)
    parser.add_argument(
        "--min-actuated-detector-coverage", type=float, default=1.0
    )
    parser.add_argument("--min-departed-vehicles", type=int, default=1)
    parser.add_argument(
        "--expected-runs",
        type=int,
        help="Refuse to certify a partial dataset (for production this is 400).",
    )
    parser.add_argument(
        "--fail-if-rejected",
        action="store_true",
        help="Exit 2 when at least one run is rejected; the report is still written.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    validate_args(args)
    thresholds = DatasetQualityThresholds(
        max_teleport_rate=args.max_teleport_rate,
        max_emergency_brakes_per_1000=args.max_emergency_brakes_per_1000,
        max_collisions=args.max_collisions,
        min_actuated_detector_coverage=args.min_actuated_detector_coverage,
        min_departed_vehicles=args.min_departed_vehicles,
    )
    report = audit_dataset(args.dataset_dir, thresholds)
    if args.expected_runs is not None and report["run_count"] != args.expected_runs:
        report["status"] = "incomplete"
        report["expected_run_count"] = args.expected_runs
    output = args.output or args.dataset_dir / "quality_report.json"
    write_json_atomic(output, report)
    print(
        f"Dataset quality: accepted={report['accepted_run_count']}/"
        f"{report['run_count']}, rejected={report['rejected_run_count']}"
    )
    print(f"Report: {output.resolve()}")
    if report["status"] != "completed":
        raise SystemExit(3)
    if args.fail_if_rejected and report["rejected_run_count"]:
        raise SystemExit(2)


def validate_args(args: argparse.Namespace) -> None:
    for name in ("max_teleport_rate", "min_actuated_detector_coverage"):
        value = float(getattr(args, name))
        if not 0.0 <= value <= 1.0:
            raise SystemExit(f"--{name.replace('_', '-')} must be between 0 and 1")
    if args.max_emergency_brakes_per_1000 < 0 or args.max_collisions < 0:
        raise SystemExit("Quality thresholds cannot be negative")
    if args.min_departed_vehicles < 1:
        raise SystemExit("--min-departed-vehicles must be positive")
    if args.expected_runs is not None and args.expected_runs < 1:
        raise SystemExit("--expected-runs must be positive")


def write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


if __name__ == "__main__":
    main()
