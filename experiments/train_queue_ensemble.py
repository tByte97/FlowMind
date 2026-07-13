from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Train the FlowMind 30s/60s/90s queue forecast ensemble."
    )
    parser.add_argument(
        "--dataset-dir",
        type=Path,
        default=PROJECT_ROOT / "results" / "dataset",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "results" / "trained_models",
    )
    parser.add_argument(
        "--horizons",
        type=int,
        nargs="+",
        default=(30, 60, 90),
    )
    parser.add_argument("--rows-per-file", type=int, default=5000)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--n-estimators", type=int, default=700)
    return parser


def training_command(
    *,
    dataset_dir: Path,
    output_dir: Path,
    horizon: int,
    rows_per_file: int,
    jobs: int,
    n_estimators: int,
) -> list[str]:
    return [
        sys.executable,
        "-u",
        str(PROJECT_ROOT / "experiments" / "train_queue_model.py"),
        "--dataset-dir",
        str(dataset_dir),
        "--target",
        f"target_queue_reduction_{horizon}s",
        "--output",
        str(output_dir / f"queue_lgbm_{horizon}s_decision.joblib"),
        "--rows-per-file",
        str(rows_per_file),
        "--jobs",
        str(jobs),
        "--n-estimators",
        str(n_estimators),
    ]


def main() -> None:
    args = build_parser().parse_args()
    if any(horizon <= 0 for horizon in args.horizons):
        raise ValueError("--horizons values must be positive")
    if args.rows_per_file <= 0:
        raise ValueError("--rows-per-file must be positive")
    if args.jobs <= 0:
        raise ValueError("--jobs must be positive")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    completed: list[int] = []
    for horizon in args.horizons:
        print(f"Training queue forecast model for {horizon}s...")
        subprocess.run(
            training_command(
                dataset_dir=args.dataset_dir,
                output_dir=args.output_dir,
                horizon=horizon,
                rows_per_file=args.rows_per_file,
                jobs=args.jobs,
                n_estimators=args.n_estimators,
            ),
            cwd=PROJECT_ROOT,
            check=True,
        )
        completed.append(horizon)

    manifest = {
        "status": "completed",
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "dataset_dir": str(args.dataset_dir),
        "rows_per_file": args.rows_per_file,
        "jobs": args.jobs,
        "n_estimators": args.n_estimators,
        "horizons": completed,
    }
    manifest_path = args.output_dir / "ensemble_training.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Queue forecast ensemble complete: {manifest_path}")


if __name__ == "__main__":
    main()
