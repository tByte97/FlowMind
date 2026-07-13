from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .area_model import AreaModel, load_zone_tls_ids
from .config import RunConfig
from .ml_dataset import ML_DATASET_SCHEMA_VERSION, ml_dataset_schema_sha256
from .provenance import controller_git_commit, scenario_provenance

if TYPE_CHECKING:
    from .queue_forecast import QueueForecastEnsemble, QueueForecastModel


RUNTIME_CONTRACT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class RuntimeContract:
    schema_version: int
    valid: bool
    strict: bool
    controller_git_commit: str
    image_git_commit: str
    controlled_tls_count: int
    configured_tls_count: int
    network_sha256: str
    zone_sha256: str
    dataset_schema_version: int
    dataset_schema_sha256: str
    model_loaded: bool
    model_network_sha256: str
    model_zone_sha256: str
    model_feature_schema_sha256: str
    model_dataset_fingerprint: str
    model_dataset_schema_version: str
    model_dataset_schema_sha256: str
    model_known_tls_count: int
    model_known_lane_count: int
    covered_tls_count: int
    required_lane_count: int
    covered_lane_count: int
    missing_tls_ids: tuple[str, ...]
    missing_lane_ids: tuple[str, ...]
    errors: tuple[str, ...]
    warnings: tuple[str, ...]

    def raise_for_errors(self) -> None:
        if self.errors:
            raise RuntimeError(
                "FlowMind runtime contract failed: " + "; ".join(self.errors)
            )


def validate_runtime_contract(
    config: RunConfig,
    area: AreaModel,
    network_path: Path,
    queue_forecast: QueueForecastModel | QueueForecastEnsemble | None,
) -> RuntimeContract:
    strict = os.getenv("FLOWMIND_STRICT_STARTUP", "0").strip() == "1"
    image_commit = os.getenv("FLOWMIND_IMAGE_COMMIT", "").strip()
    git_commit = controller_git_commit()
    scenario = scenario_provenance(
        config.config_path,
        config.zone_path,
        network_path,
        config.scenario_name,
    )
    configured_tls = tuple(load_zone_tls_ids(config.zone_path))
    required_tls = set(area.tls_ids)
    required_lanes = set(area.incoming_lanes) | set(area.outgoing_lanes)
    model_tls = (
        set(queue_forecast.known_tls_ids) if queue_forecast is not None else set()
    )
    model_lanes = (
        set(queue_forecast.known_lane_ids) if queue_forecast is not None else set()
    )
    stats = queue_forecast.stats if queue_forecast is not None else None
    errors: list[str] = []
    warnings: list[str] = []

    configured_tls_set = set(configured_tls)
    configured_area_matches = (
        configured_tls_set.issubset(required_tls)
        if config.emergency is not None
        else configured_tls_set == required_tls
    )
    if not configured_area_matches:
        errors.append(
            "configured TLS set does not match the discovered controlled area"
        )
    if image_commit in {"", "unknown", "unversioned"}:
        (errors if strict else warnings).append("image git commit is not versioned")
    elif git_commit and image_commit != git_commit:
        errors.append(
            f"image git commit {image_commit} differs from controller {git_commit}"
        )

    missing_tls = tuple(sorted(required_tls - model_tls))
    missing_lanes = tuple(sorted(required_lanes - model_lanes))
    model_issues: list[str] = []
    if queue_forecast is not None and stats is not None:
        if missing_tls:
            model_issues.append(f"model misses {len(missing_tls)} TLS")
        if missing_lanes:
            model_issues.append(f"model misses {len(missing_lanes)} lanes")
        if not _hash_set_matches(stats.network_sha256, scenario.network_sha256):
            model_issues.append("model/network hash mismatch")
        if not _hash_set_matches(stats.zone_sha256, scenario.zone_sha256):
            model_issues.append("model/zone hash mismatch")
        if not stats.feature_schema_sha256:
            model_issues.append("model feature schema hash is missing")
        elif not stats.feature_schema_valid:
            model_issues.append("model feature schema hash mismatch")
        if not stats.artifact_hash_valid:
            model_issues.append("model artifact hash mismatch or missing")
        if not stats.dataset_fingerprint:
            model_issues.append("model dataset fingerprint is missing")
        if not _hash_set_matches(
            stats.dataset_schema_version,
            str(ML_DATASET_SCHEMA_VERSION),
        ):
            model_issues.append("model dataset schema version mismatch")
        if not _hash_set_matches(
            stats.dataset_schema_sha256,
            ml_dataset_schema_sha256(),
        ):
            model_issues.append("model dataset schema hash mismatch")
    elif not config.control.queue_forecast_shadow_mode and config.mode == "flowmind":
        model_issues.append("queue control requested without a model")

    if model_issues:
        target = warnings if config.control.queue_forecast_shadow_mode else errors
        target.extend(model_issues)

    return RuntimeContract(
        schema_version=RUNTIME_CONTRACT_SCHEMA_VERSION,
        valid=not errors,
        strict=strict,
        controller_git_commit=git_commit,
        image_git_commit=image_commit,
        controlled_tls_count=len(required_tls),
        configured_tls_count=len(configured_tls),
        network_sha256=scenario.network_sha256,
        zone_sha256=scenario.zone_sha256,
        dataset_schema_version=ML_DATASET_SCHEMA_VERSION,
        dataset_schema_sha256=ml_dataset_schema_sha256(),
        model_loaded=queue_forecast is not None,
        model_network_sha256=stats.network_sha256 if stats is not None else "",
        model_zone_sha256=stats.zone_sha256 if stats is not None else "",
        model_feature_schema_sha256=(
            stats.feature_schema_sha256 if stats is not None else ""
        ),
        model_dataset_fingerprint=(
            stats.dataset_fingerprint if stats is not None else ""
        ),
        model_dataset_schema_version=(
            stats.dataset_schema_version if stats is not None else ""
        ),
        model_dataset_schema_sha256=(
            stats.dataset_schema_sha256 if stats is not None else ""
        ),
        model_known_tls_count=len(model_tls),
        model_known_lane_count=len(model_lanes),
        covered_tls_count=len(required_tls & model_tls),
        required_lane_count=len(required_lanes),
        covered_lane_count=len(required_lanes & model_lanes),
        missing_tls_ids=missing_tls,
        missing_lane_ids=missing_lanes,
        errors=tuple(errors),
        warnings=tuple(warnings),
    )


def write_runtime_contract_audit(
    results_dir: Path,
    mode: str,
    contract: RuntimeContract,
) -> Path:
    results_dir.mkdir(parents=True, exist_ok=True)
    path = results_dir / f"{mode}_runtime_contract_startup.json"
    path.write_text(
        json.dumps(asdict(contract), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def _hash_set_matches(declared: str, expected: str) -> bool:
    values = {value for value in str(declared).split(";") if value}
    return bool(values) and values == {expected}


__all__ = [
    "RuntimeContract",
    "validate_runtime_contract",
    "write_runtime_contract_audit",
]
