from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Iterable

import pandas as pd
import plotly.express as px
import streamlit as st

from flowmind.live_transport import LiveTelemetryClient


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_RESULTS_DIR = PROJECT_ROOT / "results"
RESULTS_DIR = Path(os.environ.get("FLOWMIND_RESULTS_DIR", str(PROJECT_RESULTS_DIR)))
LIVE_REFRESH_INTERVAL = (
    None if os.environ.get("FLOWMIND_DISABLE_LIVE_REFRESH") else "1s"
)

MODE_LABELS = {
    "static_fixed": "Static Fixed",
    "sumo_actuated": "SUMO Actuated",
    "local": "Local Adaptive",
    "flowmind": "FlowMind Area Balance",
    "fixed": "Fixed (legacy)",
}
MODE_COLORS = {
    "static_fixed": "#ef4444",
    "sumo_actuated": "#8b5cf6",
    "local": "#f59e0b",
    "flowmind": "#10b981",
    "fixed": "#b91c1c",
}
MODE_ORDER = {
    "static_fixed": 0,
    "sumo_actuated": 1,
    "local": 2,
    "flowmind": 3,
    "fixed": 4,
}
BASELINE_MODES = ("static_fixed", "fixed")

METRIC_LABELS = {
    "average_travel_time": "Середній час поїздки, с",
    "average_waiting_time": "Середнє очікування, с",
    "average_queue_length": "Середня черга, авто",
    "max_queue_length": "Максимальна черга, авто",
    "throughput": "Пропускна здатність, авто",
    "stops_count": "Кількість зупинок",
    "gridlock_risk": "Gridlock risk",
    "controller_decisions": "Рішення контролера",
    "phase_extensions": "Продовження зеленого",
    "phase_advances": "Перемикання фаз",
    "priority_decisions": "Пріоритети швидкої",
    "queue_forecast_predictions": "ML-прогнози черги",
    "queue_forecast_failures": "Помилки ML-прогнозу",
}
LOWER_IS_BETTER = {
    "average_travel_time",
    "average_waiting_time",
    "average_queue_length",
    "max_queue_length",
    "stops_count",
    "gridlock_risk",
}
NUMERIC_COLUMNS = [
    *METRIC_LABELS,
    "simulated_duration",
    "controlled_tls",
    "emergency_departure_time",
    "emergency_arrival_time",
    "emergency_eta",
    "emergency_trace_samples",
    "emergency_route_edge_count",
    "emergency_route_length",
    "emergency_expected_travel_time",
    "emergency_predicted_eta",
    "queue_forecast_feature_count",
    "queue_forecast_model_count",
    "queue_forecast_predictions",
    "queue_forecast_failures",
    "queue_forecast_trace_samples",
]
BEFORE_AFTER_METRICS = (
    ("average_waiting_time", "Середнє очікування", " с", True),
    ("average_queue_length", "Середня черга", " авто", True),
    ("stops_count", "Зупинки транспорту", "", True),
    ("throughput", "Завершили маршрут", " авто", False),
    ("emergency_eta", "Час доїзду швидкої", " с", True),
)


st.set_page_config(page_title="FlowMind Dashboard", page_icon="🚦", layout="wide")


def display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path.resolve())


def discover_result_sets(base_dir: Path) -> list[Path]:
    candidates: list[Path] = []
    seen: set[Path] = set()

    def add_candidate(path: Path) -> None:
        resolved = path.resolve()
        if resolved in seen:
            return
        seen.add(resolved)
        candidates.append(resolved)

    if base_dir.exists() and (base_dir / "summary.csv").exists():
        add_candidate(base_dir)

    if base_dir.exists():
        for path in sorted(base_dir.glob("**/summary.csv")):
            add_candidate(path.parent)
        for path in sorted(base_dir.glob("**/live_status.json")):
            add_candidate(path.parent)

    return candidates


@st.cache_data(show_spinner=False, ttl=2)
def _cached_result_sets(base_dir: Path) -> list[Path]:
    return discover_result_sets(base_dir)


def load_summary(result_dir: Path) -> pd.DataFrame:
    path = result_dir / "summary.csv"
    if not path.exists():
        return pd.DataFrame(columns=["mode"])
    try:
        summary = pd.read_csv(path)
    except (OSError, ValueError, pd.errors.EmptyDataError):
        return pd.DataFrame(columns=["mode"])
    summary["mode"] = summary["mode"].astype(str)
    summary["label"] = summary["mode"].map(MODE_LABELS).fillna(summary["mode"])
    summary["mode_order"] = summary["mode"].map(MODE_ORDER).fillna(99)
    summary["result_set"] = display_path(result_dir)
    for column in NUMERIC_COLUMNS:
        if column in summary.columns:
            summary[column] = pd.to_numeric(summary[column], errors="coerce")
    return summary.sort_values(["mode_order", "mode"]).reset_index(drop=True)


def format_number(value: object, suffix: str = "", decimals: int = 1) -> str:
    if pd.isna(value):
        return "—"
    number = float(value)
    if number.is_integer():
        return f"{int(number)}{suffix}"
    return f"{number:.{decimals}f}{suffix}"


def pct_delta(current: float, baseline: float) -> str:
    if pd.isna(current) or pd.isna(baseline) or baseline == 0:
        return ""
    return f"{((current - baseline) / baseline) * 100:+.1f}%"


def select_baseline_mode(modes: Iterable[str]) -> str | None:
    available = set(modes)
    return next((mode for mode in BASELINE_MODES if mode in available), None)


def before_after_rows(summary: pd.DataFrame) -> list[dict[str, object]]:
    if summary.empty or "mode" not in summary.columns:
        return []
    modes = set(summary["mode"].astype(str))
    baseline_mode = select_baseline_mode(modes)
    if baseline_mode is None or "flowmind" not in modes:
        return []
    before = summary[summary["mode"] == baseline_mode].iloc[0]
    after = summary[summary["mode"] == "flowmind"].iloc[0]
    rows: list[dict[str, object]] = []
    for metric, label, suffix, lower_is_better in BEFORE_AFTER_METRICS:
        if metric not in summary.columns:
            continue
        before_value = pd.to_numeric(pd.Series([before[metric]]), errors="coerce").iloc[0]
        after_value = pd.to_numeric(pd.Series([after[metric]]), errors="coerce").iloc[0]
        if pd.isna(before_value) or pd.isna(after_value):
            continue
        improvement = None
        if float(before_value) != 0:
            change = (
                float(before_value) - float(after_value)
                if lower_is_better
                else float(after_value) - float(before_value)
            )
            improvement = change / abs(float(before_value)) * 100.0
        rows.append(
            {
                "metric": metric,
                "label": label,
                "suffix": suffix,
                "before": float(before_value),
                "after": float(after_value),
                "improvement": improvement,
            }
        )
    return rows


