from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .emergency_vehicle import EmergencyVehicleConfig


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATIC_FIXED_MODE = "static_fixed"
SUMO_ACTUATED_MODE = "sumo_actuated"
LOCAL_MODE = "local"
FLOWMIND_MODE = "flowmind"
CONTROL_MODES = (
    STATIC_FIXED_MODE,
    SUMO_ACTUATED_MODE,
    LOCAL_MODE,
    FLOWMIND_MODE,
)
ADAPTIVE_CONTROL_MODES = (LOCAL_MODE, FLOWMIND_MODE)
LEGACY_CONTROL_MODE_ALIASES = {"fixed": STATIC_FIXED_MODE}
DEFAULT_QUEUE_MODEL_PATHS = (
    PROJECT_ROOT / "models" / "queue_lgbm_30s_current.joblib",
    PROJECT_ROOT / "models" / "queue_lgbm_60s_current.joblib",
    PROJECT_ROOT / "models" / "queue_lgbm_90s_current.joblib",
)


def normalize_control_mode(mode: str) -> str:
    """Return the canonical control mode while accepting old run commands."""

    requested = str(mode).strip().lower()
    canonical = LEGACY_CONTROL_MODE_ALIASES.get(requested, requested)
    if canonical not in CONTROL_MODES:
        supported = ", ".join(CONTROL_MODES)
        raise ValueError(f"Unknown mode: {mode}. Expected one of: {supported}")
    return canonical


@dataclass(frozen=True)
class ControlConfig:
    decision_interval: int = 3
    sensor_range_meters: float = 120.0
    min_green: int = 10
    max_green: int = 45
    use_default_phase_timing: bool = True
    default_green_extension: float = 8.0
    blocked_occupancy: float = 0.82
    min_downstream_storage_slots: float = 1.0
    downstream_weight: float = 10.0
    area_pressure_weight: float = 0.35
    queue_forecast_weight: float = 0.75
    empty_approach_penalty: float = 8.0
    empty_phase_penalty: float = 30.0
    congested_queue_threshold: int = 8
    congested_occupancy_threshold: float = 0.65
    congested_approach_bonus: float = 12.0
    demand_timer_seconds: int = 6
    demand_wait_weight: float = 0.45
    max_demand_wait_bonus: float = 18.0
    queue_forecast_horizon_weights: tuple[tuple[int, float], ...] = (
        (30, 0.50),
        (60, 0.35),
        (90, 0.15),
    )
    hysteresis: float = 1.0
    priority_distance: float = 500.0
    max_priority_override: int = 35
    clearance_seconds: int = 5


@dataclass(frozen=True)
class RunConfig:
    mode: str
    duration: int = 900
    seed: int = 42
    zone_size: int = 6
    gui: bool = False
    gui_delay_ms: int = 50
    websocket_port: int = 8765
    config_path: Path = (
        PROJECT_ROOT / "simulation" / "rivne_area" / "focused.sumocfg"
    )
    zone_path: Path = (
        PROJECT_ROOT / "simulation" / "rivne_area" / "central_zone.json"
    )
    results_dir: Path = PROJECT_ROOT / "results"
    tls_ids: tuple[str, ...] = field(default_factory=tuple)
    priority_vehicle: str | None = None
    emergency: EmergencyVehicleConfig | None = None
    control: ControlConfig = field(default_factory=ControlConfig)
    queue_model_path: Path | None = None
    queue_model_paths: tuple[Path, ...] = DEFAULT_QUEUE_MODEL_PATHS
    dataset_dir: Path | None = None
    dataset_run_id: str | None = None
    dataset_scenario: str = "rivne_focused"
    dataset_sample_interval: int = 5
    dataset_target_horizons: tuple[int, ...] = (30, 60, 90)

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", normalize_control_mode(self.mode))
        if self.duration <= 0:
            raise ValueError("duration must be positive")
        if not 1 <= self.zone_size <= 20:
            raise ValueError("zone_size must be between 1 and 20")
        if self.gui_delay_ms < 0:
            raise ValueError("gui_delay_ms cannot be negative")
        if not 1 <= self.websocket_port <= 65535:
            raise ValueError("websocket_port must be between 1 and 65535")
        if self.dataset_sample_interval <= 0:
            raise ValueError("dataset_sample_interval must be positive")
        if self.control.sensor_range_meters <= 0:
            raise ValueError("sensor_range_meters must be positive")
        if any(value <= 0 for value in self.dataset_target_horizons):
            raise ValueError("dataset_target_horizons must be positive")
        if any(weight < 0 for _horizon, weight in self.control.queue_forecast_horizon_weights):
            raise ValueError("queue forecast horizon weights cannot be negative")
        if self.emergency is not None and self.emergency.depart_time >= self.duration:
            raise ValueError("Emergency must depart before the simulation ends")
