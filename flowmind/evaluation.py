from __future__ import annotations

import csv
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import fmean, stdev
from typing import Iterable, Mapping, Sequence

from scipy import stats

from .config import CONTROL_MODES, FLOWMIND_MODE


EVALUATION_SCHEMA_VERSION = 1
DEFAULT_MIN_PAIRS = 30
DEFAULT_MAX_PAIRS = 50


@dataclass(frozen=True)
class MetricSpec:
    key: str
    label: str
    direction: str
    gate_tolerance: float
    gate_scale: str = "relative"
    emergency_only: bool = False


METRIC_SPECS = (
    MetricSpec(
        "average_waiting_time",
        "Average waiting time",
        "lower",
        0.05,
    ),
    MetricSpec("zone_outflow", "Zone outflow", "higher", 0.02),
    MetricSpec("stops_count", "Stops", "lower", 0.05),
    MetricSpec(
        "blocked_outgoing_share",
        "Blocked outgoing share",
        "lower",
        0.02,
        gate_scale="absolute",
    ),
    MetricSpec(
        "emergency_eta",
        "Emergency ETA",
        "lower",
        0.10,
        emergency_only=True,
    ),
)

PAIR_INVARIANT_FIELDS = (
    "evaluation_id",
    "evaluation_replicate",
    "seed",
    "scenario_sha256",
    "network_sha256",
    "route_files_sha256",
    "zone_sha256",
    "pair_config_sha256",
    "demand_vehicles_per_hour",
    "demand_duration_seconds",
    "emergency_route_sha256",
)
PAIR_REQUIRED_FIELDS = (
    "evaluation_id",
    "evaluation_replicate",
    "seed",
    "scenario_sha256",
    "network_sha256",
    "route_files_sha256",
    "zone_sha256",
    "pair_config_sha256",
    "demand_vehicles_per_hour",
    "demand_duration_seconds",
)