def nearest_timeseries_snapshot(
    frame: pd.DataFrame,
    selected_time: float,
) -> dict[str, float]:
    if frame.empty or "time" not in frame.columns:
        return {}
    ordered = frame.copy()
    ordered["time"] = pd.to_numeric(ordered["time"], errors="coerce")
    ordered = ordered.dropna(subset=["time"]).sort_values("time")
    if ordered.empty:
        return {}
    candidates = ordered[ordered["time"] <= selected_time]
    row = candidates.iloc[-1] if not candidates.empty else ordered.iloc[0]
    snapshot: dict[str, float] = {}
    for column in [
        "time",
        "active_vehicles",
        "arrived",
        "mean_speed",
        "waiting_time",
        "queue_length",
        "throughput",
        "stops_count",
    ]:
        if column not in row.index:
            continue
        value = pd.to_numeric(pd.Series([row[column]]), errors="coerce").iloc[0]
        if pd.notna(value):
            snapshot[column] = float(value)
    return snapshot


def visual_comparison_data(
    fixed: pd.DataFrame,
    flowmind: pd.DataFrame,
    selected_time: float,
) -> dict[str, object]:
    fixed_snapshot = nearest_timeseries_snapshot(fixed, selected_time)
    flowmind_snapshot = nearest_timeseries_snapshot(flowmind, selected_time)
    if not fixed_snapshot or not flowmind_snapshot:
        return {}
    def maximum_queue(frame: pd.DataFrame) -> float:
        if "queue_length" not in frame.columns:
            return 0.0
        values = pd.to_numeric(frame["queue_length"], errors="coerce").dropna()
        return float(values.max()) if not values.empty else 0.0

    max_queue = max(
        maximum_queue(fixed),
        maximum_queue(flowmind),
        1.0,
    )
    return {
        "time": min(
            fixed_snapshot.get("time", selected_time),
            flowmind_snapshot.get("time", selected_time),
        ),
        "fixed": fixed_snapshot,
        "flowmind": flowmind_snapshot,
        "max_queue": float(max_queue),
        "queue_difference": (
            fixed_snapshot.get("queue_length", 0.0)
            - flowmind_snapshot.get("queue_length", 0.0)
        ),
        "waiting_difference": (
            fixed_snapshot.get("waiting_time", 0.0)
            - flowmind_snapshot.get("waiting_time", 0.0)
        ),
        "throughput_difference": (
            flowmind_snapshot.get("throughput", 0.0)
            - fixed_snapshot.get("throughput", 0.0)
        ),
    }


def best_row(summary: pd.DataFrame, metric: str) -> pd.Series | None:
    if metric not in summary.columns:
        return None
    values = pd.to_numeric(summary[metric], errors="coerce")
    if values.dropna().empty:
        return None
    index = values.idxmin() if metric in LOWER_IS_BETTER else values.idxmax()
    return summary.loc[index]


def load_timeseries(result_dir: Path, mode: str) -> pd.DataFrame | None:
    path = result_dir / f"{mode}_timeseries.csv"
    if not path.exists():
        return None
    frame = pd.read_csv(path)
    frame["mode"] = mode
    frame["label"] = MODE_LABELS.get(mode, mode)
    return frame


def load_trace(result_dir: Path, mode: str) -> pd.DataFrame | None:
    path = result_dir / f"{mode}_emergency_trace.csv"
    if not path.exists():
        return None
    trace = pd.read_csv(path)
    if trace.empty:
        return None
    trace["mode"] = mode
    trace["label"] = MODE_LABELS.get(mode, mode)
    trace["result_set"] = display_path(result_dir)
    return trace


def load_queue_forecast_trace(result_dir: Path, mode: str) -> pd.DataFrame | None:
    path = result_dir / f"{mode}_queue_forecast.csv"
    if not path.exists():
        return None
    trace = pd.read_csv(path)
    if trace.empty:
        return None
    trace["mode"] = mode
    trace["label"] = MODE_LABELS.get(mode, mode)
    return trace


def truthy(value: object) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "так"}


