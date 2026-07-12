from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flowmind.config import (
    CONTROL_MODES,
    DEFAULT_QUEUE_MODEL_PATHS,
    PROJECT_ROOT,
    ControlConfig,
    RunConfig,
)
from flowmind.emergency_vehicle import EmergencyVehicleConfig, load_emergency_config
from flowmind.evaluation import (
    DEFAULT_MAX_PAIRS,
    DEFAULT_MIN_PAIRS,
    analyze_paired_summaries,
    write_evaluation_report,
)
from flowmind.experiment import run_experiment


EVALUATION_RUNNER_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class EvaluationPairRequest:
    evaluation_id: str
    evaluation_dir: Path
    replicate: int
    seed: int
    duration: int
    zone_size: int
    config_path: Path
    zone_path: Path
    scenario_name: str | None
    emergency: EmergencyVehicleConfig | None
    control: ControlConfig
    queue_model_paths: tuple[Path, ...]
    resume: bool


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run reproducible paired FlowMind evaluation across all four "
            "control modes"
        )
    )
    parser.add_argument("--replicates", type=int, default=DEFAULT_MIN_PAIRS)
    parser.add_argument("--duration", type=int, default=1800)
    parser.add_argument("--seed-start", type=int, default=42)
    parser.add_argument("--zone-size", type=int, default=20)
    parser.add_argument(
        "--sensor-range",
        type=float,
        default=ControlConfig().sensor_range_meters,
    )
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
        "--without-emergency",
        action="store_true",
        help="Skip the emergency ETA gate (the resulting report is partial).",
    )
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=PROJECT_ROOT / "results" / "evaluation",
    )
    parser.add_argument("--evaluation-id")
    parser.add_argument("--scenario-name")
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of independent paired SUMO workers (1–8).",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Reuse validated completed summaries in an existing evaluation.",
    )
    parser.add_argument(
        "--allow-small",
        action="store_true",
        help="Allow fewer than 30 pairs for smoke testing only.",
    )
    parser.add_argument(
        "--fail-on-regression",
        action="store_true",
        help="Return a non-zero exit code unless the full report passes.",
    )
    parser.add_argument(
        "--queue-model",
        dest="queue_models",
        type=Path,
        nargs="+",
        action="extend",
    )
    parser.add_argument("--no-queue-model", action="store_true")
    parser.add_argument(
        "--enable-queue-control",
        action="store_true",
        help="Enable accepted ML predictions; default evaluation is shadow mode.",
    )
    return parser


def validate_replicate_count(replicates: int, allow_small: bool) -> None:
    if replicates <= 0:
        raise ValueError("replicates must be positive")
    if allow_small:
        if replicates > DEFAULT_MAX_PAIRS:
            raise ValueError(f"replicates cannot exceed {DEFAULT_MAX_PAIRS}")
        return
    if not DEFAULT_MIN_PAIRS <= replicates <= DEFAULT_MAX_PAIRS:
        raise ValueError(
            f"Full evaluation requires {DEFAULT_MIN_PAIRS}–"
            f"{DEFAULT_MAX_PAIRS} paired replicates"
        )


