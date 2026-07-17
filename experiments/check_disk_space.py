from __future__ import annotations

import argparse
import shutil
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Fail before a disk-heavy FlowMind job.")
    parser.add_argument("--path", type=Path, required=True)
    parser.add_argument("--min-free-gb", type=float, required=True)
    args = parser.parse_args()
    args.path.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(args.path)
    free_gb = usage.free / 1024**3
    print(f"Disk preflight: {free_gb:.2f} GiB free at {args.path}")
    if free_gb < args.min_free_gb:
        raise SystemExit(
            f"Disk preflight failed: need {args.min_free_gb:.2f} GiB, "
            f"have {free_gb:.2f} GiB. Clean old images/build cache first."
        )


if __name__ == "__main__":
    main()