def emergency_runs_for_result(result_dir: Path, summary: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    modes = set(summary["mode"].astype(str))
    modes.update(path.name.removesuffix("_emergency_trace.csv") for path in result_dir.glob("*_emergency_trace.csv"))

    for mode in sorted(modes, key=lambda item: MODE_ORDER.get(item, 99)):
        summary_row = summary[summary["mode"] == mode]
        source = summary_row.iloc[0].to_dict() if not summary_row.empty else {}
        trace = load_trace(result_dir, mode)
        has_summary_eta = (
            "emergency_eta" in source
            and pd.notna(source.get("emergency_eta"))
            and float(source.get("emergency_eta", 0)) > 0
        )
        if not has_summary_eta and trace is None:
            continue

        trace_eta = None
        avg_speed = None
        max_speed = None
        first_edge = None
        last_edge = None
        trace_samples = None
        if trace is not None:
            times = pd.to_numeric(trace["time"], errors="coerce").dropna()
            speeds = pd.to_numeric(trace["speed"], errors="coerce").dropna()
            if not times.empty:
                # Trace is sampled every simulation second, so inclusive duration
                # matches SUMO trip ETA for existing result files.
                trace_eta = float(times.max() - times.min() + 1)
            if not speeds.empty:
                avg_speed = float(speeds.mean())
                max_speed = float(speeds.max())
            first_edge = str(trace.iloc[0].get("edge_id", ""))
            last_edge = str(trace.iloc[-1].get("edge_id", ""))
            trace_samples = len(trace)

        rows.append(
            {
                "result_set": display_path(result_dir),
                "mode": mode,
                "label": MODE_LABELS.get(mode, mode),
                "vehicle_id": source.get("emergency_vehicle_id", "flowmind_ambulance"),
                "start": source.get("emergency_start", first_edge or "—"),
                "destination": source.get("emergency_destination", last_edge or "—"),
                "departure": source.get("emergency_departure_time"),
                "arrival": source.get("emergency_arrival_time"),
                "eta": source.get("emergency_eta", trace_eta),
                "trace_eta": trace_eta,
                "predicted_eta": source.get("emergency_predicted_eta"),
                "expected_travel_time": source.get("emergency_expected_travel_time"),
                "route_edges": source.get("emergency_route_edge_count"),
                "route_length": source.get("emergency_route_length"),
                "trace_samples": source.get("emergency_trace_samples", trace_samples),
                "trace_rows": trace_samples,
                "avg_speed": avg_speed,
                "max_speed": max_speed,
                "priority_decisions": source.get("priority_decisions"),
                "first_edge": first_edge,
                "last_edge": last_edge,
            }
        )
    frame = pd.DataFrame(rows)
    for column in [
        "departure",
        "arrival",
        "eta",
        "trace_eta",
        "predicted_eta",
        "expected_travel_time",
        "route_edges",
        "route_length",
        "trace_samples",
        "trace_rows",
        "avg_speed",
        "max_speed",
        "priority_decisions",
    ]:
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame


def load_all_emergency_runs(result_sets: Iterable[Path]) -> pd.DataFrame:
    frames = []
    for result_dir in result_sets:
        try:
            frames.append(emergency_runs_for_result(result_dir, load_summary(result_dir)))
        except (OSError, ValueError, pd.errors.EmptyDataError):
            continue
    frames = [frame for frame in frames if not frame.empty]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def load_live_status(result_dir: Path) -> dict[str, object] | None:
    path = result_dir / "live_status.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


class LiveSocketClient:
    def __init__(self, url: str) -> None:
        self._client = LiveTelemetryClient(url)
        self._latest: dict[str, object] | None = None

    def start(self) -> None:
        self._client.start()

    def latest(self) -> dict[str, object] | None:
        payload = self._client.latest_update()
        if payload is not None:
            self._latest = payload
        return self._latest

    @property
    def status(self) -> dict[str, object]:
        return self._client.status

    def stop(self) -> None:
        self._client.stop()


def live_history_frame(payload: dict[str, object]) -> pd.DataFrame:
    history = payload.get("metric_history", [])
    if not isinstance(history, list):
        return pd.DataFrame()
    rows = [item for item in history if isinstance(item, dict)]
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)
    numeric_columns = [
        "time",
        "active_vehicles",
        "departed",
        "arrived",
        "inflow_per_minute",
        "outflow_per_minute",
        "mean_speed",
        "waiting_time",
        "queue_length",
        "max_queue_length",
        "throughput",
        "stops_count",
        "gridlock_risk",
    ]
    for column in numeric_columns:
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame


def live_system_rows(payload: dict[str, object]) -> list[dict[str, object]]:
    system = payload.get("system", {})
    if not isinstance(system, dict):
        return []
    labels = {
        "simulation": "SUMO simulation",
        "websocket": "WebSocket",
        "controller": "Signal controller",
        "tls_programs": "SUMO TLS programs",
        "queue_forecast": "ML queue forecast",
        "corridor": "Emergency corridor",
        "metrics": "Metrics collector",
    }
    rows = []
    for name, details in system.items():
        if not isinstance(details, dict):
            continue
        status = details.get("status", details.get("corridor_state", "unknown"))
        def visible(value: object) -> bool:
            return value is not None and value != "" and value != ()

        facts = ", ".join(
            f"{key}={value}"
            for key, value in details.items()
            if key not in {"status", "corridor_state", "items"} and visible(value)
        )
        rows.append(
            {
                "Компонент": labels.get(str(name), str(name)),
                "Стан": status,
                "Деталі": facts or "—",
            }
        )
    return rows


def payload_age_seconds(payload: dict[str, object]) -> float | None:
    emitted_at = payload.get("emitted_at")
    if not emitted_at:
        return None
    try:
        timestamp = pd.Timestamp(str(emitted_at))
        if timestamp.tzinfo is None:
            timestamp = timestamp.tz_localize("UTC")
        return max(time.time() - timestamp.timestamp(), 0.0)
    except (TypeError, ValueError):
        return None


def socket_payload_matches(
    payload: dict[str, object],
    result_dir: Path,
) -> bool:
    source = payload.get("results_dir")
    if not source:
        return True
    try:
        return Path(str(source)).resolve() == result_dir.resolve()
    except OSError:
        return False


def simulation_status(payload: dict[str, object] | None) -> str:
    if not isinstance(payload, dict):
        return "waiting"
    system = payload.get("system", {})
    if not isinstance(system, dict):
        return "unknown"
    simulation = system.get("simulation", {})
    if not isinstance(simulation, dict):
        return "unknown"
    return str(simulation.get("status", "unknown"))


def live_average_metrics(payload: dict[str, object]) -> dict[str, float]:
    history = live_history_frame(payload)
    fields = {
        "active_vehicles": "active_vehicles",
        "inflow_per_minute": "inflow_per_minute",
        "outflow_per_minute": "outflow_per_minute",
        "queue_length": "queue_length",
        "waiting_time": "waiting_time",
        "mean_speed": "mean_speed",
        "gridlock_risk": "gridlock_risk",
    }
    averages: dict[str, float] = {}
    for output_name, column in fields.items():
        if column not in history.columns:
            averages[output_name] = 0.0
            continue
        values = pd.to_numeric(history[column], errors="coerce").dropna()
        averages[output_name] = float(values.mean()) if not values.empty else 0.0
    return averages


def decision_log_rows(payload: dict[str, object]) -> list[dict[str, object]]:
    value = payload.get("decision_log", [])
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def corridor_status(payload: dict[str, object]) -> dict[str, object]:
    system = payload.get("system", {})
    if not isinstance(system, dict):
        return {}
    corridor = system.get("corridor", {})
    return corridor if isinstance(corridor, dict) else {}