def analyze_paired_summaries(
    summaries: Sequence[Mapping[str, object]],
    *,
    contender: str = FLOWMIND_MODE,
    baselines: Sequence[str] | None = None,
    min_pairs: int = DEFAULT_MIN_PAIRS,
    max_pairs: int = DEFAULT_MAX_PAIRS,
) -> dict[str, object]:
    """Validate full paired runs and calculate paired statistics and gates.

    A pair is admitted only when every requested mode is present exactly once
    and the seed/config/demand/network/emergency-route provenance matches.
    Missing metric values remain missing and are never converted to zero.
    """

    if min_pairs <= 0:
        raise ValueError("min_pairs must be positive")
    if max_pairs < min_pairs:
        raise ValueError("max_pairs cannot be lower than min_pairs")
    if contender not in CONTROL_MODES:
        raise ValueError(f"Unknown contender mode: {contender}")
    selected_baselines = tuple(
        baselines
        if baselines is not None
        else (mode for mode in CONTROL_MODES if mode != contender)
    )
    if not selected_baselines:
        raise ValueError("At least one baseline is required")
    if contender in selected_baselines:
        raise ValueError("The contender cannot also be a baseline")
    if len(set(selected_baselines)) != len(selected_baselines):
        raise ValueError("Baseline modes must be unique")
    unknown = set(selected_baselines) - set(CONTROL_MODES)
    if unknown:
        raise ValueError("Unknown baseline mode(s): " + ", ".join(sorted(unknown)))
    required_modes = (*selected_baselines, contender)

    grouped: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    orphaned: list[dict[str, object]] = []
    for index, summary in enumerate(summaries):
        pair_id = str(summary.get("evaluation_pair_id") or "").strip()
        if not pair_id:
            orphaned.append(
                {
                    "input_index": index,
                    "reason": "missing evaluation_pair_id",
                }
            )
            continue
        grouped[pair_id].append(summary)

    valid_pairs: list[dict[str, object]] = []
    valid_rows: dict[str, dict[str, Mapping[str, object]]] = {}
    invalid_pairs: list[dict[str, object]] = []
    for pair_id in sorted(grouped):
        rows = grouped[pair_id]
        mode_counts = Counter(str(row.get("mode") or "") for row in rows)
        errors: list[str] = []
        missing_modes = [mode for mode in required_modes if mode_counts[mode] == 0]
        duplicate_modes = [
            mode for mode in required_modes if mode_counts[mode] > 1
        ]
        unexpected_modes = sorted(set(mode_counts) - set(required_modes))
        if missing_modes:
            errors.append("missing modes: " + ", ".join(missing_modes))
        if duplicate_modes:
            errors.append("duplicate modes: " + ", ".join(duplicate_modes))
        if unexpected_modes:
            errors.append("unexpected modes: " + ", ".join(unexpected_modes))
        by_mode = {
            str(row.get("mode")): row
            for row in rows
            if str(row.get("mode")) in required_modes
        }
        if not errors:
            errors.extend(_pair_consistency_errors(by_mode, required_modes))
        if errors:
            invalid_pairs.append(
                {
                    "pair_id": pair_id,
                    "errors": errors,
                    "mode_counts": dict(sorted(mode_counts.items())),
                }
            )
            continue
        representative = by_mode[contender]
        valid_rows[pair_id] = by_mode
        valid_pairs.append(
            {
                "pair_id": pair_id,
                "replicate": representative.get("evaluation_replicate"),
                "seed": representative.get("seed"),
                "pair_config_sha256": representative.get("pair_config_sha256"),
                "emergency_route_sha256": representative.get(
                    "emergency_route_sha256"
                ),
            }
        )

    valid_pair_count = len(valid_pairs)
    if valid_pair_count < min_pairs:
        pair_count_status = "below_minimum"
    elif valid_pair_count > max_pairs:
        pair_count_status = "above_maximum"
    else:
        pair_count_status = "complete"
    emergency_evaluated = any(
        bool(pair["emergency_route_sha256"]) for pair in valid_pairs
    )

    comparisons: dict[str, object] = {}
    gate_rows: list[dict[str, object]] = []
    for baseline in selected_baselines:
        metrics: dict[str, object] = {}
        gates: dict[str, object] = {}
        for spec in METRIC_SPECS:
            paired = _metric_pairs(valid_rows, baseline, contender, spec.key)
            metric_stats = _paired_metric_statistics(
                paired,
                direction=spec.direction,
                total_pairs=valid_pair_count,
            )
            metrics[spec.key] = metric_stats
            if spec.emergency_only and not emergency_evaluated:
                gate: dict[str, object] = {
                    "status": "not_applicable",
                    "reason": "No paired emergency route was configured",
                    "sample_count": 0,
                    "required_pairs": min_pairs,
                    "tolerance": spec.gate_tolerance,
                    "scale": spec.gate_scale,
                }
            else:
                degradations = [
                    _degradation(
                        baseline_value,
                        contender_value,
                        direction=spec.direction,
                        scale=spec.gate_scale,
                    )
                    for baseline_value, contender_value in paired
                ]
                gate = _regression_gate(
                    degradations,
                    tolerance=spec.gate_tolerance,
                    min_pairs=min_pairs,
                    scale=spec.gate_scale,
                )
            gates[spec.key] = gate
            gate_rows.append(
                {
                    "baseline": baseline,
                    "contender": contender,
                    "metric": spec.key,
                    **gate,
                }
            )
        comparisons[baseline] = {
            "baseline": baseline,
            "contender": contender,
            "metrics": metrics,
            "regression_gates": gates,
        }

    applicable_gate_statuses = [
        str(row["status"])
        for row in gate_rows
        if row["status"] != "not_applicable"
    ]
    if orphaned or invalid_pairs:
        overall_status = "invalid_pairs"
    elif pair_count_status != "complete":
        overall_status = pair_count_status
    elif not emergency_evaluated:
        overall_status = "partial_missing_emergency"
    elif any(status == "fail" for status in applicable_gate_statuses):
        overall_status = "regression"
    elif any(status != "pass" for status in applicable_gate_statuses):
        overall_status = "insufficient_data"
    else:
        overall_status = "pass"

    mode_run_counts = Counter(str(summary.get("mode") or "") for summary in summaries)
    return {
        "evaluation_schema_version": EVALUATION_SCHEMA_VERSION,
        "contender": contender,
        "baselines": list(selected_baselines),
        "required_modes": list(required_modes),
        "input_run_count": len(summaries),
        "mode_run_counts": dict(sorted(mode_run_counts.items())),
        "valid_pair_count": valid_pair_count,
        "invalid_pair_count": len(invalid_pairs),
        "orphaned_run_count": len(orphaned),
        "minimum_required_pairs": min_pairs,
        "maximum_supported_pairs": max_pairs,
        "pair_count_status": pair_count_status,
        "emergency_route_evaluated": emergency_evaluated,
        "overall_status": overall_status,
        "valid_pairs": valid_pairs,
        "invalid_pairs": invalid_pairs,
        "orphaned_runs": orphaned,
        "comparisons": comparisons,
    }


