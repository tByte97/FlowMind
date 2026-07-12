from __future__ import annotations

import hashlib
import json
import os
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import RunConfig
    from .queue_forecast import QueueForecastStats


PROVENANCE_SCHEMA_VERSION = 1
CONTROLLER_SCHEMA_VERSION = "flowmind-controller/6"
_CONTROLLER_SOURCE_FILES = (
    "config.py",
    "controller.py",
    "corridor_manager.py",
    "corridor_recovery.py",
    "queue_forecast.py",
    "safety_validator.py",
    "signal_policy.py",
    "tls_safety.py",
    "traffic_state.py",
    "zone_graph.py",
)


@dataclass(frozen=True)
class ScenarioProvenance:
    scenario: str
    demand_vehicles_per_hour: float | None
    demand_duration_seconds: float | None
    manifest_path: str
    manifest_sha256: str
    sumo_config_sha256: str
    route_files_sha256: str
    scenario_sha256: str
    network_sha256: str
    zone_sha256: str


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def files_sha256(paths: tuple[Path, ...] | list[Path]) -> str:
    digest = hashlib.sha256()
    usable = sorted(
        (path.resolve() for path in paths if path.is_file()),
        key=lambda path: path.as_posix(),
    )
    if not usable:
        return ""
    common_parent = Path(os.path.commonpath([str(path.parent) for path in usable]))
    for path in usable:
        try:
            name = path.relative_to(common_parent).as_posix()
        except ValueError:
            name = path.name
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        try:
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError:
            return ""
        digest.update(b"\0")
    return digest.hexdigest()


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def scenario_provenance(
    config_path: Path,
    zone_path: Path,
    network_path: Path,
    explicit_name: str | None = None,
) -> ScenarioProvenance:
    config_path = config_path.resolve()
    scenario_dir = config_path.parent
    manifest_path = scenario_dir / f"{config_path.stem}.manifest.json"
    manifest: dict[str, Any] = {}
    if manifest_path.is_file():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            loaded = {}
        if isinstance(loaded, dict):
            manifest = loaded
    route_paths = _sumo_route_paths(config_path)
    scenario_paths = [config_path, zone_path.resolve(), *route_paths]
    if manifest_path.is_file():
        scenario_paths.append(manifest_path)
    name = explicit_name or str(
        manifest.get("scenario")
        or manifest.get("id")
        or f"{scenario_dir.name}_{config_path.stem}"
    )
    return ScenarioProvenance(
        scenario=name,
        demand_vehicles_per_hour=_optional_float(
            manifest.get("vehicles_per_hour")
        ),
        demand_duration_seconds=_optional_float(manifest.get("duration")),
        manifest_path=str(manifest_path) if manifest_path.is_file() else "",
        manifest_sha256=file_sha256(manifest_path),
        sumo_config_sha256=file_sha256(config_path),
        route_files_sha256=files_sha256(route_paths),
        scenario_sha256=files_sha256(scenario_paths),
        network_sha256=file_sha256(network_path.resolve()),
        zone_sha256=file_sha256(zone_path.resolve()),
    )


def controller_source_sha256() -> str:
    source_dir = Path(__file__).resolve().parent
    return files_sha256([source_dir / name for name in _CONTROLLER_SOURCE_FILES])


def controller_git_commit() -> str:
    explicit = os.getenv("FLOWMIND_CONTROLLER_COMMIT", "").strip()
    if explicit:
        return explicit
    project_root = Path(__file__).resolve().parents[1]
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip()


def run_config_sha256(
    config: RunConfig,
    emergency_route_edges: tuple[str, ...] = (),
) -> str:
    payload = _substantive_run_config(config)
    payload["emergency_route_edges"] = list(emergency_route_edges)
    return canonical_sha256(payload)


def pair_config_sha256(
    config: RunConfig,
    emergency_route_edges: tuple[str, ...] = (),
) -> str:
    payload = _substantive_run_config(config)
    payload.pop("mode", None)
    payload["emergency_route_edges"] = list(emergency_route_edges)
    return canonical_sha256(payload)


def run_provenance_summary(
    config: RunConfig,
    network_path: Path,
    queue_stats: QueueForecastStats | None,
    emergency_route_edges: tuple[str, ...] = (),
) -> dict[str, object]:
    scenario = scenario_provenance(
        config.config_path,
        config.zone_path,
        network_path,
        config.scenario_name,
    )
    source_hash = controller_source_sha256()
    git_commit = controller_git_commit()
    controller_version = os.getenv("FLOWMIND_CONTROLLER_VERSION", "").strip()
    if not controller_version:
        suffix = git_commit[:12] if git_commit else source_hash[:12]
        controller_version = f"{CONTROLLER_SCHEMA_VERSION}@{suffix}"
    model_hash = queue_stats.artifact_sha256 if queue_stats is not None else ""
    return {
        "provenance_schema_version": PROVENANCE_SCHEMA_VERSION,
        "seed": config.seed,
        "scenario": scenario.scenario,
        "demand_vehicles_per_hour": scenario.demand_vehicles_per_hour,
        "demand_duration_seconds": scenario.demand_duration_seconds,
        "scenario_manifest": scenario.manifest_path,
        "scenario_manifest_sha256": scenario.manifest_sha256,
        "sumo_config_sha256": scenario.sumo_config_sha256,
        "route_files_sha256": scenario.route_files_sha256,
        "scenario_sha256": scenario.scenario_sha256,
        "network_sha256": scenario.network_sha256,
        "zone_sha256": scenario.zone_sha256,
        "controller_version": controller_version,
        "controller_git_commit": git_commit,
        "controller_source_sha256": source_hash,
        "run_config_sha256": run_config_sha256(config, emergency_route_edges),
        "pair_config_sha256": pair_config_sha256(config, emergency_route_edges),
        "model_artifact_sha256": model_hash,
        "emergency_route_sha256": (
            canonical_sha256(emergency_route_edges)
            if emergency_route_edges
            else ""
        ),
        "evaluation_id": config.evaluation_id,
        "evaluation_pair_id": config.evaluation_pair_id,
        "evaluation_replicate": config.evaluation_replicate,
    }


def _substantive_run_config(config: RunConfig) -> dict[str, Any]:
    payload = asdict(config)
    for key in (
        "results_dir",
        "websocket_port",
        "gui",
        "gui_delay_ms",
        "dataset_dir",
        "dataset_run_id",
        "evaluation_id",
        "evaluation_pair_id",
        "evaluation_replicate",
        "enable_live_telemetry",
        "fixed_emergency_route_edges",
    ):
        payload.pop(key, None)
    return _jsonable(payload)


def _sumo_route_paths(config_path: Path) -> list[Path]:
    try:
        root = ET.parse(config_path).getroot()
    except (OSError, ET.ParseError):
        return []
    value = ""
    for element in root.findall(".//route-files"):
        value = str(element.attrib.get("value", ""))
        if value:
            break
    return [
        (config_path.parent / item.strip()).resolve()
        for item in value.split(",")
        if item.strip()
    ]


def _optional_float(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Path):
        return str(value.resolve())
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    return value


__all__ = [
    "CONTROLLER_SCHEMA_VERSION",
    "ScenarioProvenance",
    "canonical_sha256",
    "controller_source_sha256",
    "file_sha256",
    "files_sha256",
    "pair_config_sha256",
    "run_config_sha256",
    "run_provenance_summary",
    "scenario_provenance",
]