def render_before_after(summary: pd.DataFrame) -> None:
    rows = before_after_rows(summary)
    if not rows:
        if "flowmind" in set(summary.get("mode", pd.Series(dtype=str)).astype(str)):
            st.info(
                "Для фінального «було → стало» потрібен static_fixed-прогін із тим самим "
                "seed. `run_demo.py` тепер збирає його автоматично."
            )
        return

    st.subheader("🏁 Було → стало: Fixed проти FlowMind")
    st.caption(
        "Один сценарій, тривалість і seed. Позитивний відсоток означає "
        "покращення FlowMind."
    )
    columns = st.columns(len(rows))
    for column, row in zip(columns, rows, strict=True):
        improvement = row["improvement"]
        delta = (
            (
                f"{float(improvement):+.1f}% покращення"
                if float(improvement) >= 0
                else f"{float(improvement):.1f}% погіршення"
            )
            if improvement is not None
            else None
        )
        column.metric(
            str(row["label"]),
            (
                f"{format_number(row['before'], str(row['suffix']))} → "
                f"{format_number(row['after'], str(row['suffix']))}"
            ),
            delta=delta,
        )


def traffic_strip_html(
    snapshot: dict[str, float],
    max_queue: float,
    accent: str,
) -> str:
    queue = max(int(round(snapshot.get("queue_length", 0.0))), 0)
    active = max(int(round(snapshot.get("active_vehicles", 0.0))), 0)
    stopped_icons = "🚗" * min(queue, 12)
    moving_icons = "▸" * min(max(active - queue, 0), 12)
    load = min(queue / max(max_queue, 1.0), 1.0)
    return (
        f'<div style="background:#111827;border:2px solid {accent};'
        'border-radius:12px;padding:14px;margin:8px 0 14px 0;">'
        '<div style="height:7px;background:#374151;border-radius:4px;">'
        f'<div style="height:7px;width:{load * 100:.1f}%;background:{accent};'
        'border-radius:4px;"></div></div>'
        '<div style="font-size:23px;letter-spacing:2px;min-height:36px;'
        f'margin-top:10px;">{stopped_icons or "·"} '
        f'<span style="color:{accent}">{moving_icons}</span></div>'
        f'<div style="color:#d1d5db;font-size:13px;">{queue} авто в черзі · '
        f'{active} авто в зоні</div></div>'
    )


def render_comparison_panel(
    title: str,
    subtitle: str,
    snapshot: dict[str, float],
    max_queue: float,
    accent: str,
) -> None:
    st.markdown(f"### {title}")
    st.caption(subtitle)
    st.markdown(
        traffic_strip_html(snapshot, max_queue, accent),
        unsafe_allow_html=True,
    )
    first = st.columns(2)
    first[0].metric(
        "Черга зараз",
        format_number(snapshot.get("queue_length"), " авто", 0),
    )
    first[1].metric(
        "Очікування",
        format_number(snapshot.get("waiting_time"), " с", 1),
    )
    second = st.columns(3)
    second[0].metric(
        "Швидкість",
        format_number(snapshot.get("mean_speed", 0.0) * 3.6, " км/год", 1),
    )
    second[1].metric(
        "Проїхали",
        format_number(snapshot.get("throughput"), " авто", 0),
    )
    second[2].metric(
        "Зупинки",
        format_number(snapshot.get("stops_count"), "", 0),
    )


def render_visual_comparison(result_dir: Path) -> None:
    fixed = load_timeseries(result_dir, "static_fixed")
    if fixed is None or fixed.empty:
        fixed = load_timeseries(result_dir, "fixed")
    flowmind = load_timeseries(result_dir, "flowmind")
    if fixed is None or flowmind is None or fixed.empty or flowmind.empty:
        return
    fixed_times = pd.to_numeric(fixed["time"], errors="coerce").dropna()
    flowmind_times = pd.to_numeric(flowmind["time"], errors="coerce").dropna()
    if fixed_times.empty or flowmind_times.empty:
        return

    start_time = max(float(fixed_times.min()), float(flowmind_times.min()))
    end_time = min(float(fixed_times.max()), float(flowmind_times.max()))
    if end_time < start_time:
        return
    intervals = fixed_times.sort_values().diff().dropna()
    step = float(intervals.median()) if not intervals.empty else 1.0

    st.subheader("🚦 Наочне порівняння: Static Fixed vs FlowMind")
    st.caption(
        "Перетягніть час: обидві панелі показують той самий момент двох "
        "прогонів з однаковим сценарієм."
    )
    selected_time = (
        st.slider(
            "Момент симуляції",
            min_value=start_time,
            max_value=end_time,
            value=end_time,
            step=max(step, 1.0),
            format="%.0f с",
            key=f"visual-comparison-time::{result_dir.resolve()}",
        )
        if end_time > start_time
        else end_time
    )
    comparison = visual_comparison_data(fixed, flowmind, selected_time)
    if not comparison:
        return
    fixed_snapshot = comparison.get("fixed")
    flowmind_snapshot = comparison.get("flowmind")
    if not isinstance(fixed_snapshot, dict) or not isinstance(
        flowmind_snapshot, dict
    ):
        return

    left, right = st.columns(2, gap="large")
    with left:
        render_comparison_panel(
            "🔴 Static Fixed",
            "Працює за наперед заданою програмою та не бачить стан усієї зони.",
            fixed_snapshot,
            float(comparison["max_queue"]),
            "#ef4444",
        )
    with right:
        render_comparison_panel(
            "🟢 FlowMind",
            "Аналізує черги сусідніх перехресть і адаптує безпечні фази.",
            flowmind_snapshot,
            float(comparison["max_queue"]),
            "#10b981",
        )

    queue_difference = float(comparison["queue_difference"])
    waiting_difference = float(comparison["waiting_difference"])
    throughput_difference = float(comparison["throughput_difference"])
    if queue_difference >= 0 and waiting_difference >= 0:
        st.success(
            f"На {float(comparison['time']):.0f}-й секунді FlowMind має "
            f"на {queue_difference:.0f} авто меншу чергу та скорочує "
            f"поточне середнє очікування на {waiting_difference:.1f} с. "
            f"Додатково завершили маршрут: {throughput_difference:+.0f} авто."
        )
    else:
        st.warning(
            f"На {float(comparison['time']):.0f}-й секунді локальний стан "
            "FlowMind ще не кращий за static fixed. Оцінюйте також фінальні KPI: "
            "контролер може тимчасово накопичити чергу, щоб розвантажити "
            "сусідні перехрестя."
        )