def write_evaluation_report(
    report: Mapping[str, object],
    output_dir: Path,
) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "evaluation_report.json"
    metrics_path = output_dir / "paired_metrics.csv"
    gates_path = output_dir / "regression_gates.csv"
    pairs_path = output_dir / "validated_pairs.csv"

    _write_json_atomic(json_path, report)
    metric_rows: list[dict[str, object]] = []
    gate_rows: list[dict[str, object]] = []
    comparisons = report.get("comparisons", {})
    if isinstance(comparisons, Mapping):
        for baseline, raw_comparison in comparisons.items():
            if not isinstance(raw_comparison, Mapping):
                continue
            metrics = raw_comparison.get("metrics", {})
            if isinstance(metrics, Mapping):
                for metric, raw_values in metrics.items():
                    if isinstance(raw_values, Mapping):
                        metric_rows.append(
                            {
                                "baseline": baseline,
                                "contender": report.get("contender"),
                                "metric": metric,
                                **raw_values,
                            }
                        )
            gates = raw_comparison.get("regression_gates", {})
            if isinstance(gates, Mapping):
                for metric, raw_values in gates.items():
                    if isinstance(raw_values, Mapping):
                        gate_rows.append(
                            {
                                "baseline": baseline,
                                "contender": report.get("contender"),
                                "metric": metric,
                                **raw_values,
                            }
                        )
    _write_csv(metrics_path, metric_rows)
    _write_csv(gates_path, gate_rows)
    valid_pairs = report.get("valid_pairs", [])
    pair_rows = [dict(row) for row in valid_pairs if isinstance(row, Mapping)]
    _write_csv(pairs_path, pair_rows)
    return {
        "evaluation_report": str(json_path),
        "paired_metrics": str(metrics_path),
        "regression_gates": str(gates_path),
        "validated_pairs": str(pairs_path),
    }


def load_summary_files(paths: Iterable[Path]) -> list[dict[str, object]]:
    summaries: list[dict[str, object]] = []
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"Cannot read summary {path}: {error}") from error
        if not isinstance(payload, dict):
            raise ValueError(f"Summary must contain a JSON object: {path}")
        summaries.append(payload)
    return summaries


def _pair_consistency_errors(
    by_mode: Mapping[str, Mapping[str, object]],
    modes: Sequence[str],
) -> list[str]:
    reference = by_mode[modes[0]]
    errors: list[str] = []
    for field in PAIR_REQUIRED_FIELDS:
        missing = [
            mode
            for mode in modes
            if by_mode[mode].get(field) in (None, "")
        ]
        if missing:
            errors.append(f"{field} missing for: " + ", ".join(missing))
    for field in PAIR_INVARIANT_FIELDS:
        expected = reference.get(field)
        mismatched = [
            mode for mode in modes[1:] if by_mode[mode].get(field) != expected
        ]
        if mismatched:
            errors.append(
                f"{field} differs for: " + ", ".join(mismatched)
            )
    return errors


def _metric_pairs(
    valid_rows: Mapping[str, Mapping[str, Mapping[str, object]]],
    baseline: str,
    contender: str,
    key: str,
) -> list[tuple[float, float]]:
    values: list[tuple[float, float]] = []
    for pair_id in sorted(valid_rows):
        baseline_value = _finite_float(valid_rows[pair_id][baseline].get(key))
        contender_value = _finite_float(valid_rows[pair_id][contender].get(key))
        if baseline_value is None or contender_value is None:
            continue
        values.append((baseline_value, contender_value))
    return values


