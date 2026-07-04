from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

import pandas as pd
import plotly.express as px
import streamlit as st


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_RESULTS_DIR = PROJECT_ROOT / "results"
RESULTS_DIR = Path(os.environ.get("FLOWMIND_RESULTS_DIR", str(PROJECT_RESULTS_DIR)))

MODE_LABELS = {
    "fixed": "Fixed",
    "local": "Local Adaptive",
    "flowmind": "FlowMind Area Balance",
}
MODE_COLORS = {
    "fixed": "#ef4444",
    "local": "#f59e0b",
    "flowmind": "#10b981",
}
MODE_ORDER = {"fixed": 0, "local": 1, "flowmind": 2}

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
]


st.set_page_config(page_title="FlowMind Dashboard", page_icon="🚦", layout="wide")


def display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path.resolve())


def discover_result_sets(base_dir: Path) -> list[Path]:
    candidates = []
    if (base_dir / "summary.csv").exists():
        candidates.append(base_dir)
    if base_dir.exists():
        candidates.extend(
            path.parent
            for path in sorted(base_dir.glob("**/summary.csv"))
            if path.parent not in candidates
        )
    return candidates


def load_summary(result_dir: Path) -> pd.DataFrame:
    summary = pd.read_csv(result_dir / "summary.csv")
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


st.title("🚦 FlowMind Dashboard")
st.caption(
    "Розширена статистика зонального керування світлофорами, "
    "порівняння режимів і контроль швидкої допомоги."
)

result_sets = discover_result_sets(RESULTS_DIR)
if not result_sets and RESULTS_DIR != PROJECT_RESULTS_DIR:
    result_sets = discover_result_sets(PROJECT_RESULTS_DIR)

if not result_sets:
    st.info(
        "Результатів ще немає. Запустіть порівняння, наприклад: "
        "`python experiments/run_comparison.py --duration 900`"
    )
    st.stop()

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

st.subheader("1. Стан симуляції")
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

st.subheader("2. Головні KPI режимів")
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

baseline_mode = "fixed" if "fixed" in set(summary["mode"]) else summary.iloc[0]["mode"]
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

st.subheader("3. Порівняння метрик")
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
    st.subheader("4. Динаміка по часу")
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

st.subheader("5. 🚑 Екстрені служби / швидка допомога")
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