def render_completed_run_charts(payload: dict[str, object] | None) -> None:
    if not isinstance(payload, dict) or simulation_status(payload) != "completed":
        return
    history = live_history_frame(payload)
    if history.empty or "time" not in history.columns:
        return

    st.subheader("Графіки завершеної симуляції")
    left, right = st.columns(2)
    flow_columns = [
        column
        for column in ["departed", "arrived", "active_vehicles"]
        if column in history.columns
    ]
    if flow_columns:
        flow_chart = px.line(
            history,
            x="time",
            y=flow_columns,
            labels={
                "time": "Час симуляції, с",
                "value": "Автомобілі",
                "variable": "Показник",
            },
            title="Потік машин через контрольовану зону",
        )
        left.plotly_chart(flow_chart, width="stretch")

    operation_columns = [
        column
        for column in ["queue_length", "waiting_time", "mean_speed"]
        if column in history.columns
    ]
    if operation_columns:
        operation_chart = px.line(
            history,
            x="time",
            y=operation_columns,
            labels={
                "time": "Час симуляції, с",
                "value": "Значення",
                "variable": "Показник",
            },
            title="Черга, очікування та швидкість",
        )
        right.plotly_chart(operation_chart, width="stretch")


@st.fragment(run_every=LIVE_REFRESH_INTERVAL)
def render_live_dashboard(result_dir: Path) -> None:
    client: LiveSocketClient = st.session_state.live_socket_client
    socket_payload = client.latest()
    file_payload = load_live_status(result_dir)
    live_status = (
        socket_payload
        if isinstance(socket_payload, dict)
        and socket_payload
        and socket_payload_matches(socket_payload, result_dir)
        else file_payload
    )

    st.subheader("1. 📡 Live-стан симуляції")
    connection = client.status
    age = payload_age_seconds(live_status) if isinstance(live_status, dict) else None
    if connection.get("connected"):
        delay = f", затримка {age:.1f} с" if age is not None else ""
        st.success(f"WebSocket підключено{delay}. Оновлення інтерфейсу: 1 с.")
    elif live_status is not None:
        reason = connection.get("last_error") or "очікування з’єднання"
        st.warning(f"WebSocket не підключено ({reason}); показано останній JSON snapshot.")
    else:
        st.info(
            "Очікую live-дані. Запустіть симуляцію; дашборд підключиться до "
            f"`{st.session_state.get('live_socket_url', 'ws://127.0.0.1:8765')}` "
            "автоматично."
        )
        return

    assert isinstance(live_status, dict)
    flow_value = live_status.get("traffic_flow", {})
    traffic_flow = flow_value if isinstance(flow_value, dict) else {}
    status = simulation_status(live_status)
    active = status in {"starting", "running"}
    state_key = f"live_active::{result_dir.resolve()}"
    was_active = bool(st.session_state.get(state_key, False))
    st.session_state[state_key] = active
    if was_active and status == "completed":
        st.rerun()

    averages = live_average_metrics(live_status)
    live_cols = st.columns(4)
    live_cols[0].metric(
        "Середньо авто в зоні",
        format_number(averages["active_vehicles"], "", 1),
    )
    live_cols[1].metric(
        "Середній вхідний потік",
        format_number(averages["inflow_per_minute"], " авто/хв", 1),
    )
    live_cols[2].metric(
        "Середній вихідний потік",
        format_number(averages["outflow_per_minute"], " авто/хв", 1),
    )
    live_cols[3].metric(
        "Середня черга",
        format_number(averages["queue_length"], " авто", 1),
    )

    quality_cols = st.columns(3)
    quality_cols[0].metric(
        "Середнє очікування",
        format_number(averages["waiting_time"], " с", 1),
    )
    quality_cols[1].metric(
        "Середня швидкість",
        format_number(averages["mean_speed"], " м/с", 1),
    )
    quality_cols[2].metric(
        "Середній gridlock risk",
        format_number(averages["gridlock_risk"], "", 3),
    )

    st.caption(
        f"Статус: **{status}** · час симуляції: "
        f"**{format_number(live_status.get('simulated_time'), ' с', 0)}** · "
        f"виїхало: **{format_number(traffic_flow.get('departed_total'), '', 0)}** · "
        f"завершили маршрут: "
        f"**{format_number(traffic_flow.get('arrived_total'), '', 0)}**"
    )

    corridor = corridor_status(live_status)
    corridor_state = str(corridor.get("corridor_state", "DISABLED")).upper()
    active_tls = corridor.get("corridor_active_tls")
    completed_tls = corridor.get("corridor_completed_tls", ())
    completed_count = (
        len(completed_tls)
        if isinstance(completed_tls, (list, tuple))
        else 0
    )
    if corridor_state == "GREEN_WINDOW":
        st.success(
            "🟢 **ЗЕЛЕНИЙ КОРИДОР АКТИВНИЙ** · "
            f"пріоритет на перехресті `{active_tls or '—'}` · "
            f"вже пройдено: {completed_count}"
        )
    elif corridor_state == "PREPARE":
        st.warning(
            "🟡 **ПІДГОТОВКА КОРИДОРУ** · "
            f"FlowMind готує зелений на `{active_tls or '—'}`"
        )
    elif corridor_state in {"CLEARANCE", "RECOVERY"}:
        st.info(
            "🔵 **ВІДНОВЛЕННЯ РУХУ** · "
            f"коридор завершив пріоритет, пройдено перехресть: {completed_count}"
        )

    intersections_value = live_status.get("intersections", [])
    intersections = (
        [item for item in intersections_value if isinstance(item, dict)]
        if isinstance(intersections_value, list)
        else []
    )
    if intersections:
        intersection_frame = pd.DataFrame(intersections)
        signals = intersection_frame.get("signal", pd.Series(dtype=str)).value_counts()
        mean_queue = pd.to_numeric(
            intersection_frame.get("incoming_queue", pd.Series(dtype=float)),
            errors="coerce",
        ).mean()
        mean_occupancy = pd.to_numeric(
            intersection_frame.get("outgoing_occupancy", pd.Series(dtype=float)),
            errors="coerce",
        ).mean()
        st.markdown(
            "#### Світлофори\n"
            f"Зелених: **{int(signals.get('green', 0))}**, "
            f"жовтих: **{int(signals.get('yellow', 0))}**, "
            f"червоних: **{int(signals.get('red', 0))}**. "
            f"Середня черга на перехрестя: **{format_number(mean_queue, ' авто', 1)}**, "
            f"середня вихідна зайнятість: "
            f"**{format_number(mean_occupancy * 100, '%', 1)}**."
        )
        with st.expander("Текстовий стан кожного світлофора"):
            for item in intersections:
                st.write(
                    f"`{item.get('tls_id', '—')}` — {item.get('signal', 'unknown')}, "
                    f"фаза {item.get('phase', '—')}, "
                    f"черга {item.get('incoming_queue', 0)}, "
                    f"авто на вході {item.get('incoming_vehicles', 0)}"
                )

    system_rows = live_system_rows(live_status)
    if system_rows:
        st.markdown("#### Як працюють компоненти системи")
        for row in system_rows:
            st.write(
                f"**{row['Компонент']}** — {row['Стан']}. {row['Деталі']}"
            )

    decisions = decision_log_rows(live_status)
    if decisions:
        st.markdown("#### 🧠 Що FlowMind робить зараз")
        icons = {
            "system": "⚙️",
            "route": "🗺️",
            "controller": "🚦",
            "corridor": "🚑",
        }
        for event in reversed(decisions[-7:]):
            category = str(event.get("category", "system"))
            st.markdown(
                f"{icons.get(category, '•')} "
                f"**{format_number(event.get('time'), ' с', 0)} — "
                f"{event.get('title', 'Рішення')}**  \n"
                f"{event.get('detail', '')}"
            )

    emergency_trace = live_status.get("emergency_trace")
    if isinstance(emergency_trace, list) and emergency_trace:
        latest_emergency = emergency_trace[-1]
        if isinstance(latest_emergency, dict):
            st.write(
                "**Швидка:** "
                f"ребро `{latest_emergency.get('edge_id', '—')}`, "
                f"швидкість {format_number(latest_emergency.get('speed'), ' м/с', 1)}, "
                f"залишилось ребер: {latest_emergency.get('remaining_edges', '—')}."
            )