def _paired_metric_statistics(
    pairs: Sequence[tuple[float, float]],
    *,
    direction: str,
    total_pairs: int,
) -> dict[str, object]:
    if not pairs:
        return {
            "sample_count": 0,
            "missing_pair_count": total_pairs,
            "baseline_mean": None,
            "contender_mean": None,
            "mean_delta_contender_minus_baseline": None,
            "delta_ci95_lower": None,
            "delta_ci95_upper": None,
            "mean_improvement": None,
            "paired_t_pvalue_two_sided": None,
            "cohens_dz": None,
            "direction": direction,
        }
    baseline_values = [item[0] for item in pairs]
    contender_values = [item[1] for item in pairs]
    deltas = [contender - baseline for baseline, contender in pairs]
    ci_lower, ci_upper = _confidence_interval(deltas)
    delta_mean = fmean(deltas)
    improvement = -delta_mean if direction == "lower" else delta_mean
    return {
        "sample_count": len(pairs),
        "missing_pair_count": max(total_pairs - len(pairs), 0),
        "baseline_mean": _rounded(fmean(baseline_values)),
        "contender_mean": _rounded(fmean(contender_values)),
        "mean_delta_contender_minus_baseline": _rounded(delta_mean),
        "delta_ci95_lower": _rounded(ci_lower),
        "delta_ci95_upper": _rounded(ci_upper),
        "mean_improvement": _rounded(improvement),
        "paired_t_pvalue_two_sided": _rounded(_paired_pvalue(deltas), 8),
        "cohens_dz": _rounded(_cohens_dz(deltas)),
        "direction": direction,
    }


def _regression_gate(
    degradations: Sequence[float],
    *,
    tolerance: float,
    min_pairs: int,
    scale: str,
) -> dict[str, object]:
    if len(degradations) < min_pairs:
        status = "insufficient_data"
        reason = f"{len(degradations)} paired values; {min_pairs} required"
        ci_lower = ci_upper = None
        mean = fmean(degradations) if degradations else None
    else:
        ci_lower, ci_upper = _confidence_interval(degradations)
        mean = fmean(degradations)
        status = "pass" if ci_upper <= tolerance else "fail"
        reason = (
            "Upper 95% degradation bound is within tolerance"
            if status == "pass"
            else "Upper 95% degradation bound exceeds tolerance"
        )
    return {
        "status": status,
        "reason": reason,
        "sample_count": len(degradations),
        "required_pairs": min_pairs,
        "tolerance": tolerance,
        "scale": scale,
        "mean_degradation": _rounded(mean),
        "degradation_ci95_lower": _rounded(ci_lower),
        "degradation_ci95_upper": _rounded(ci_upper),
    }


def _degradation(
    baseline: float,
    contender: float,
    *,
    direction: str,
    scale: str,
) -> float:
    raw = contender - baseline if direction == "lower" else baseline - contender
    if scale == "absolute":
        return raw
    return raw / max(abs(baseline), 1.0)


def _confidence_interval(values: Sequence[float]) -> tuple[float, float]:
    mean = fmean(values)
    if len(values) < 2:
        return mean, mean
    sample_std = stdev(values)
    if sample_std == 0:
        return mean, mean
    critical = float(stats.t.ppf(0.975, len(values) - 1))
    margin = critical * sample_std / math.sqrt(len(values))
    return mean - margin, mean + margin


def _paired_pvalue(deltas: Sequence[float]) -> float | None:
    if len(deltas) < 2:
        return None
    sample_std = stdev(deltas)
    if sample_std == 0:
        return 1.0 if deltas[0] == 0 else 0.0
    result = stats.ttest_1samp(deltas, popmean=0.0)
    value = float(result.pvalue)
    return value if math.isfinite(value) else None


def _cohens_dz(deltas: Sequence[float]) -> float | None:
    if len(deltas) < 2:
        return None
    sample_std = stdev(deltas)
    if sample_std == 0:
        return 0.0 if deltas[0] == 0 else None
    return fmean(deltas) / sample_std


def _finite_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        converted = float(value)
    except (TypeError, ValueError):
        return None
    return converted if math.isfinite(converted) else None


def _rounded(value: float | None, digits: int = 6) -> float | None:
    return round(value, digits) if value is not None else None


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _write_csv(path: Path, rows: Sequence[Mapping[str, object]]) -> None:
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        if not fieldnames:
            return
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


__all__ = [
    "DEFAULT_MAX_PAIRS",
    "DEFAULT_MIN_PAIRS",
    "EVALUATION_SCHEMA_VERSION",
    "METRIC_SPECS",
    "MetricSpec",
    "analyze_paired_summaries",
    "load_summary_files",
    "write_evaluation_report",
]