def main() -> None:
    args = build_parser().parse_args()
    validate_replicate_count(args.replicates, args.allow_small)
    if args.duration <= 0:
        raise ValueError("duration must be positive")
    if not 1 <= args.workers <= 8:
        raise ValueError("workers must be between 1 and 8")

    evaluation_id = args.evaluation_id or datetime.now(timezone.utc).strftime(
        "eval_%Y%m%dT%H%M%SZ"
    )
    evaluation_dir = args.results_dir.resolve() / evaluation_id
    if evaluation_dir.exists() and not args.resume:
        raise FileExistsError(
            f"Evaluation directory already exists: {evaluation_dir}; "
            "use --resume or choose another --evaluation-id"
        )
    evaluation_dir.mkdir(parents=True, exist_ok=True)

    emergency = (
        None
        if args.without_emergency
        else load_emergency_config(args.emergency_config)
    )
    if emergency is not None and emergency.depart_time >= args.duration:
        raise ValueError(
            "Evaluation duration must extend beyond emergency departure time"
        )
    queue_model_paths = (
        ()
        if args.no_queue_model
        else tuple(args.queue_models or DEFAULT_QUEUE_MODEL_PATHS)
    )
    control = ControlConfig(
        sensor_range_meters=args.sensor_range,
        queue_forecast_shadow_mode=not args.enable_queue_control,
    )
    manifest_path = evaluation_dir / "evaluation_manifest.json"
    manifest = _initial_manifest(
        evaluation_id=evaluation_id,
        args=args,
        evaluation_dir=evaluation_dir,
        emergency_enabled=emergency is not None,
        queue_model_paths=queue_model_paths,
    )
    if args.resume and manifest_path.is_file():
        _validate_resume_manifest(manifest_path, manifest)
    _write_json_atomic(manifest_path, manifest)

    summaries_by_replicate: dict[int, list[dict[str, object]]] = {}
    try:
        requests = [
            EvaluationPairRequest(
                evaluation_id=evaluation_id,
                evaluation_dir=evaluation_dir,
                replicate=replicate,
                seed=args.seed_start + replicate - 1,
                duration=args.duration,
                zone_size=args.zone_size,
                config_path=args.config,
                zone_path=args.zone,
                scenario_name=args.scenario_name,
                emergency=emergency,
                control=control,
                queue_model_paths=queue_model_paths,
                resume=args.resume,
            )
            for replicate in range(1, args.replicates + 1)
        ]
        if args.workers == 1:
            for request in requests:
                replicate, pair_summaries = _run_pair(request)
                summaries_by_replicate[replicate] = pair_summaries
                _checkpoint(
                    evaluation_dir,
                    manifest_path,
                    manifest,
                    summaries_by_replicate,
                )
        else:
            with ProcessPoolExecutor(max_workers=args.workers) as executor:
                futures = {
                    executor.submit(_run_pair, request): request.replicate
                    for request in requests
                }
                for future in as_completed(futures):
                    replicate, pair_summaries = future.result()
                    summaries_by_replicate[replicate] = pair_summaries
                    print(
                        f"completed pair {replicate}/{args.replicates}",
                        flush=True,
                    )
                    _checkpoint(
                        evaluation_dir,
                        manifest_path,
                        manifest,
                        summaries_by_replicate,
                    )

        summaries = _flatten_summaries(summaries_by_replicate)

        minimum = 1 if args.allow_small else DEFAULT_MIN_PAIRS
        report = analyze_paired_summaries(
            summaries,
            min_pairs=minimum,
            max_pairs=DEFAULT_MAX_PAIRS,
        )
        report["validation_profile"] = (
            "smoke" if args.allow_small else "full_30_to_50_pairs"
        )
        report["evaluation_id"] = evaluation_id
        artifacts = write_evaluation_report(report, evaluation_dir)
        manifest.update(
            {
                "status": "completed",
                "completed_pairs": args.replicates,
                "completed_runs": len(summaries),
                "overall_status": report["overall_status"],
                "artifacts": artifacts,
            }
        )
        _write_json_atomic(manifest_path, manifest)
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        if args.fail_on_regression and report["overall_status"] != "pass":
            raise SystemExit(2)
    except Exception as error:
        manifest["status"] = "failed"
        manifest["error"] = f"{type(error).__name__}: {error}"
        _write_json_atomic(manifest_path, manifest)
        raise


def emergency_route_edges(summary: Mapping[str, object]) -> tuple[str, ...]:
    raw = summary.get("emergency_route_edges")
    if isinstance(raw, str):
        return tuple(edge for edge in raw.split() if edge)
    if isinstance(raw, (list, tuple)):
        return tuple(str(edge) for edge in raw if str(edge))
    return ()


def _initial_manifest(
    *,
    evaluation_id: str,
    args: argparse.Namespace,
    evaluation_dir: Path,
    emergency_enabled: bool,
    queue_model_paths: tuple[Path, ...],
) -> dict[str, object]:
    return {
        "evaluation_runner_schema_version": EVALUATION_RUNNER_SCHEMA_VERSION,
        "evaluation_id": evaluation_id,
        "status": "running",
        "modes": list(CONTROL_MODES),
        "replicates": args.replicates,
        "duration": args.duration,
        "seed_start": args.seed_start,
        "seeds": [args.seed_start + index for index in range(args.replicates)],
        "zone_size": args.zone_size,
        "sensor_range_meters": args.sensor_range,
        "sumo_config": str(args.config.resolve()),
        "zone_config": str(args.zone.resolve()),
        "scenario_name": args.scenario_name,
        "emergency_enabled": emergency_enabled,
        "emergency_config": (
            str(args.emergency_config.resolve()) if emergency_enabled else None
        ),
        "emergency_route_policy": "fixed_within_each_pair",
        "queue_forecast_shadow_mode": not args.enable_queue_control,
        "queue_model_paths": [str(path.resolve()) for path in queue_model_paths],
        "live_telemetry": False,
        "workers": args.workers,
        "evaluation_dir": str(evaluation_dir),
        "completed_pairs": 0,
        "completed_runs": 0,
    }