st.title("🚦 FlowMind Dashboard")
st.caption(
    "Розширена статистика зонального керування світлофорами, "
    "порівняння режимів і контроль швидкої допомоги."
)

result_sets = _cached_result_sets(RESULTS_DIR)
if not result_sets and RESULTS_DIR != PROJECT_RESULTS_DIR:
    # Keep the explicitly requested directory selectable even when Streamlit
    # starts a moment before SUMO creates its first live snapshot.
    result_sets = [RESULTS_DIR.resolve()]
if not result_sets:
    result_sets = [PROJECT_RESULTS_DIR.resolve()]

labels = [display_path(path) for path in result_sets]
default_index = 0
for index, path in enumerate(result_sets):
    if path.resolve() == RESULTS_DIR.resolve():
        default_index = index
        break

with st.sidebar:
    st.header("Налаштування")
    selected_label = st.selectbox(
        "Набір результатів",
        labels,
        index=default_index,
        help="Manager передає сюди FLOWMIND_RESULTS_DIR, але можна переглядати й інші папки results/*.",
    )
    result_dir = result_sets[labels.index(selected_label)]
    st.caption(f"Папка: `{display_path(result_dir)}`")

summary = load_summary(result_dir)
live_ws_url = os.environ.get("FLOWMIND_LIVE_WS", "ws://127.0.0.1:8765")
if (
    "live_socket_client" not in st.session_state
    or st.session_state.get("live_socket_url") != live_ws_url
):
    previous_client = st.session_state.get("live_socket_client")
    if previous_client is not None:
        previous_client.stop()
    st.session_state.live_socket_client = LiveSocketClient(
        live_ws_url
    )
    st.session_state.live_socket_url = live_ws_url
    st.session_state.live_socket_client.start()

render_live_dashboard(result_dir)

page_live_status = load_live_status(result_dir)
page_simulation_status = simulation_status(page_live_status)
if page_simulation_status in {"starting", "running"}:
    st.info(
        "Симуляція виконується. Під час live-режиму показуються тільки "
        "усереднені та текстові значення. Графіки з’являться після завершення."
    )
    st.stop()
if page_simulation_status == "failed":
    system = page_live_status.get("system", {}) if page_live_status else {}
    simulation = system.get("simulation", {}) if isinstance(system, dict) else {}
    error = simulation.get("error", "невідома помилка") if isinstance(simulation, dict) else "невідома помилка"
    st.error(f"Симуляція завершилась з помилкою: {error}")
    st.stop()

if summary.empty:
    st.info(
        "Фінальний `summary.csv` з’явиться після завершення симуляції. "
        "Live-панель вище продовжує оновлюватися щосекунди."
    )
    st.stop()

render_before_after(summary)
render_visual_comparison(result_dir)
render_completed_run_charts(page_live_status)

st.subheader("2. Стан симуляції")
status_columns = st.columns(5)
duration = summary["simulated_duration"].max() if "simulated_duration" in summary else None
tls_count = summary["controlled_tls"].max() if "controlled_tls" in summary else None
status_columns[0].metric("Режимів у звіті", len(summary))
status_columns[1].metric("Тривалість", format_number(duration, " с", 0))
status_columns[2].metric("Світлофорів у зоні", format_number(tls_count, "", 0))
status_columns[3].metric(
    "Сумарний throughput",
    format_number(summary["throughput"].sum() if "throughput" in summary else None, "", 0),
)
status_columns[4].metric(
    "Gridlock risk",
    format_number(summary["gridlock_risk"].max() if "gridlock_risk" in summary else None, "", 2),
)

if "tls_ids" in summary.columns and not summary.empty:
    with st.expander("Контрольовані світлофори"):
        tls_ids = str(summary.iloc[0].get("tls_ids", "")).split(",")
        st.write("\n".join(f"- `{tls_id}`" for tls_id in tls_ids if tls_id))

