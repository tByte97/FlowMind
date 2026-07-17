from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flowmind.config import PROJECT_ROOT, ControlConfig


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the five pre-training FlowMind graph-mask ablations."
    )
    parser.add_argument("--replicates", type=int, default=10)
    parser.add_argument("--duration", type=int, default=900)
    parser.add_argument("--seed-start", type=int, default=42)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=PROJECT_ROOT / "results" / "ablation",
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def ablation_variants(base: ControlConfig | None = None) -> dict[str, ControlConfig]:
    base = base or ControlConfig()
    return {
        "graph_penalty_only": replace(
            base,
            physical_hard_mask_enabled=False,
            graph_hard_mask_enabled=False,
            throughput_fallback_enabled=False,
        ),
        "physical_confirmed_only": replace(
            base,
            physical_hard_mask_enabled=True,
            graph_hard_mask_enabled=False,
            throughput_fallback_enabled=False,
        ),
        "graph_hysteresis_3_samples": replace(
            base,
            physical_hard_mask_enabled=True,
            graph_hard_mask_enabled=True,
            graph_hard_mask_confirmation_samples=3,
            graph_hard_mask_release_samples=3,
            max_graph_masked_movement_share=0.25,
            throughput_fallback_enabled=False,
        ),
        "per_tls_local_fallback": replace(
            base,
            physical_hard_mask_enabled=True,
            graph_hard_mask_enabled=False,
            throughput_fallback_enabled=True,
        ),
        "legacy_aggressive_control": replace(
            base,
            physical_hard_mask_enabled=True,
            graph_hard_mask_enabled=True,
            spillback_hard_gate_release_probability=0.84,
            graph_hard_mask_confirmation_samples=1,
            graph_hard_mask_release_samples=1,
            max_graph_masked_movement_share=1.0,
            throughput_fallback_enabled=False,
        ),
    }


def main() -> None:
    args = build_parser().parse_args()
    if not 1 <= args.replicates <= 50:
        raise SystemExit("--replicates must be between 1 and 50")
    args.results_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, object] = {
        "status": "running",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "replicates": args.replicates,
        "duration": args.duration,
        "seed_start": args.seed_start,
        "variants": {},
    }
    for name, control in ablation_variants().items():
        config_path = args.results_dir / f"{name}_control.json"
        write_json(config_path, {"control_config": asdict(control)})
        evaluation_id = f"ablation_{name}"
        command = [
            sys.executable,
            "-u",
            str(PROJECT_ROOT / "experiments" / "run_evaluation.py"),
            "--replicates",
            str(args.replicates),
            "--duration",
            str(args.duration),
            "--seed-start",
            str(args.seed_start),
            "--workers",
            str(args.workers),
            "--allow-small",
            "--without-emergency",
            "--no-queue-model",
            "--allow-incomplete-actuated-detectors",
            "--evaluation-id",
            evaluation_id,
            "--results-dir",
            str(args.results_dir),
            "--control-config",
            str(config_path),
        ]
        if args.resume:
            command.append("--resume")
        manifest["variants"][name] = {
            "control_config": str(config_path),
            "evaluation_id": evaluation_id,
            "command": command,
        }
        write_json(args.results_dir / "ablation_manifest.json", manifest)
        print(f"Ablation variant: {name}", flush=True)
        if not args.dry_run:
            subprocess.run(command, cwd=PROJECT_ROOT, check=True)
    manifest["status"] = "planned" if args.dry_run else "completed"
    manifest["completed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    write_json(args.results_dir / "ablation_manifest.json", manifest)


def write_json(path: Path, payload: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


if __name__ == "__main__":
    main()