def _validate_resume_manifest(
    manifest_path: Path,
    requested: Mapping[str, object],
) -> None:
    try:
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Cannot resume invalid manifest: {error}") from error
    invariant_fields = (
        "evaluation_runner_schema_version",
        "evaluation_id",
        "modes",
        "replicates",
        "duration",
        "seed_start",
        "seeds",
        "zone_size",
        "sensor_range_meters",
        "sumo_config",
        "zone_config",
        "scenario_name",
        "emergency_enabled",
        "emergency_config",
        "emergency_route_policy",
        "queue_forecast_shadow_mode",
        "queue_model_paths",
    )
    mismatched = [
        field
        for field in invariant_fields
        if existing.get(field) != requested.get(field)
    ]
    if mismatched:
        raise ValueError(
            "Resume settings differ from the existing manifest: "
            + ", ".join(mismatched)
        )


def _load_resumable_summary(
    path: Path,
    *,
    evaluation_id: str,
    pair_id: str,
    replicate: int,
    seed: int,
    mode: str,
) -> dict[str, object] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Cannot resume summary {path}: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"Cannot resume non-object summary: {path}")
    expected = {
        "evaluation_id": evaluation_id,
        "evaluation_pair_id": pair_id,
        "evaluation_replicate": replicate,
        "seed": seed,
        "mode": mode,
    }
    mismatched = [
        key for key, value in expected.items() if payload.get(key) != value
    ]
    if mismatched:
        raise ValueError(
            f"Resume summary metadata mismatch in {path}: "
            + ", ".join(mismatched)
        )
    return payload


def _run_pair(
    request: EvaluationPairRequest,
) -> tuple[int, list[dict[str, object]]]:
    pair_id = f"{request.evaluation_id}:pair:{request.replicate:03d}"
    pair_dir = request.evaluation_dir / (
        f"pair_{request.replicate:03d}_seed_{request.seed}"
    )
    fixed_route: tuple[str, ...] = ()
    summaries: list[dict[str, object]] = []
    print(
        f"[{request.replicate}] seed={request.seed} pair={pair_id}",
        flush=True,
    )
    for mode in CONTROL_MODES:
        mode_dir = pair_dir / mode
        summary_path = mode_dir / f"{mode}_summary.json"
        summary = (
            _load_resumable_summary(
                summary_path,
                evaluation_id=request.evaluation_id,
                pair_id=pair_id,
                replicate=request.replicate,
                seed=request.seed,
                mode=mode,
            )
            if request.resume
            else None
        )
        if summary is None:
            print(f"  [{request.replicate}] running {mode}", flush=True)
            summary = run_experiment(
                RunConfig(
                    mode=mode,
                    duration=request.duration,
                    seed=request.seed,
                    zone_size=request.zone_size,
                    config_path=request.config_path,
                    zone_path=request.zone_path,
                    results_dir=mode_dir,
                    scenario_name=request.scenario_name,
                    emergency=request.emergency,
                    control=request.control,
                    queue_model_paths=request.queue_model_paths,
                    evaluation_id=request.evaluation_id,
                    evaluation_pair_id=pair_id,
                    evaluation_replicate=request.replicate,
                    fixed_emergency_route_edges=fixed_route,
                    allow_emergency_reroute=False,
                    enable_live_telemetry=False,
                )
            )
        else:
            print(f"  [{request.replicate}] resumed {mode}", flush=True)

        route = emergency_route_edges(summary)
        if request.emergency is not None:
            if not route:
                raise RuntimeError(
                    f"{pair_id}/{mode} did not produce an emergency route"
                )
            if fixed_route and route != fixed_route:
                raise RuntimeError(
                    f"{pair_id}/{mode} used a different emergency route"
                )
            fixed_route = route
        summaries.append(summary)
    return request.replicate, summaries


def _checkpoint(
    evaluation_dir: Path,
    manifest_path: Path,
    manifest: dict[str, object],
    summaries_by_replicate: Mapping[int, list[dict[str, object]]],
) -> None:
    summaries = _flatten_summaries(summaries_by_replicate)
    _write_json_atomic(evaluation_dir / "summaries.json", summaries)
    manifest["completed_pairs"] = len(summaries_by_replicate)
    manifest["completed_runs"] = len(summaries)
    _write_json_atomic(manifest_path, manifest)


def _flatten_summaries(
    summaries_by_replicate: Mapping[int, list[dict[str, object]]],
) -> list[dict[str, object]]:
    return [
        summary
        for replicate in sorted(summaries_by_replicate)
        for summary in summaries_by_replicate[replicate]
    ]


def _write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


if __name__ == "__main__":
    main()
