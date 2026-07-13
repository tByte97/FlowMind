from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flowmind.config import DEFAULT_QUEUE_MODEL_PATHS, PROJECT_ROOT
from flowmind.ml_approval import ML_APPROVAL_SCHEMA_VERSION
from flowmind.provenance import canonical_sha256, file_sha256


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Approve ML influence only after shadow and paired gates pass."
    )
    parser.add_argument("--shadow-dir", type=Path, required=True)
    parser.add_argument("--baseline-dir", type=Path, required=True)
    parser.add_argument("--control-dir", type=Path, required=True)
    parser.add_argument("--max-shadow-mae", type=float, default=2.0)
    parser.add_argument("--max-ood-rate", type=float, default=0.05)
    parser.add_argument(
        "--model",
        dest="models",
        type=Path,
        nargs="+",
        default=list(DEFAULT_QUEUE_MODEL_PATHS),
    )
    parser.add_argument(
        "--network",
        type=Path,
        default=PROJECT_ROOT / "simulation" / "rivne_area" / "osm.net.xml.gz",
    )
    parser.add_argument(
        "--zone",
        type=Path,
        default=PROJECT_ROOT / "simulation" / "rivne_area" / "central_zone.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "models" / "queue_control_approval.json",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.max_shadow_mae < 0 or not 0 <= args.max_ood_rate <= 1:
        raise ValueError("Invalid MAE/OOD approval threshold")
    baseline = _read_json(args.baseline_dir / "evaluation_report.json")
    control = _read_json(args.control_dir / "evaluation_report.json")
    control_manifest = _read_json(
        args.control_dir / "evaluation_manifest.json"
    )
    if baseline.get("overall_status") != "pass":
        raise SystemExit("Baseline paired evaluation did not pass")
    if control.get("overall_status") != "pass":
        raise SystemExit("ML-control paired evaluation did not pass")
    evaluated_control = control_manifest.get("control_config")
    if not isinstance(evaluated_control, dict):
        raise SystemExit("ML-control manifest has no ControlConfig contract")
    shadow_summaries = _read_json(args.shadow_dir / "summaries.json")
    if not isinstance(shadow_summaries, list):
        raise SystemExit("Shadow summaries.json must contain a list")
    mae_values = [
        float(item["queue_forecast_shadow_mae"])
        for item in shadow_summaries
        if isinstance(item, dict)
        and item.get("mode") == "flowmind"
        and item.get("queue_forecast_shadow_mae") is not None
    ]
    predictions = sum(
        int(item.get("queue_forecast_predictions", 0))
        for item in shadow_summaries
        if isinstance(item, dict) and item.get("mode") == "flowmind"
    )
    ood = sum(
        int(item.get("queue_forecast_ood_predictions", 0))
        for item in shadow_summaries
        if isinstance(item, dict) and item.get("mode") == "flowmind"
    )
    if not mae_values or predictions <= 0:
        raise SystemExit("Shadow evaluation has no measured ML predictions")
    shadow_mae = sum(mae_values) / len(mae_values)
    ood_rate = ood / predictions
    if shadow_mae > args.max_shadow_mae:
        raise SystemExit(
            f"Shadow MAE {shadow_mae:.4f} exceeds {args.max_shadow_mae:.4f}"
        )
    if ood_rate > args.max_ood_rate:
        raise SystemExit(
            f"OOD rate {ood_rate:.4%} exceeds {args.max_ood_rate:.4%}"
        )
    model_paths = tuple(path.resolve() for path in args.models)
    missing = [str(path) for path in model_paths if not path.is_file()]
    if missing:
        raise SystemExit("Missing model artifacts: " + ", ".join(missing))
    payload = {
        "schema_version": ML_APPROVAL_SCHEMA_VERSION,
        "status": "approved",
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "shadow_evaluation": str(args.shadow_dir.resolve()),
        "baseline_evaluation": str(args.baseline_dir.resolve()),
        "control_evaluation": str(args.control_dir.resolve()),
        "shadow_mae": shadow_mae,
        "ood_rate": ood_rate,
        "network_sha256": file_sha256(args.network),
        "zone_sha256": file_sha256(args.zone),
        "model_paths": [str(path) for path in model_paths],
        "model_artifact_sha256": [file_sha256(path) for path in model_paths],
        "control_config_sha256": canonical_sha256(evaluated_control),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Queue control approved: {args.output}")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"Cannot read {path}: {error}") from error


if __name__ == "__main__":
    main()
