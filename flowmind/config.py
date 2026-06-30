from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class ControlConfig:
    decision_interval: int = 5
    min_green: int = 10
    max_green: int = 45
    blocked_occupancy: float = 0.82
    downstream_weight: float = 10.0
    hysteresis: float = 1.0
    priority_distance: float = 500.0


@dataclass(frozen=True)
class RunConfig:
    mode: str
    duration: int = 900
    seed: int = 42
    zone_size: int = 6
    gui: bool = False
    config_path: Path = (
        PROJECT_ROOT / "simulation" / "rivne_area" / "focused.sumocfg"
    )
    zone_path: Path = (
        PROJECT_ROOT / "simulation" / "rivne_area" / "central_zone.json"
    )
    results_dir: Path = PROJECT_ROOT / "results"
    tls_ids: tuple[str, ...] = field(default_factory=tuple)
    priority_vehicle: str | None = None
    control: ControlConfig = field(default_factory=ControlConfig)

    def __post_init__(self) -> None:
        if self.mode not in {"fixed", "local", "flowmind"}:
            raise ValueError(f"Unknown mode: {self.mode}")
        if self.duration <= 0:
            raise ValueError("duration must be positive")
        if not 1 <= self.zone_size <= 20:
            raise ValueError("zone_size must be between 1 and 20")
