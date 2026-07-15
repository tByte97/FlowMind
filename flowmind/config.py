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
    PROJECT_ROOT / "models" / "queue_lgbm_30s_decision.joblib",
    PROJECT_ROOT / "models" / "queue_lgbm_60s_decision.joblib",
    PROJECT_ROOT / "models" / "queue_lgbm_90s_decision.joblib",
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
    priority_min_storage_slots: float = 2.0
    downstream_weight: float = 10.0
    downstream_graph_weight: float = 24.0
    downstream_graph_hops: int = 2
    downstream_graph_decay: float = 0.55
    spillback_start_occupancy: float = 0.55
    spillback_hard_gate_probability: float = 0.85
    spillback_hard_gate_release_probability: float = 0.65
    graph_hard_mask_enabled: bool = False
    physical_hard_mask_enabled: bool = True
    graph_hard_mask_confirmation_samples: int = 3
    graph_hard_mask_release_samples: int = 2
    max_graph_masked_movement_share: float = 0.25
    # Fail back to the empirically safer Local policy when a congested TLS
    # stops discharging.  This is a safety default, not an ablation option:
    # zone coordination must prove that it is healthy before it keeps control.
    throughput_fallback_enabled: bool = True
    throughput_fallback_window_seconds: int = 30
    throughput_fallback_queue_threshold: int = 20
    throughput_fallback_min_discharge_rate: float = 0.02
    throughput_fallback_confirmation_samples: int = 3
    throughput_fallback_recovery_samples: int = 3
    area_pressure_weight: float = 0.35
    coordination_horizon_seconds: int = 60
    saturation_flow_vph_per_lane: float = 1800.0
    objective_delay_weight: float = 1.0
    objective_queue_growth_weight: float = 2.0
    objective_spillback_weight: float = 30.0
    objective_stops_weight: float = 0.5
    objective_throughput_weight: float = 1.0
    platoon_arrival_weight: float = 0.8
    zone_coordination_weight: float = 1.5
    queue_forecast_weight: float = 0.75
    queue_forecast_shadow_mode: bool = True
    queue_forecast_min_confidence: float = 0.70
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
    sensor_last_known_good_ttl: float = 6.0
    corridor_prepare_bonus: float = 12.0
    corridor_prepare_tls_count: int = 3
    corridor_pass_confirmation_distance: float = 35.0
    corridor_reroute_lead_seconds: float = 15.0

    def __post_init__(self) -> None:
        positive = {
            "decision_interval": self.decision_interval,
            "sensor_range_meters": self.sensor_range_meters,
            "min_green": self.min_green,
            "max_green": self.max_green,
            "min_downstream_storage_slots": self.min_downstream_storage_slots,
            "priority_min_storage_slots": self.priority_min_storage_slots,
            "downstream_graph_hops": self.downstream_graph_hops,
            "graph_hard_mask_confirmation_samples": (
                self.graph_hard_mask_confirmation_samples
            ),
            "graph_hard_mask_release_samples": self.graph_hard_mask_release_samples,
            "throughput_fallback_window_seconds": self.throughput_fallback_window_seconds,
            "throughput_fallback_confirmation_samples": self.throughput_fallback_confirmation_samples,
            "throughput_fallback_recovery_samples": self.throughput_fallback_recovery_samples,
            "priority_distance": self.priority_distance,
            "max_priority_override": self.max_priority_override,
            "clearance_seconds": self.clearance_seconds,
            "sensor_last_known_good_ttl": self.sensor_last_known_good_ttl,
            "corridor_prepare_tls_count": self.corridor_prepare_tls_count,
            "corridor_pass_confirmation_distance": (
                self.corridor_pass_confirmation_distance
            ),
            "coordination_horizon_seconds": self.coordination_horizon_seconds,
            "saturation_flow_vph_per_lane": self.saturation_flow_vph_per_lane,
        }
        for name, value in positive.items():
            if float(value) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.min_green > self.max_green:
            raise ValueError("min_green cannot exceed max_green")

        probabilities = {
            "blocked_occupancy": self.blocked_occupancy,
            "downstream_graph_decay": self.downstream_graph_decay,
            "spillback_start_occupancy": self.spillback_start_occupancy,
            "spillback_hard_gate_probability": (
                self.spillback_hard_gate_probability
            ),
            "spillback_hard_gate_release_probability": (
                self.spillback_hard_gate_release_probability
            ),
            "max_graph_masked_movement_share": (
                self.max_graph_masked_movement_share
            ),
            "congested_occupancy_threshold": (
                self.congested_occupancy_threshold
            ),
            "queue_forecast_min_confidence": self.queue_forecast_min_confidence,
        }
        for name, value in probabilities.items():
            if not 0.0 <= float(value) <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")
        if (
            self.spillback_hard_gate_release_probability
            >= self.spillback_hard_gate_probability
        ):
            raise ValueError(
                "spillback_hard_gate_release_probability must be below "
                "spillback_hard_gate_probability"
            )

        non_negative = {
            "default_green_extension": self.default_green_extension,
            "downstream_weight": self.downstream_weight,
            "downstream_graph_weight": self.downstream_graph_weight,
            "area_pressure_weight": self.area_pressure_weight,
            "objective_delay_weight": self.objective_delay_weight,
            "objective_queue_growth_weight": self.objective_queue_growth_weight,
            "objective_spillback_weight": self.objective_spillback_weight,
            "objective_stops_weight": self.objective_stops_weight,
            "objective_throughput_weight": self.objective_throughput_weight,
            "platoon_arrival_weight": self.platoon_arrival_weight,
            "zone_coordination_weight": self.zone_coordination_weight,
            "queue_forecast_weight": self.queue_forecast_weight,
            "empty_approach_penalty": self.empty_approach_penalty,
            "empty_phase_penalty": self.empty_phase_penalty,
            "congested_queue_threshold": self.congested_queue_threshold,
            "congested_approach_bonus": self.congested_approach_bonus,
            "demand_timer_seconds": self.demand_timer_seconds,
            "demand_wait_weight": self.demand_wait_weight,
            "max_demand_wait_bonus": self.max_demand_wait_bonus,
            "throughput_fallback_queue_threshold": self.throughput_fallback_queue_threshold,
            "throughput_fallback_min_discharge_rate": self.throughput_fallback_min_discharge_rate,
            "hysteresis": self.hysteresis,
            "corridor_prepare_bonus": self.corridor_prepare_bonus,
            "corridor_reroute_lead_seconds": self.corridor_reroute_lead_seconds,
        }
        for name, value in non_negative.items():
            if float(value) < 0:
                raise ValueError(f"{name} cannot be negative")

        horizons = tuple(int(horizon) for horizon, _ in self.queue_forecast_horizon_weights)
        weights = tuple(float(weight) for _, weight in self.queue_forecast_horizon_weights)
        if not horizons or any(horizon <= 0 for horizon in horizons):
            raise ValueError("queue forecast horizons must be positive")
        if len(set(horizons)) != len(horizons):
            raise ValueError("queue forecast horizons must be unique")
        if any(weight < 0 for weight in weights) or sum(weights) <= 0:
            raise ValueError("queue forecast weights must be non-negative and non-zero")


