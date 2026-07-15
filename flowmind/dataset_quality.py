from __future__ import annotations

import csv
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


TELEPORT_RE = re.compile(r"Teleporting vehicle .*?\(([^)]+)\)", re.IGNORECASE)
DETECTOR_RE = re.compile(
    r"At actuated tlLogic '([^']+)', linkIndex ([0-9,]+) has no controlling detector",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DatasetQualityThresholds:
    max_teleport_rate: float = 0.05
    max_emergency_brakes_per_1000: float = 20.0
    max_collisions: int = 0
    min_actuated_detector_coverage: float = 1.0
    min_departed_vehicles: int = 1


def audit_dataset(
    dataset_dir: Path,
    thresholds: DatasetQualityThresholds,
) -> dict[str, Any]:
    dataset_dir = dataset_dir.resolve()
    summaries_dir = dataset_dir / "summaries"
    samples_dir = dataset_dir / "samples"
    summary_paths = sorted(summaries_dir.glob("*/*_summary.json"))
    controlled_links = discover_controlled_links(samples_dir)
    rows = [
        audit_run(summary_path, samples_dir, controlled_links, thresholds)
        for summary_path in summary_paths
    ]
    accepted = [row for row in rows if row["accepted"]]
    rejected = [row for row in rows if not row["accepted"]]
    return {
        "schema_version": 1,
        "status": "completed",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dataset_dir": str(dataset_dir),
        "thresholds": asdict(thresholds),
        "controlled_link_count": len(controlled_links),
        "run_count": len(rows),
        "accepted_run_count": len(accepted),
        "rejected_run_count": len(rejected),
        "accepted_sample_files": [
            str(row["sample_file"]) for row in accepted if row.get("sample_file")
        ],
        "teleport_rate_distribution": distribution(
            float(row["teleport_rate"]) for row in rows
        ),
        "emergency_brakes_per_1000_distribution": distribution(
            float(row["emergency_brakes_per_1000"]) for row in rows
        ),
        "actuated_detector_coverage_distribution": distribution(
            float(row["actuated_detector_coverage"])
            for row in rows
            if row["mode"] == "sumo_actuated"
        ),
        "rejection_reasons": count_rejection_reasons(rejected),
        "runs": rows,
    }


def audit_run(
    summary_path: Path,
    samples_dir: Path,
    controlled_links: set[tuple[str, int]],
    thresholds: DatasetQualityThresholds,
) -> dict[str, Any]:
    run_id = summary_path.parent.name
    sample_path = samples_dir / f"{run_id}.csv"
    log_paths = sorted((summary_path.parent / "raw").glob("*_sumo.log"))
    reasons: list[str] = []
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        summary = {}
        reasons.append("invalid_summary")
    mode = str(summary.get("mode", infer_mode(run_id)))
    departed = safe_int(summary.get("departed_vehicles"))
    log_text = ""
    if log_paths:
        try:
            log_text = log_paths[0].read_text(encoding="utf-8", errors="replace")
        except OSError:
            reasons.append("unreadable_sumo_log")
    else:
        reasons.append("missing_sumo_log")

    teleport_reasons: dict[str, int] = {}
    for match in TELEPORT_RE.finditer(log_text):
        reason = normalize_teleport_reason(match.group(1))
        teleport_reasons[reason] = teleport_reasons.get(reason, 0) + 1
    teleport_count = sum(teleport_reasons.values())
    teleport_rate = teleport_count / departed if departed > 0 else 1.0
    emergency_brakes = log_text.lower().count("performs emergency braking")
    emergency_brakes_per_1000 = (
        emergency_brakes * 1000.0 / departed if departed > 0 else float("inf")
    )
    collision_count = count_collisions(log_text)
    missing_detector_links = parse_missing_detector_links(log_text)
    detector_denominator = len(controlled_links)
    detector_coverage = (
        max(0.0, 1.0 - len(missing_detector_links & controlled_links) / detector_denominator)
        if detector_denominator
        else 0.0
    )

    if not sample_path.is_file():
        reasons.append("missing_sample_file")
    if departed < thresholds.min_departed_vehicles:
        reasons.append("departed_below_minimum")
    if teleport_rate > thresholds.max_teleport_rate:
        reasons.append("teleport_rate_exceeded")
    if emergency_brakes_per_1000 > thresholds.max_emergency_brakes_per_1000:
        reasons.append("emergency_braking_rate_exceeded")
    if collision_count > thresholds.max_collisions:
        reasons.append("collision_count_exceeded")
    if (
        mode == "sumo_actuated"
        and detector_coverage < thresholds.min_actuated_detector_coverage
    ):
        reasons.append("actuated_detector_coverage_below_minimum")

    return {
        "run_id": run_id,
        "mode": mode,
        "seed": safe_int(summary.get("seed")),
        "demand_profile": str(summary.get("demand_profile", "unknown")),
        "departed_vehicles": departed,
        "teleport_count": teleport_count,
        "teleport_rate": round(teleport_rate, 7),
        "teleport_reasons": teleport_reasons,
        "emergency_braking_count": emergency_brakes,
        "emergency_brakes_per_1000": round(emergency_brakes_per_1000, 5),
        "collision_count": collision_count,
        "missing_actuated_detector_links": len(missing_detector_links),
        "actuated_detector_coverage": round(detector_coverage, 7),
        "sample_file": str(sample_path),
        "summary_file": str(summary_path),
        "sumo_log": str(log_paths[0]) if log_paths else "",
        "accepted": not reasons,
        "rejection_reasons": reasons,
    }


def discover_controlled_links(samples_dir: Path) -> set[tuple[str, int]]:
    """Read one complete sample file; topology is invariant for the dataset."""

    first = next(iter(sorted(samples_dir.glob("*.csv"))), None)
    if first is None:
        return set()
    links: set[tuple[str, int]] = set()
    with first.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            try:
                links.add((str(row["tls_id"]), int(row["signal_index"])))
            except (KeyError, TypeError, ValueError):
                continue
    return links


def parse_missing_detector_links(log_text: str) -> set[tuple[str, int]]:
    return {
        (tls_id, int(index))
        for tls_id, indices in DETECTOR_RE.findall(log_text)
        for index in indices.split(",")
        if index
    }


def normalize_teleport_reason(value: str) -> str:
    reason = value.strip().lower().replace(" ", "_").replace("-", "_")
    if reason in {"wrong_lane", "yield", "jam"}:
        return reason
    return reason or "unknown"


def count_collisions(log_text: str) -> int:
    lowered = log_text.lower()
    return sum(
        1
        for line in lowered.splitlines()
        if "collision" in line and ("warning:" in line or "error:" in line)
    )


def distribution(values: Iterable[float]) -> dict[str, float | int | None]:
    finite = sorted(value for value in values if value != float("inf"))
    if not finite:
        return {"count": 0, "min": None, "p50": None, "p95": None, "max": None}
    return {
        "count": len(finite),
        "min": round(finite[0], 7),
        "p50": round(percentile(finite, 0.50), 7),
        "p95": round(percentile(finite, 0.95), 7),
        "max": round(finite[-1], 7),
    }


def percentile(values: list[float], quantile: float) -> float:
    if len(values) == 1:
        return values[0]
    position = (len(values) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    fraction = position - lower
    return values[lower] * (1.0 - fraction) + values[upper] * fraction


def count_rejection_reasons(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        for reason in row["rejection_reasons"]:
            counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items()))


def safe_int(value: object) -> int:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return 0


def infer_mode(run_id: str) -> str:
    for mode in ("sumo_actuated", "static_fixed", "flowmind", "local"):
        if f"_{mode}_" in run_id:
            return mode
    return "unknown"


__all__ = [
    "DatasetQualityThresholds",
    "audit_dataset",
    "audit_run",
    "discover_controlled_links",
    "parse_missing_detector_links",
]
