from __future__ import annotations

from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = PROJECT_ROOT / "results"
MODE_LABELS = {
    "fixed": "Fixed",
    "local": "Local Adaptive",
    "flowmind": "FlowMind Area Balance",
}

st.set_page_config(page_title="FlowMind Rivne", page_icon="🚦", layout="wide")
st.title("FlowMind Rivne")
st.caption("Порівняння режимів керування світлофорною зоною")

summary_path = RESULTS_DIR / "summary.csv"
if not summary_path.exists():
    st.info(
        "Результатів ще немає. Запустіть: "
        "`python experiments/run_comparison.py --duration 900`"
    )
    st.stop()

summary = pd.read_csv(summary_path)
summary["label"] = summary["mode"].map(MODE_LABELS).fillna(summary["mode"])

metrics = [
    ("average_travel_time", "Середній час поїздки, с", "min"),
    ("average_waiting_time", "Середнє очікування, с", "min"),
    ("average_queue_length", "Середня черга, авто", "min"),
    ("throughput", "Пропускна здатність, авто", "max"),
]
columns = st.columns(4)
for column, (field, label, preference) in zip(columns, metrics, strict=True):
    values = pd.to_numeric(summary[field], errors="coerce")
    best_index = values.idxmin() if preference == "min" else values.idxmax()
    with column:
        st.metric(label, f"{values.loc[best_index]:.1f}")
        st.caption(f"Найкраще: {summary.loc[best_index, 'label']}")

comparison_fields = [
    "average_travel_time",
    "average_waiting_time",
    "average_queue_length",
    "max_queue_length",
    "stops_count",
    "gridlock_risk",
]
available_fields = [field for field in comparison_fields if field in summary]
selected_metric = st.selectbox(
    "Метрика для порівняння",
    available_fields,
    format_func=lambda field: dict((item[0], item[1]) for item in metrics).get(
        field, field.replace("_", " ").title()
    ),
)
figure = px.bar(
    summary,
    x="label",
    y=selected_metric,
    color="mode",
    color_discrete_map={
        "fixed": "#ef4444",
        "local": "#f59e0b",
        "flowmind": "#10b981",
    },
    labels={"label": "Режим", selected_metric: selected_metric.replace("_", " ")},
)
figure.update_layout(showlegend=False)
st.plotly_chart(figure, width="stretch")

mode = st.selectbox(
    "Динаміка режиму",
    summary["mode"].tolist(),
    format_func=lambda value: MODE_LABELS.get(value, value),
)
timeseries_path = RESULTS_DIR / f"{mode}_timeseries.csv"
if timeseries_path.exists():
    timeseries = pd.read_csv(timeseries_path)
    chart = px.line(
        timeseries,
        x="time",
        y=["queue_length", "active_vehicles", "waiting_time"],
        labels={"time": "Час симуляції, с", "value": "Значення", "variable": "KPI"},
    )
    st.plotly_chart(chart, width="stretch")

with st.expander("Повна таблиця результатів"):
    st.dataframe(summary.drop(columns=["label"]), width="stretch")