@dataclass(frozen=True)
class RunConfig:
    mode: str
    duration: int = 900
    seed: int = 42
    zone_size: int = 20
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
    scenario_name: str | None = None
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
    dataset_fingerprint: str = ""
    demand_profile: str = "normal"
    demand_scale: float = 1.0
    dataset_emergency_active: bool = False
    evaluation_id: str | None = None
    evaluation_pair_id: str | None = None
    evaluation_replicate: int | None = None
    fixed_emergency_route_edges: tuple[str, ...] = field(default_factory=tuple)
    allow_emergency_reroute: bool = True
    enable_live_telemetry: bool = True
    require_complete_actuated_detectors: bool = False

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
        if self.demand_scale <= 0:
            raise ValueError("demand_scale must be positive")
        supported_profiles = {
            "off_peak",
            "normal",
            "morning_peak",
            "evening_peak",
            "oversaturated",
            "incident",
            "lane_closure",
        }
        if self.demand_profile not in supported_profiles:
            raise ValueError(
                "Unsupported demand_profile: " + self.demand_profile
            )
        if self.control.sensor_range_meters <= 0:
            raise ValueError("sensor_range_meters must be positive")
        if any(value <= 0 for value in self.dataset_target_horizons):
            raise ValueError("dataset_target_horizons must be positive")
        if any(weight < 0 for _horizon, weight in self.control.queue_forecast_horizon_weights):
            raise ValueError("queue forecast horizon weights cannot be negative")
        if self.emergency is not None and self.emergency.depart_time >= self.duration:
            raise ValueError("Emergency must depart before the simulation ends")
        if self.evaluation_replicate is not None and self.evaluation_replicate <= 0:
            raise ValueError("evaluation_replicate must be positive")
        if self.fixed_emergency_route_edges and len(self.fixed_emergency_route_edges) < 2:
            raise ValueError("fixed_emergency_route_edges must contain at least two edges")
        if self.fixed_emergency_route_edges and self.emergency is None:
            raise ValueError("A fixed emergency route requires emergency configuration")