st.subheader("2. ML-прогноз черг")
if "queue_forecast_enabled" not in summary.columns:
    st.info("У цьому наборі результатів ще немає ML forecast-полів.")
else:
    forecast_summary = summary.copy()
    forecast_summary["forecast_enabled"] = forecast_summary[
        "queue_forecast_enabled"
    ].map(truthy)
    enabled_rows = forecast_summary[forecast_summary["forecast_enabled"]]

    forecast_columns = st.columns(5)
    forecast_columns[0].metric("ML увімкнено", "так" if not enabled_rows.empty else "ні")
    forecast_columns[1].metric(
        "Моделей",
        format_number(
            enabled_rows["queue_forecast_model_count"].max()
            if "queue_forecast_model_count" in enabled_rows
            else None,
            "",
            0,
        ),
    )
    horizons = (
        str(enabled_rows.iloc[0].get("queue_forecast_horizons", ""))
        if not enabled_rows.empty
        else ""
    )
    forecast_columns[2].metric("Горизонти", horizons or "—")
    forecast_columns[3].metric(
        "Прогнозів",
        format_number(
            enabled_rows["queue_forecast_predictions"].sum()
            if "queue_forecast_predictions" in enabled_rows
            else None,
            "",
            0,
        ),
    )
    forecast_columns[4].metric(
        "Failures",
        format_number(
            enabled_rows["queue_forecast_failures"].sum()
            if "queue_forecast_failures" in enabled_rows
            else None,
            "",
            0,
        ),
    )

    visible_forecast_columns = [
        "label",
        "queue_forecast_enabled",
        "queue_forecast_model_count",
        "queue_forecast_horizons",
        "queue_forecast_horizon_weights",
        "queue_forecast_predictions",
        "queue_forecast_failures",
        "queue_forecast_trace_samples",
    ]
    visible_forecast_columns = [
        column for column in visible_forecast_columns if column in forecast_summary.columns
    ]
    if visible_forecast_columns:
        st.dataframe(
            forecast_summary[visible_forecast_columns],
            width="stretch",
            hide_index=True,
        )

    forecast_traces = [
        trace
        for mode in summary["mode"].astype(str)
        if (trace := load_queue_forecast_trace(result_dir, mode)) is not None
    ]
    if forecast_traces:
        forecast_trace = pd.concat(forecast_traces, ignore_index=True)
        forecast_chart = px.line(
            forecast_trace,
            x="time",
            y=["mean_prediction", "max_prediction"],
            color="tls_id",
            labels={
                "time": "Час симуляції, с",
                "value": "Прогнозована черга, авто",
                "variable": "Показник",
                "tls_id": "Світлофор",
            },
            title="Динаміка ML-прогнозу черги по світлофорах",
        )
        st.plotly_chart(forecast_chart, width="stretch")
    else:
        st.caption(
            "Forecast trace зʼявиться після запуску FlowMind з увімкненим ML-прогнозом."
        )

st.subheader("3. Головні KPI режимів")
kpi_metrics = [
    ("average_travel_time", "с"),
    ("average_waiting_time", "с"),
    ("average_queue_length", ""),
    ("throughput", ""),
]
kpi_columns = st.columns(len(kpi_metrics))
for column, (metric, suffix) in zip(kpi_columns, kpi_metrics, strict=True):
    row = best_row(summary, metric)
    with column:
        if row is None:
            st.metric(METRIC_LABELS[metric], "—")
        else:
            st.metric(METRIC_LABELS[metric], format_number(row[metric], f" {suffix}".rstrip()))
            st.caption(f"Найкраще: {row['label']}")

baseline_mode = select_baseline_mode(summary["mode"].astype(str)) or summary.iloc[0]["mode"]
baseline = summary[summary["mode"] == baseline_mode].iloc[0]
comparison_rows = []
for _, row in summary.iterrows():
    item = {"Режим": row["label"]}
    for metric in [
        "average_travel_time",
        "average_waiting_time",
        "average_queue_length",
        "throughput",
        "stops_count",
    ]:
        if metric not in summary.columns:
            continue
        value = row[metric]
        delta = pct_delta(float(value), float(baseline[metric])) if pd.notna(value) and pd.notna(baseline[metric]) else ""
        item[METRIC_LABELS[metric]] = f"{format_number(value)} ({delta})" if delta else format_number(value)
    comparison_rows.append(item)

st.dataframe(
    pd.DataFrame(comparison_rows),
    width="stretch",
    hide_index=True,
)
st.caption(f"Дельти рахуються відносно режиму: {MODE_LABELS.get(str(baseline_mode), baseline_mode)}.")

st.subheader("4. Порівняння метрик")
available_fields = [field for field in METRIC_LABELS if field in summary.columns]
selected_metric = st.selectbox(
    "Метрика на графіку",
    available_fields,
    format_func=lambda field: METRIC_LABELS.get(field, field),
)
figure = px.bar(
    summary,
    x="label",
    y=selected_metric,
    color="mode",
    color_discrete_map=MODE_COLORS,
    labels={"label": "Режим", selected_metric: METRIC_LABELS[selected_metric]},
    text_auto=".2s",
)
figure.update_layout(showlegend=False)
st.plotly_chart(figure, width="stretch")

timeseries_frames = [
    frame
    for mode in summary["mode"].astype(str)
    if (frame := load_timeseries(result_dir, mode)) is not None
]
if timeseries_frames:
    st.subheader("5. Динаміка по часу")
    timeseries_all = pd.concat(timeseries_frames, ignore_index=True)
    dynamic_candidates = [
        field
        for field in [
            "queue_length",
            "active_vehicles",
            "waiting_time",
            "mean_speed",
            "arrived",
            "departed",
            "gridlock_risk",
        ]
        if field in timeseries_all.columns
    ]
    selected_dynamic = st.multiselect(
        "Показники time-series",
        dynamic_candidates,
        default=dynamic_candidates[: min(3, len(dynamic_candidates))],
    )
    if selected_dynamic:
        melted = timeseries_all.melt(
            id_vars=["time", "mode", "label"],
            value_vars=selected_dynamic,
            var_name="metric",
            value_name="value",
        )
        dynamic_chart = px.line(
            melted,
            x="time",
            y="value",
            color="label",
            facet_row="metric" if len(selected_dynamic) > 1 else None,
            color_discrete_sequence=["#ef4444", "#f59e0b", "#10b981", "#3b82f6"],
            labels={"time": "Час симуляції, с", "value": "Значення", "label": "Режим"},
        )
        dynamic_chart.update_yaxes(matches=None)
        st.plotly_chart(dynamic_chart, width="stretch")

st.subheader("6. 🚑 Екстрені служби / швидка допомога")
selected_emergency = emergency_runs_for_result(result_dir, summary)
all_emergency = load_all_emergency_runs(discover_result_sets(PROJECT_RESULTS_DIR))

if selected_emergency.empty and all_emergency.empty:
    st.info(
        "У цьому наборі ще немає emergency-метрик. Запустіть demo зі швидкою "
        "або в manager увімкніть “Створити швидку”."
    )
else:
    emergency_source = selected_emergency if not selected_emergency.empty else all_emergency
    emergency_columns = st.columns(4)
    eta_values = emergency_source["eta"].combine_first(emergency_source.get("trace_eta"))
    best_eta_index = eta_values.idxmin() if not eta_values.dropna().empty else None
    best_eta = emergency_source.loc[best_eta_index] if best_eta_index is not None else None
    emergency_columns[0].metric(
        "Найкращий ETA",
        format_number(best_eta["eta"] if best_eta is not None else None, " с", 1),
    )
    emergency_columns[1].metric(
        "Прогонів зі швидкою",
        len(emergency_source),
    )
    emergency_columns[2].metric(
        "Найбільше пріоритетів",
        format_number(emergency_source["priority_decisions"].max(), "", 0)
        if "priority_decisions" in emergency_source
        else "—",
    )
    emergency_columns[3].metric(
        "Trace samples",
        format_number(emergency_source["trace_rows"].max(), "", 0)
        if "trace_rows" in emergency_source
        else "—",
    )

    emergency_view = emergency_source.copy()
    emergency_view["ETA, с"] = emergency_view["eta"].combine_first(emergency_view["trace_eta"])
    emergency_view["Помилка прогнозу, с"] = emergency_view["ETA, с"] - emergency_view["predicted_eta"]
    visible_columns = [
        "result_set",
        "label",
        "vehicle_id",
        "start",
        "destination",
        "departure",
        "arrival",
        "ETA, с",
        "predicted_eta",
        "Помилка прогнозу, с",
        "route_edges",
        "avg_speed",
        "max_speed",
        "priority_decisions",
        "trace_rows",
    ]
    visible_columns = [column for column in visible_columns if column in emergency_view.columns]
    st.dataframe(
        emergency_view[visible_columns].sort_values("ETA, с", na_position="last"),
        width="stretch",
        hide_index=True,
    )

    if not emergency_view["ETA, с"].dropna().empty:
        emergency_chart = px.bar(
            emergency_view.sort_values("ETA, с"),
            x="label",
            y="ETA, с",
            color="result_set",
            hover_data=[
                column
                for column in ["start", "destination", "predicted_eta", "priority_decisions", "trace_rows"]
                if column in emergency_view.columns
            ],
            labels={"label": "Режим / служба", "ETA, с": "ETA швидкої, с"},
            title="Порівняння часу доїзду швидкої",
        )
        st.plotly_chart(emergency_chart, width="stretch")

    if {"predicted_eta", "ETA, с"}.issubset(emergency_view.columns):
        prediction_rows = emergency_view.dropna(subset=["predicted_eta", "ETA, с"])
        if not prediction_rows.empty:
            prediction_chart = px.scatter(
                prediction_rows,
                x="predicted_eta",
                y="ETA, с",
                color="result_set",
                symbol="label",
                hover_data=["start", "destination"],
                labels={
                    "predicted_eta": "Прогноз ETA, с",
                    "ETA, с": "Фактичний ETA, с",
                },
                title="Прогноз маршруту швидкої vs фактичний доїзд",
            )
            st.plotly_chart(prediction_chart, width="stretch")

    trace_modes = [
        mode
        for mode in sorted(set(summary["mode"].astype(str)), key=lambda item: MODE_ORDER.get(item, 99))
        if load_trace(result_dir, mode) is not None
    ]
    if trace_modes:
        selected_trace_mode = st.selectbox(
            "Трек швидкої для перегляду",
            trace_modes,
            format_func=lambda value: MODE_LABELS.get(value, value),
        )
        trace = load_trace(result_dir, selected_trace_mode)
        if trace is not None:
            route_chart = px.line(
                trace,
                x="x",
                y="y",
                markers=True,
                hover_data=["time", "edge_id", "speed", "remaining_edges"],
                labels={"x": "SUMO X", "y": "SUMO Y"},
                title="Маршрут швидкої у SUMO-координатах",
            )
            route_chart.update_yaxes(scaleanchor="x", scaleratio=1)
            st.plotly_chart(route_chart, width="stretch")

            telemetry_columns = [
                column
                for column in ["speed", "remaining_edges", "route_index"]
                if column in trace.columns
            ]
            telemetry_chart = px.line(
                trace,
                x="time",
                y=telemetry_columns,
                labels={
                    "time": "Час симуляції, с",
                    "value": "Значення",
                    "variable": "Показник",
                },
                title="Телеметрія швидкої",
            )
            st.plotly_chart(telemetry_chart, width="stretch")

    with st.expander("Порівняння швидкої по всіх наборах results"):
        if all_emergency.empty:
            st.info("Не знайдено emergency-прогонів у `results`.")
        else:
            all_view = all_emergency.copy()
            all_view["ETA, с"] = all_view["eta"].combine_first(all_view["trace_eta"])
            st.dataframe(
                all_view[
                    [
                        column
                        for column in [
                            "result_set",
                            "label",
                            "vehicle_id",
                            "ETA, с",
                            "predicted_eta",
                            "route_edges",
                            "priority_decisions",
                            "trace_rows",
                            "avg_speed",
                        ]
                        if column in all_view.columns
                    ]
                ].sort_values(["result_set", "ETA, с"], na_position="last"),
                width="stretch",
                hide_index=True,
            )

with st.expander("Повна таблиця результатів"):
    st.dataframe(summary.drop(columns=["mode_order"], errors="ignore"), width="stretch")
