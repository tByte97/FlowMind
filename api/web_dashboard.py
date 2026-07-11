from __future__ import annotations

import csv
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from fastapi import FastAPI, Request
    from fastapi.responses import FileResponse, HTMLResponse
except ImportError:
    FastAPI = None  # type: ignore[assignment]
    FileResponse = None  # type: ignore[assignment]
    Request = None  # type: ignore[assignment]
    HTMLResponse = None  # type: ignore[assignment]


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_RESULTS_DIR = PROJECT_ROOT / "results"
CAR_ICON_PATH = PROJECT_ROOT / "icon_car.png"
DASHBOARD_ASSETS_DIR = PROJECT_ROOT / "dashboard" / "assets"
ZONE_SIMULATION_CSS_PATH = DASHBOARD_ASSETS_DIR / "zone_simulation.css"
ZONE_SIMULATION_JS_PATH = DASHBOARD_ASSETS_DIR / "zone_simulation.js"
RESULTS_DIR = Path(os.environ.get("FLOWMIND_RESULTS_DIR", PROJECT_RESULTS_DIR))
WEB_RESULTS_DIR = Path(
    os.environ.get("FLOWMIND_WEB_RESULTS_DIR", PROJECT_RESULTS_DIR / "web_demo")
)
MAX_HISTORY_POINTS = 180
MAX_DECISION_ROWS = 80
MAX_TABLE_ROWS = 120
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")
GEMINI_SUMMARY_FILE = "gemini_summary.json"
MODE_ORDER = {
    "static_fixed": 0,
    "sumo_actuated": 1,
    "local": 2,
    "flowmind": 3,
    "fixed": 4,
}
AVERAGE_METRICS = {
    "average_travel_time": "Сер. час поїздки, с",
    "average_waiting_time": "Сер. очікування, с",
    "average_queue_length": "Сер. черга, авто",
    "max_queue_length": "Макс. черга, авто",
    "throughput": "Пропуск, авто",
    "stops_count": "Зупинки",
    "gridlock_risk": "Gridlock risk",
    "controller_decisions": "Рішення контролера",
    "phase_extensions": "Продовження зеленого",
    "phase_advances": "Перемикання фаз",
    "priority_decisions": "Пріоритети швидкої",
    "queue_forecast_predictions": "ML-прогнози",
    "sensor_range_meters": "Радіус датчиків, м",
    "simulated_duration": "Тривалість, с",
}


def _python_executable() -> Path:
    candidate = PROJECT_ROOT / ".venv" / "bin" / "python"
    return candidate if candidate.exists() else Path(sys.executable)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _safe_stat_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT.resolve()))
    except ValueError:
        return str(path.resolve())


def _result_id(path: Path) -> str:
    return hashlib.sha1(str(path.resolve()).encode("utf-8")).hexdigest()[:16]


def _as_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_summary_value(value: str) -> Any:
    number = _as_float(value)
    if number is None:
        return value
    if number.is_integer():
        return int(number)
    return number


def _read_summary_rows(path: Path) -> list[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            rows = [
                {key: _coerce_summary_value(value) for key, value in row.items()}
                for row in csv.DictReader(handle)
            ]
    except OSError:
        return []
    return rows


def summary_rows_for_result(result_dir: Path) -> list[dict[str, Any]]:
    summary_path = result_dir / "summary.csv"
    if summary_path.exists():
        return _read_summary_rows(summary_path)
    live_path = result_dir / "live_status.json"
    if not live_path.exists():
        return []
    payload = _read_json(live_path)
    summary = payload.get("summary")
    if isinstance(summary, dict) and summary:
        return [dict(summary)]
    return []


def primary_summary_for_result(result_dir: Path) -> dict[str, Any]:
    rows = summary_rows_for_result(result_dir)
    if rows:
        return rows[-1]
    return {}


def find_latest_live_status(
    base_dir: Path = RESULTS_DIR,
    preferred_dir: Path | None = None,
) -> Path | None:
    candidates: list[Path] = []
    if preferred_dir is not None:
        preferred = preferred_dir / "live_status.json"
        if preferred.exists():
            candidates.append(preferred)
    direct = base_dir / "live_status.json"
    if direct.exists():
        candidates.append(direct)
    if WEB_RESULTS_DIR.exists():
        candidates.extend(WEB_RESULTS_DIR.glob("**/live_status.json"))
    if base_dir.exists():
        candidates.extend(base_dir.glob("**/live_status.json"))
    unique = {path.resolve(): path for path in candidates}
    if not unique:
        return None
    return max(unique.values(), key=_safe_stat_mtime)


def discover_result_sets(base_dir: Path = RESULTS_DIR) -> list[dict[str, Any]]:
    markers: list[Path] = []
    if base_dir.exists():
        markers.extend(base_dir.glob("**/live_status.json"))
        markers.extend(base_dir.glob("**/summary.csv"))
    try:
        include_web_results = base_dir.resolve() == RESULTS_DIR.resolve()
    except OSError:
        include_web_results = base_dir == RESULTS_DIR
    if include_web_results and WEB_RESULTS_DIR.exists() and WEB_RESULTS_DIR != base_dir:
        markers.extend(WEB_RESULTS_DIR.glob("**/live_status.json"))
        markers.extend(WEB_RESULTS_DIR.glob("**/summary.csv"))

    result_dirs: dict[Path, dict[str, Any]] = {}
    for marker in markers:
        result_dir = marker.parent.resolve()
        entry = result_dirs.setdefault(
            result_dir,
            {
                "id": _result_id(result_dir),
                "path": _display_path(result_dir),
                "updated_at": 0.0,
                "has_live_status": False,
                "has_summary": False,
                "mode": None,
                "summary": {},
                "summary_rows": [],
            },
        )
        entry["updated_at"] = max(entry["updated_at"], _safe_stat_mtime(marker))
        if marker.name == "live_status.json":
            entry["has_live_status"] = True
            payload = _read_json(marker)
            entry["mode"] = payload.get("mode") or entry["mode"]
            summary = payload.get("summary")
            if isinstance(summary, dict) and summary and not entry["summary"]:
                entry["summary"] = dict(summary)
                entry["summary_rows"] = [dict(summary)]
                if summary.get("mode"):
                    entry["modes"] = [str(summary["mode"])]
        if marker.name == "summary.csv":
            entry["has_summary"] = True
            rows = _read_summary_rows(marker)
            entry["summary_rows"] = rows
            entry["summary"] = rows[-1] if rows else {}
            entry["modes"] = sorted(
                {str(row.get("mode")) for row in rows if row.get("mode")}
            )
            if entry["mode"] is None and rows:
                entry["mode"] = rows[-1].get("mode")

    return sorted(
        result_dirs.values(),
        key=lambda item: float(item.get("updated_at", 0.0)),
        reverse=True,
    )


def _summary_modes(path: Path) -> list[str]:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            modes = [str(row.get("mode", "")) for row in reader if row.get("mode")]
    except OSError:
        return []
    return sorted(set(modes))


def find_result_dir_by_id(
    result_id: str,
    base_dir: Path | None = None,
) -> Path | None:
    search_dir = base_dir or RESULTS_DIR
    for entry in discover_result_sets(search_dir):
        if entry.get("id") == result_id:
            path = entry.get("path")
            if not isinstance(path, str):
                continue
            result_dir = PROJECT_ROOT / path
            if result_dir.exists():
                return result_dir.resolve()
    return None


def build_archive_payload(base_dir: Path = RESULTS_DIR) -> dict[str, Any]:
    results = discover_result_sets(base_dir)
    mode_counts: dict[str, int] = {}
    for result in results:
        mode = str(result.get("mode") or "unknown")
        mode_counts[mode] = mode_counts.get(mode, 0) + 1
    return {
        "results": results,
        "total": len(results),
        "mode_counts": mode_counts,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def build_result_detail_payload(result_id: str) -> dict[str, Any]:
    result_dir = find_result_dir_by_id(result_id)
    if result_dir is None:
        return {"available": False, "id": result_id}
    live_path = result_dir / "live_status.json"
    payload = _trim_live_payload(_read_json(live_path)) if live_path.exists() else {}
    payload["available"] = bool(payload)
    payload["id"] = result_id
    payload["path"] = _display_path(result_dir)
    payload["summary_rows"] = summary_rows_for_result(result_dir)
    payload["summary"] = payload.get("summary") or primary_summary_for_result(result_dir)
    payload["_meta"] = {
        "source": _display_path(live_path if live_path.exists() else result_dir),
        "updated_at": datetime.fromtimestamp(
            _safe_stat_mtime(live_path if live_path.exists() else result_dir),
            tz=timezone.utc,
        ).isoformat(),
    }
    return payload


def build_averages_payload(base_dir: Path = RESULTS_DIR) -> dict[str, Any]:
    grouped: dict[str, dict[str, list[float]]] = {}
    result_count_by_mode: dict[str, int] = {}
    total_rows = 0
    for result in discover_result_sets(base_dir):
        path = result.get("path")
        if not isinstance(path, str):
            continue
        result_dir = PROJECT_ROOT / path
        for row in summary_rows_for_result(result_dir):
            mode = str(row.get("mode") or result.get("mode") or "unknown")
            total_rows += 1
            result_count_by_mode[mode] = result_count_by_mode.get(mode, 0) + 1
            metrics = grouped.setdefault(mode, {})
            for key in AVERAGE_METRICS:
                value = _as_float(row.get(key))
                if value is not None:
                    metrics.setdefault(key, []).append(value)

    modes: list[dict[str, Any]] = []
    for mode, metric_values in grouped.items():
        metrics = {
            key: {
                "label": AVERAGE_METRICS[key],
                "average": sum(values) / len(values),
                "min": min(values),
                "max": max(values),
                "count": len(values),
            }
            for key, values in metric_values.items()
            if values
        }
        modes.append(
            {
                "mode": mode,
                "count": result_count_by_mode.get(mode, 0),
                "metrics": metrics,
            }
        )

    modes.sort(key=lambda item: (MODE_ORDER.get(str(item["mode"]), 99), str(item["mode"])))
    return {
        "modes": modes,
        "metrics": AVERAGE_METRICS,
        "total_rows": total_rows,
        "total_results": len(discover_result_sets(base_dir)),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _trim_live_payload(payload: dict[str, Any]) -> dict[str, Any]:
    trimmed = dict(payload)
    for key, limit in {
        "metric_history": MAX_HISTORY_POINTS,
        "decision_log": MAX_DECISION_ROWS,
        "vehicles": MAX_TABLE_ROWS,
        "lanes": MAX_TABLE_ROWS,
    }.items():
        value = trimmed.get(key)
        if isinstance(value, list):
            trimmed[key] = value[-limit:]
    intersections = trimmed.get("intersections")
    if isinstance(intersections, list):
        trimmed["intersections"] = intersections[:MAX_TABLE_ROWS]
    return trimmed


def build_status_payload(manager: "DemoProcessManager") -> dict[str, Any]:
    path = find_latest_live_status(RESULTS_DIR, manager.current_results_dir)
    process = manager.snapshot()
    if path is None:
        return {
            "available": False,
            "mode": None,
            "summary": {},
            "latest_sample": {},
            "metric_history": [],
            "intersections": [],
            "decision_log": [],
            "system": {
                "simulation": {"status": "waiting"},
                "metrics": {
                    "sensor_model": "intersection_camera_detector",
                    "coverage": "controlled_intersections_only",
                },
            },
            "process": process,
            "_meta": {"source": None, "updated_at": None},
        }

    payload = _trim_live_payload(_read_json(path))
    payload["available"] = True
    payload["process"] = process
    payload["summary_rows"] = summary_rows_for_result(path.parent)
    payload["summary"] = payload.get("summary") or primary_summary_for_result(path.parent)
    payload["_meta"] = {
        "source": _display_path(path),
        "updated_at": datetime.fromtimestamp(
            _safe_stat_mtime(path), tz=timezone.utc
        ).isoformat(),
    }
    return payload


def _format_metric(value: Any, suffix: str = "", digits: int = 1) -> str:
    number = _as_float(value)
    if number is None:
        return "немає"
    if abs(number - round(number)) < 0.001:
        text = str(int(round(number)))
    else:
        text = f"{number:.{digits}f}"
    return f"{text}{suffix}"


def _percent_delta(before: Any, after: Any, lower_is_better: bool = True) -> float | None:
    before_number = _as_float(before)
    after_number = _as_float(after)
    if before_number in (None, 0) or after_number is None:
        return None
    delta = before_number - after_number if lower_is_better else after_number - before_number
    return (delta / before_number) * 100


def _delta(before: Any, after: Any, lower_is_better: bool = True) -> float | None:
    before_number = _as_float(before)
    after_number = _as_float(after)
    if before_number is None or after_number is None:
        return None
    return before_number - after_number if lower_is_better else after_number - before_number


def build_summary_context(payload: dict[str, Any]) -> dict[str, Any]:
    rows = payload.get("summary_rows")
    if not isinstance(rows, list):
        rows = []
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    fixed = next(
        (row for row in rows if row.get("mode") == "static_fixed"),
        None,
    ) or next((row for row in rows if row.get("mode") == "fixed"), None)
    flow = next((row for row in rows if row.get("mode") == "flowmind"), None) or summary
    latest = payload.get("latest_sample") if isinstance(payload.get("latest_sample"), dict) else {}
    history = payload.get("metric_history") if isinstance(payload.get("metric_history"), list) else []
    peak_queue = max(
        [_as_float(row.get("queue_length")) or 0 for row in history] + [_as_float(summary.get("max_queue_length")) or 0]
    )
    return {
        "mode": payload.get("mode") or flow.get("mode"),
        "duration": flow.get("simulated_duration") or summary.get("simulated_duration"),
        "sensor_range": flow.get("sensor_range_meters") or summary.get("sensor_range_meters"),
        "controlled_tls": flow.get("controlled_tls") or summary.get("controlled_tls"),
        "fixed": fixed,
        "flowmind": flow,
        "latest": latest,
        "peak_queue": peak_queue,
        "improvements": {
            "waiting_time_delta": _delta(
                fixed.get("average_waiting_time") if fixed else None,
                flow.get("average_waiting_time"),
            ),
            "waiting_time_percent": _percent_delta(
                fixed.get("average_waiting_time") if fixed else None,
                flow.get("average_waiting_time"),
            ),
            "queue_delta": _delta(
                fixed.get("average_queue_length") if fixed else None,
                flow.get("average_queue_length"),
            ),
            "queue_percent": _percent_delta(
                fixed.get("average_queue_length") if fixed else None,
                flow.get("average_queue_length"),
            ),
            "throughput_delta": _delta(
                fixed.get("throughput") if fixed else None,
                flow.get("throughput"),
                lower_is_better=False,
            ),
            "throughput_percent": _percent_delta(
                fixed.get("throughput") if fixed else None,
                flow.get("throughput"),
                lower_is_better=False,
            ),
        },
    }


def build_local_report(context: dict[str, Any]) -> str:
    flow = context.get("flowmind") or {}
    fixed = context.get("fixed")
    improvements = context.get("improvements") or {}
    if fixed:
        return (
            "FlowMind завершив порівняльну симуляцію зі static fixed baseline. "
            f"Середній час очікування змінився з {_format_metric(fixed.get('average_waiting_time'), ' с')} "
            f"до {_format_metric(flow.get('average_waiting_time'), ' с')}, тобто покращення становить "
            f"{_format_metric(improvements.get('waiting_time_percent'), '%')}. "
            f"Середня черга змінилася з {_format_metric(fixed.get('average_queue_length'), ' авто')} "
            f"до {_format_metric(flow.get('average_queue_length'), ' авто')}. "
            f"Пропускна здатність: static fixed {_format_metric(fixed.get('throughput'), ' авто', 0)}, "
            f"FlowMind {_format_metric(flow.get('throughput'), ' авто', 0)}. "
            f"Пікова черга в live-історії: {_format_metric(context.get('peak_queue'), ' авто', 0)}. "
            "Висновок: система краще підлаштовується під потік і дає зрозумілий ефект для демонстрації."
        )
    return (
        "FlowMind завершив симуляцію без static fixed baseline. "
        f"Середній час очікування: {_format_metric(flow.get('average_waiting_time'), ' с')}, "
        f"середня черга: {_format_metric(flow.get('average_queue_length'), ' авто')}, "
        f"пропускна здатність: {_format_metric(flow.get('throughput'), ' авто', 0)}. "
        f"Пікова черга в live-історії: {_format_metric(context.get('peak_queue'), ' авто', 0)}. "
        "Для повного порівняльного висновку запусти симуляцію з увімкненим режимом static fixed + FlowMind."
    )


def build_gemini_prompt(context: dict[str, Any]) -> str:
    return (
        "Ти технічний аналітик системи керування світлофорами FlowMind. "
        "Сформуй короткий, презентаційний висновок українською мовою для журі. "
        "Використовуй тільки наведені JSON-дані, не вигадуй цифри, не згадуй Gemini. "
        "Структура: 1 абзац підсумку, 3 короткі bullet-пункти з ключовими метриками, "
        "1 речення про практичну користь. Дані:\n"
        f"{json.dumps(context, ensure_ascii=False, indent=2, default=str)}"
    )


def generate_gemini_text(context: dict[str, Any]) -> tuple[str | None, str | None]:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None, "GEMINI_API_KEY is not set"
    try:
        from google import genai
        from google.genai import types
    except ImportError as error:
        return None, f"google-genai is not installed: {error}"

    try:
        client = genai.Client(api_key=api_key)
        contents = [
            types.Content(
                role="user",
                parts=[types.Part.from_text(text=build_gemini_prompt(context))],
            )
        ]
        config = types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(thinking_level="MINIMAL"),
        )
        chunks: list[str] = []
        for chunk in client.models.generate_content_stream(
            model=GEMINI_MODEL,
            contents=contents,
            config=config,
        ):
            text = getattr(chunk, "text", None)
            if text:
                chunks.append(text)
        generated = "".join(chunks).strip()
    except Exception as error:  # external API should never break the dashboard
        return None, str(error)
    return (generated or None), None if generated else "Gemini returned an empty response"


def _context_fingerprint(context: dict[str, Any]) -> str:
    raw = json.dumps(context, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _result_dir_for_report(
    manager: "DemoProcessManager",
    result_id: str | None = None,
) -> Path | None:
    if result_id:
        return find_result_dir_by_id(result_id)
    path = find_latest_live_status(RESULTS_DIR, manager.current_results_dir)
    return path.parent if path is not None else None


def build_ai_report_payload(
    manager: "DemoProcessManager",
    result_id: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    process = manager.snapshot()
    if process.get("running"):
        return {
            "available": False,
            "status": "pending",
            "provider": "none",
            "text": "Симуляція ще виконується. Висновок буде доступний після завершення.",
            "process": process,
        }

    result_dir = _result_dir_for_report(manager, result_id)
    if result_dir is None:
        return {
            "available": False,
            "status": "missing",
            "provider": "none",
            "text": "Немає завершеного результату для аналізу.",
            "process": process,
        }

    live_path = result_dir / "live_status.json"
    payload = _trim_live_payload(_read_json(live_path)) if live_path.exists() else {}
    payload["summary_rows"] = summary_rows_for_result(result_dir)
    payload["summary"] = payload.get("summary") or primary_summary_for_result(result_dir)
    context = build_summary_context(payload)
    fingerprint = _context_fingerprint(context)
    cache_path = result_dir / GEMINI_SUMMARY_FILE
    cached = _read_json(cache_path)
    if (
        not force
        and cached.get("provider") == "gemini"
        and cached.get("fingerprint") == fingerprint
        and cached.get("text")
    ):
        cached["cached"] = True
        return cached

    fallback_text = build_local_report(context)
    gemini_text, error = generate_gemini_text(context)
    result = {
        "available": True,
        "status": "generated" if gemini_text else "fallback",
        "provider": "gemini" if gemini_text else "local",
        "model": GEMINI_MODEL if gemini_text else None,
        "text": gemini_text or fallback_text,
        "fallback_text": fallback_text,
        "error": error,
        "fingerprint": fingerprint,
        "result_path": _display_path(result_dir),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cached": False,
    }
    if gemini_text:
        try:
            cache_path.write_text(
                json.dumps(result, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass
    return result


def find_free_port(preferred: int, attempts: int = 50) -> int:
    for port in range(preferred, preferred + attempts):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind(("127.0.0.1", port))
                return port
        except PermissionError:
            return preferred
        except OSError:
            continue
    return preferred


def _int_option(
    options: dict[str, Any],
    key: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(options.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _float_option(
    options: dict[str, Any],
    key: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    try:
        value = float(options.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _bool_option(options: dict[str, Any], key: str, default: bool) -> bool:
    value = options.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes", "on"}
    return bool(value)


def mark_live_snapshot_inactive(
    results_dir: Path | None,
    status: str = "stopped",
) -> None:
    """Immediately disable the live-only map after this dashboard stops a run."""

    if results_dir is None:
        return
    output_path = results_dir / "live_status.json"
    payload = _read_json(output_path)
    if not payload:
        return
    system = payload.get("system")
    if not isinstance(system, dict):
        system = {}
    simulation = system.get("simulation")
    if not isinstance(simulation, dict):
        simulation = {}
    simulation["status"] = status
    system["simulation"] = simulation
    payload["system"] = system
    zone = payload.get("zone_simulation")
    if isinstance(zone, dict):
        zone["status"] = status
        zone["active"] = False
    payload["emitted_at"] = datetime.now(timezone.utc).isoformat()
    temporary_path = output_path.with_suffix(".json.tmp")
    try:
        temporary_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary_path.replace(output_path)
    except OSError:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass


class DemoProcessManager:
    def __init__(self) -> None:
        self._process: subprocess.Popen[str] | None = None
        self._reader_thread: threading.Thread | None = None
        self._logs: deque[str] = deque(maxlen=400)
        self._lock = threading.Lock()
        self._status = "idle"
        self._started_at: float | None = None
        self._finished_at: float | None = None
        self._exit_code: int | None = None
        self._command: list[str] = []
        self._results_dir: Path | None = None

    @property
    def current_results_dir(self) -> Path | None:
        with self._lock:
            return self._results_dir

    def start(self, options: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return self.snapshot_unlocked()

            command, results_dir = build_demo_command(options)
            results_dir.mkdir(parents=True, exist_ok=True)
            environment = os.environ.copy()
            environment["PYTHONUNBUFFERED"] = "1"
            environment["FLOWMIND_RESULTS_DIR"] = str(results_dir)
            self._logs.clear()
            self._logs.append("Starting FlowMind web demo...")
            try:
                process = subprocess.Popen(
                    command,
                    cwd=str(PROJECT_ROOT),
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    start_new_session=os.name != "nt",
                )
            except OSError as error:
                self._status = "failed"
                self._exit_code = None
                self._logs.append(f"Could not start demo: {error}")
                return self.snapshot_unlocked()

            self._process = process
            self._status = "running"
            self._started_at = time.time()
            self._finished_at = None
            self._exit_code = None
            self._command = command
            self._results_dir = results_dir
            self._reader_thread = threading.Thread(
                target=self._capture_logs,
                args=(process,),
                daemon=True,
            )
            self._reader_thread.start()
            return self.snapshot_unlocked()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None:
                self._status = "idle" if self._status == "running" else self._status
                return self.snapshot_unlocked()
            self._status = "stopping"
            self._logs.append("Stopping FlowMind demo...")

        try:
            if os.name == "nt":
                process.terminate()
            else:
                os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        except OSError:
            process.terminate()

        with self._lock:
            self._exit_code = process.poll()
            self._finished_at = time.time()
            self._status = "stopped"
            snapshot = self.snapshot_unlocked()
            results_dir = self._results_dir
        mark_live_snapshot_inactive(results_dir)
        return snapshot

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self.snapshot_unlocked()

    def snapshot_unlocked(self) -> dict[str, Any]:
        process = self._process
        exit_code = self._exit_code
        if process is not None:
            polled = process.poll()
            if polled is not None:
                exit_code = polled
                if self._status in {"running", "stopping"}:
                    self._status = "completed" if polled == 0 else "failed"
                    self._finished_at = self._finished_at or time.time()
        return {
            "status": self._status,
            "running": process is not None and process.poll() is None,
            "pid": process.pid if process is not None else None,
            "exit_code": exit_code,
            "started_at": self._started_at,
            "finished_at": self._finished_at,
            "results_dir": (
                _display_path(self._results_dir)
                if self._results_dir is not None
                else None
            ),
            "command": self._command,
            "logs": list(self._logs)[-120:],
        }

    def _capture_logs(self, process: subprocess.Popen[str]) -> None:
        stream = process.stdout
        if stream is not None:
            for line in stream:
                clean = line.rstrip()
                if clean:
                    with self._lock:
                        self._logs.append(clean)
        exit_code = process.wait()
        with self._lock:
            if self._process is process:
                self._exit_code = exit_code
                self._finished_at = time.time()
                if self._status not in {"stopped", "stopping"}:
                    self._status = "completed" if exit_code == 0 else "failed"
                self._logs.append(f"Demo finished with exit code {exit_code}.")


def build_demo_command(options: dict[str, Any]) -> tuple[list[str], Path]:
    duration = _int_option(options, "duration", 600, 60, 7200)
    seed = _int_option(options, "seed", 42, 0, 2_147_483_647)
    sensor_range = _float_option(options, "sensor_range", 120.0, 20.0, 500.0)
    emergency_depart = _float_option(
        options,
        "emergency_depart",
        min(180.0, max(20.0, duration * 0.35)),
        0.0,
        max(0.0, duration - 1.0),
    )
    gui = _bool_option(options, "gui", False)
    baseline = _bool_option(options, "baseline", False)
    websocket_port = find_free_port(
        _int_option(options, "websocket_port", 8765, 1024, 65535)
    )
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    results_dir = WEB_RESULTS_DIR / f"run_{timestamp}_seed_{seed}"

    command = [
        str(_python_executable()),
        "-u",
        str(PROJECT_ROOT / "experiments" / "run_demo.py"),
        "--duration",
        str(duration),
        "--seed",
        str(seed),
        "--emergency-depart",
        f"{emergency_depart:.1f}",
        "--sensor-range",
        f"{sensor_range:.1f}",
        "--websocket-port",
        str(websocket_port),
        "--results-dir",
        str(results_dir),
        "--no-dashboard",
    ]
    if not gui:
        command.append("--headless")
    if not baseline:
        command.append("--no-baseline")
    return command, results_dir


HTML_PAGE = r"""<!doctype html>
<html lang="uk">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>FlowMind Dashboard</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f7f4ef;
      --panel: #ffffff;
      --ink: #1c1f23;
      --muted: #626b76;
      --line: #d9d3c8;
      --green: #14866d;
      --green-soft: #e1f4ee;
      --amber: #d8901f;
      --red: #c94b4b;
      --blue: #2f6f9f;
      --shadow: 0 8px 28px rgba(28, 31, 35, 0.08);
    }

    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--ink);
      font: 14px/1.45 Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }

    .page {
      width: min(1440px, 100%);
      margin: 0 auto;
      padding: 20px;
    }

    .topbar {
      display: grid;
      grid-template-columns: minmax(240px, 1fr) auto;
      gap: 16px;
      align-items: start;
      margin-bottom: 16px;
    }

    h1 {
      margin: 0 0 4px;
      font-size: 28px;
      line-height: 1.1;
      letter-spacing: 0;
    }

    .subtitle {
      color: var(--muted);
      margin: 0;
      max-width: 760px;
    }

    .status-line {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      justify-content: flex-end;
    }

    .nav {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 10px;
    }

    .nav a {
      color: var(--ink);
      text-decoration: none;
      border: 1px solid var(--line);
      background: #fffdf9;
      padding: 7px 10px;
      border-radius: 6px;
      font-weight: 750;
    }

    .tag {
      border: 1px solid var(--line);
      background: #fffaf2;
      padding: 6px 9px;
      border-radius: 6px;
      color: var(--muted);
      white-space: nowrap;
    }

    .tag strong { color: var(--ink); font-weight: 700; }
    .tag.good { background: var(--green-soft); border-color: #afd9cc; }
    .tag.warn { background: #fff0cf; border-color: #e7c674; }
    .tag.bad { background: #fae3e0; border-color: #e5aaa3; }

    .controls {
      display: grid;
      grid-template-columns: repeat(5, minmax(120px, 1fr)) auto;
      gap: 10px;
      align-items: end;
      padding: 14px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      margin-bottom: 16px;
    }

    label {
      display: grid;
      gap: 5px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 650;
    }

    input {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 9px 10px;
      background: #fffdf9;
      color: var(--ink);
      font: inherit;
      min-height: 38px;
    }

    .check {
      display: flex;
      gap: 8px;
      align-items: center;
      min-height: 38px;
      padding: 0 2px;
    }

    .check input { width: 18px; min-height: 18px; }

    .buttons {
      display: flex;
      gap: 8px;
      justify-content: flex-end;
      align-items: center;
    }

    button {
      border: 1px solid transparent;
      border-radius: 6px;
      padding: 9px 12px;
      min-height: 38px;
      font: inherit;
      font-weight: 750;
      cursor: pointer;
      color: #fff;
      background: var(--green);
    }

    button.secondary {
      color: var(--ink);
      border-color: var(--line);
      background: #fffdf9;
    }

    button.danger { background: var(--red); }
    button:disabled { opacity: .55; cursor: wait; }

    .grid {
      display: grid;
      grid-template-columns: minmax(0, 1.35fr) minmax(340px, .65fr);
      gap: 16px;
      align-items: start;
    }

    .section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      overflow: hidden;
      margin-bottom: 16px;
    }

    .section-header {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
      background: #fffaf2;
    }

    .section-header h2 {
      margin: 0;
      font-size: 15px;
      letter-spacing: 0;
    }

    .section-body { padding: 14px; }

    .cards {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
    }

    .metric {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 11px;
      min-height: 86px;
      background: #fffdf9;
      display: grid;
      align-content: space-between;
      gap: 6px;
    }

    .metric .label {
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
    }

    .metric .value {
      font-size: 23px;
      font-weight: 850;
      line-height: 1.1;
      overflow-wrap: anywhere;
    }

    .metric .note {
      color: var(--muted);
      font-size: 12px;
      min-height: 17px;
    }

    canvas {
      display: block;
      width: 100%;
      height: 260px;
      background: #fffdf9;
      border: 1px solid var(--line);
      border-radius: 8px;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }

    th, td {
      padding: 9px 8px;
      border-bottom: 1px solid #ece6dc;
      text-align: left;
      vertical-align: middle;
    }

    th {
      color: var(--muted);
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0;
    }

    .mono {
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
      overflow-wrap: anywhere;
    }

    .bar {
      height: 8px;
      background: #efe6d9;
      border-radius: 4px;
      overflow: hidden;
      min-width: 70px;
    }

    .bar > span {
      display: block;
      height: 100%;
      width: 0;
      background: var(--green);
    }

    .bar.red > span { background: var(--red); }
    .bar.amber > span { background: var(--amber); }

    .list {
      display: grid;
      gap: 9px;
      max-height: 520px;
      overflow: auto;
    }

    .event {
      border: 1px solid var(--line);
      border-left: 4px solid var(--blue);
      border-radius: 8px;
      padding: 9px 10px;
      background: #fffdf9;
    }

    .event.success { border-left-color: var(--green); }
    .event.warning { border-left-color: var(--amber); }
    .event.error { border-left-color: var(--red); }

    .event-title {
      font-weight: 800;
      margin-bottom: 2px;
    }

    .event-detail {
      color: var(--muted);
      font-size: 12px;
    }

    .log {
      height: 250px;
      overflow: auto;
      white-space: pre-wrap;
      background: #1f2525;
      color: #e8f3ed;
      border-radius: 8px;
      padding: 12px;
      font: 12px/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }

    .empty {
      color: var(--muted);
      padding: 18px;
      border: 1px dashed var(--line);
      border-radius: 8px;
      background: #fffdf9;
    }

    @media (max-width: 1100px) {
      .controls { grid-template-columns: repeat(3, minmax(120px, 1fr)); }
      .buttons { justify-content: flex-start; }
      .grid { grid-template-columns: 1fr; }
      .cards { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .status-line { justify-content: flex-start; }
      .topbar { grid-template-columns: 1fr; }
    }

    @media (max-width: 680px) {
      .page { padding: 12px; }
      h1 { font-size: 23px; }
      .controls { grid-template-columns: 1fr; }
      .buttons { flex-wrap: wrap; }
      button { flex: 1 1 130px; }
      .cards { grid-template-columns: 1fr; }
      th:nth-child(1), td:nth-child(1) { max-width: 150px; }
    }
  </style>
</head>
<body>
  <main class="page">
    <header class="topbar">
      <div>
        <h1>FlowMind Dashboard</h1>
        <p class="subtitle">Live-панель симуляції з камерною моделлю датчиків біля контрольованих перехресть.</p>
        <nav class="nav" aria-label="Dashboard navigation">
          <a href="/">Live</a>
          <a href="/archive">Архів</a>
          <a href="/averages">Середні</a>
        </nav>
      </div>
      <div class="status-line">
        <span class="tag" id="sourceTag">джерело: <strong>немає</strong></span>
        <span class="tag" id="simTag">симуляція: <strong>waiting</strong></span>
        <span class="tag" id="processTag">процес: <strong>idle</strong></span>
      </div>
    </header>

    <section class="controls" aria-label="FlowMind controls">
      <label>Тривалість, с
        <input id="duration" type="number" min="60" max="7200" value="600">
      </label>
      <label>Seed
        <input id="seed" type="number" min="0" value="42">
      </label>
      <label>Радіус датчиків, м
        <input id="sensorRange" type="number" min="20" max="500" value="120">
      </label>
      <label>Старт швидкої, с
        <input id="emergencyDepart" type="number" min="0" value="180">
      </label>
      <label>Baseline
        <span class="check"><input id="baseline" type="checkbox"> static fixed + FlowMind</span>
      </label>
      <div class="buttons">
        <button id="startBtn">Запустити</button>
        <button id="stopBtn" class="danger">Зупинити</button>
        <button id="refreshBtn" class="secondary">Оновити</button>
      </div>
    </section>

    <section class="section">
      <div class="section-header">
        <h2>Поточні метрики</h2>
        <span class="tag" id="modeTag">режим: <strong>немає</strong></span>
      </div>
      <div class="section-body">
        <div class="cards" id="cards"></div>
      </div>
    </section>

    <div class="grid">
      <div>
        <section class="section">
          <div class="section-header">
            <h2>Динаміка потоку</h2>
            <span class="tag" id="sensorTag">датчики: <strong>немає</strong></span>
          </div>
          <div class="section-body">
            <canvas id="historyChart" width="1200" height="420"></canvas>
          </div>
        </section>

        <section class="section">
          <div class="section-header">
            <h2>Перехрестя</h2>
            <span class="tag" id="intersectionTag">0 активних</span>
          </div>
          <div class="section-body" id="intersections"></div>
        </section>
      </div>

      <aside>
        <section class="section">
          <div class="section-header">
            <h2>Система</h2>
            <span class="tag" id="queueTag">ML: <strong>немає</strong></span>
          </div>
          <div class="section-body">
            <div class="cards" id="systemCards"></div>
          </div>
        </section>

        <section class="section">
          <div class="section-header">
            <h2>Рішення контролера</h2>
            <span class="tag" id="decisionTag">0 подій</span>
          </div>
          <div class="section-body">
            <div class="list" id="decisionLog"></div>
          </div>
        </section>

        <section class="section">
          <div class="section-header">
            <h2>Процес</h2>
            <span class="tag" id="pidTag">PID: немає</span>
          </div>
          <div class="section-body">
            <div class="log" id="logs">waiting...</div>
          </div>
        </section>
      </aside>
    </div>
  </main>

  <script>
    const $ = (id) => document.getElementById(id);
    const pollMs = 1000;
    let busy = false;
    let archiveResults = [];
    let currentPayload = null;
    let selectedView = "overview";
    let selectedHistoryIndex = null;
    let selectedHistoryTime = null;
    let userSelectedTime = false;

    const scenarioPresets = {
      balanced: { duration: 600, seed: 42, sensorRange: 120, emergencyDepart: 180, baseline: false },
      rush: { duration: 900, seed: 20260707, sensorRange: 140, emergencyDepart: 260, baseline: true },
      emergency: { duration: 600, seed: 202607071, sensorRange: 120, emergencyDepart: 180, baseline: true },
      short: { duration: 120, seed: 43, sensorRange: 120, emergencyDepart: 40, baseline: false },
    };
    let archiveResults = [];
    let currentPayload = null;
    let selectedView = "full";

    const scenarioPresets = {
      balanced: { duration: 600, seed: 42, sensorRange: 120, emergencyDepart: 180, baseline: false },
      rush: { duration: 900, seed: 20260707, sensorRange: 140, emergencyDepart: 260, baseline: true },
      emergency: { duration: 600, seed: 202607071, sensorRange: 120, emergencyDepart: 180, baseline: true },
      short: { duration: 120, seed: 43, sensorRange: 120, emergencyDepart: 40, baseline: false },
    };

    function asNumber(value) {
      const number = Number(value);
      return Number.isFinite(number) ? number : null;
    }

    function fmt(value, suffix = "", digits = 1) {
      const number = asNumber(value);
      if (number === null) return "немає";
      const rounded = Math.abs(number - Math.round(number)) < 0.001
        ? String(Math.round(number))
        : number.toFixed(digits);
      return `${rounded}${suffix}`;
    }

    function shortText(value, max = 42) {
      const text = String(value ?? "немає");
      return text.length > max ? `${text.slice(0, max - 1)}...` : text;
    }

    function setTag(id, label, value, kind = "") {
      const node = $(id);
      node.className = `tag ${kind}`.trim();
      node.innerHTML = `${label}: <strong></strong>`;
      node.querySelector("strong").textContent = value ?? "немає";
    }

    async function api(path, options = {}) {
      const response = await fetch(path, options);
      if (!response.ok) {
        const text = await response.text();
        throw new Error(text || response.statusText);
      }
      return await response.json();
    }

    function applyScenarioPreset(name) {
      const preset = scenarioPresets[name] || scenarioPresets.balanced;
      $("duration").value = preset.duration;
      $("seed").value = preset.seed;
      $("sensorRange").value = preset.sensorRange;
      $("emergencyDepart").value = preset.emergencyDepart;
      $("baseline").checked = preset.baseline;
    }

    function applyViewMode(view) {
      selectedView = view || "full";
      document.querySelectorAll(".view-mode button").forEach((button) => {
        button.classList.toggle("active", button.dataset.view === selectedView);
      });
      document.querySelectorAll("[data-panel]").forEach((panel) => {
        const visible = panel.dataset.panel.split(/\s+/).includes(selectedView);
        panel.hidden = !visible;
      });
      if (currentPayload) {
        drawHistory(currentPayload.metric_history || []);
      }
    }

    function resetControls() {
      $("scenarioPicker").value = "balanced";
      applyScenarioPreset("balanced");
      $("resultSource").value = "live";
      $("archiveSelect").disabled = true;
      applyViewMode("full");
    }

    async function loadArchiveOptions() {
      try {
        const payload = await api("/api/archive");
        archiveResults = payload.results || [];
        const select = $("archiveSelect");
        select.innerHTML = archiveResults.length
          ? archiveResults.map((result) => {
              const summary = result.summary || {};
              const label = `${result.path || result.id} · ${summary.mode || result.mode || "unknown"} · wait ${fmt(summary.average_waiting_time, " с")}`;
              return `<option value="${result.id}">${label}</option>`;
            }).join("")
          : `<option value="">немає архіву</option>`;
      } catch (error) {
        $("archiveSelect").innerHTML = `<option value="">archive error</option>`;
      }
    }

    async function selectedPayload() {
      if ($("resultSource").value === "archive") {
        const id = $("archiveSelect").value;
        if (id) {
          const payload = await api(`/api/archive/${id}`);
          payload.process = { status: "archive", running: false, logs: [] };
          const system = payload.system && typeof payload.system === "object" ? payload.system : {};
          const simulation = system.simulation && typeof system.simulation === "object" ? system.simulation : {};
          payload.system = {
            ...system,
            simulation: { ...simulation, status: "archive" },
            metrics: system.metrics || { sensor_range_meters: payload.summary?.sensor_range_meters },
          };
          if (payload.zone_simulation && typeof payload.zone_simulation === "object") {
            payload.zone_simulation = {
              ...payload.zone_simulation,
              status: "archive",
              active: false,
            };
          }
          return payload;
        }
      }
      return await api("/api/status");
    }

    function metricCard(label, value, note = "") {
      return `<article class="metric">
        <div class="label"></div>
        <div class="value"></div>
        <div class="note"></div>
      </article>`;
    }

    function renderCards(container, cards) {
      container.innerHTML = cards.map(() => metricCard()).join("");
      [...container.children].forEach((node, index) => {
        const card = cards[index];
        node.querySelector(".label").textContent = card.label;
        node.querySelector(".value").textContent = card.value;
        node.querySelector(".note").textContent = card.note || "";
      });
    }

    function renderMetrics(payload) {
      const latest = payload.latest_sample || {};
      const summary = payload.summary || {};
      const gridlockRisk = latest.gridlock_risk ?? summary.gridlock_risk;
      const cards = [
        {
          label: "Активні авто",
          value: fmt(latest.active_vehicles ?? summary.peak_active_vehicles),
          note: `пропуск: ${fmt(latest.throughput ?? summary.throughput, " авто")}`
        },
        {
          label: "Черга",
          value: fmt(latest.queue_length ?? summary.average_queue_length, " авто"),
          note: `макс: ${fmt(latest.max_queue_length ?? summary.max_queue_length, " авто")}`
        },
        {
          label: "Очікування",
          value: fmt(latest.waiting_time ?? summary.average_waiting_time, " с"),
          note: "середнє по поточному зрізу"
        },
        {
          label: "Швидкість",
          value: fmt(latest.mean_speed, " м/с"),
          note: `ризик затору: ${fmt(gridlockRisk == null ? null : gridlockRisk * 100, "%")}`
        },
        {
          label: "Прибуло",
          value: fmt(latest.arrived ?? summary.throughput, " авто"),
          note: `виїхало: ${fmt(latest.departed ?? summary.departed_vehicles, " авто")}`
        },
        {
          label: "Зупинки",
          value: fmt(latest.stops_count ?? summary.stops_count),
          note: "сума за симуляцію"
        },
        {
          label: "Час симуляції",
          value: fmt(payload.simulated_time ?? summary.simulated_duration, " с"),
          note: `режим: ${payload.mode || "немає"}`
        },
        {
          label: "Швидка",
          value: fmt(summary.emergency_eta, " с"),
          note: summary.emergency_arrival_time ? "маршрут завершено" : "ETA або немає даних"
        },
      ];
      renderCards($("cards"), cards);
    }

    function renderSystem(payload) {
      const system = payload.system || {};
      const summary = payload.summary || {};
      const simulation = system.simulation || {};
      const controller = system.controller || {};
      const forecast = system.queue_forecast || {};
      const corridor = system.corridor || {};
      const metrics = system.metrics || {};
      const process = payload.process || {};
      renderCards($("systemCards"), [
        {
          label: "Контролер",
          value: controller.status || "немає",
          note: `${fmt(controller.decisions)} рішень`
        },
        {
          label: "Прогноз",
          value: forecast.status || "немає",
          note: `${fmt(forecast.predictions)} прогнозів, ${fmt(forecast.failures)} помилок`
        },
        {
          label: "Коридор",
          value: corridor.corridor_state || "немає",
          note: shortText(corridor.corridor_active_tls, 36)
        },
        {
          label: "Процес",
          value: process.status || "idle",
          note: process.results_dir || "немає активної директорії"
        },
      ]);
      setTag("queueTag", "ML", forecast.status || "немає", forecast.status === "active" ? "good" : "");
      setTag("sensorTag", "датчики", `${metrics.sensor_range_meters || "?"} м, ${metrics.coverage || "coverage"}`);
      setTag("simTag", "симуляція", simulation.status || "waiting", simulation.status === "running" ? "good" : "");
      setTag("processTag", "процес", process.status || "idle", process.running ? "good" : "");
      $("pidTag").textContent = `PID: ${process.pid || "немає"}`;
    }

    function renderIntersections(rows) {
      $("intersectionTag").textContent = `${rows.length} активних`;
      if (!rows.length) {
        $("intersections").innerHTML = `<div class="empty">Дані по перехрестях ще не надійшли.</div>`;
        return;
      }
      const maxQueue = Math.max(1, ...rows.map(row => asNumber(row.incoming_queue) || 0));
      const table = document.createElement("table");
      table.innerHTML = `<thead><tr>
        <th>Перехрестя</th><th>Сигнал</th><th>Фаза</th><th>Авто</th><th>Черга</th><th>Виїзд</th>
      </tr></thead><tbody></tbody>`;
      const body = table.querySelector("tbody");
      [...rows]
        .sort((a, b) => (asNumber(b.incoming_queue) || 0) - (asNumber(a.incoming_queue) || 0))
        .forEach((row) => {
          const queue = asNumber(row.incoming_queue) || 0;
          const occupancy = asNumber(row.outgoing_occupancy) || 0;
          const tr = document.createElement("tr");
          tr.innerHTML = `<td class="mono"></td>
            <td></td>
            <td></td>
            <td></td>
            <td><div class="bar"><span></span></div></td>
            <td></td>`;
          tr.children[0].textContent = shortText(row.tls_id, 46);
          tr.children[1].textContent = row.signal || "немає";
          tr.children[2].textContent = `${row.phase ?? "?"} / ${fmt(row.phase_elapsed, " с")}`;
          tr.children[3].textContent = fmt(row.incoming_vehicles, " авто");
          const bar = tr.querySelector(".bar");
          bar.className = `bar ${queue >= 8 ? "red" : queue >= 4 ? "amber" : ""}`;
          bar.querySelector("span").style.width = `${Math.min(100, (queue / maxQueue) * 100)}%`;
          tr.children[5].textContent = fmt(occupancy * 100, "%");
          body.appendChild(tr);
        });
      $("intersections").replaceChildren(table);
    }

    function renderDecisions(rows) {
      $("decisionTag").textContent = `${rows.length} подій`;
      if (!rows.length) {
        $("decisionLog").innerHTML = `<div class="empty">Рішення контролера ще не записані.</div>`;
        return;
      }
      $("decisionLog").innerHTML = "";
      rows.slice(-24).reverse().forEach((row) => {
        const item = document.createElement("article");
        item.className = `event ${row.level || ""}`.trim();
        const title = document.createElement("div");
        title.className = "event-title";
        title.textContent = `${fmt(row.time, " с", 0)} - ${row.title || row.category || "подія"}`;
        const detail = document.createElement("div");
        detail.className = "event-detail";
        detail.textContent = row.detail || shortText(row.tls_id, 80);
        item.append(title, detail);
        $("decisionLog").appendChild(item);
      });
    }

    function drawHistory(history) {
      const canvas = $("historyChart");
      const ctx = canvas.getContext("2d");
      const dpr = window.devicePixelRatio || 1;
      const rect = canvas.getBoundingClientRect();
      canvas.width = Math.max(640, Math.floor(rect.width * dpr));
      canvas.height = Math.max(260, Math.floor(rect.height * dpr));
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      const width = rect.width;
      const height = rect.height;
      ctx.clearRect(0, 0, width, height);
      ctx.fillStyle = "#fffdf9";
      ctx.fillRect(0, 0, width, height);

      if (!history.length) {
        ctx.fillStyle = "#626b76";
        ctx.font = "14px system-ui";
        ctx.fillText("Немає історії метрик", 20, 34);
        return;
      }

      const pad = { left: 46, right: 18, top: 18, bottom: 30 };
      const plotW = width - pad.left - pad.right;
      const plotH = height - pad.top - pad.bottom;
      const series = [
        { key: "queue_length", color: "#c94b4b", label: "черга" },
        { key: "waiting_time", color: "#d8901f", label: "очікування" },
        { key: "active_vehicles", color: "#14866d", label: "активні авто" },
      ];
      const values = history.flatMap(row => series.map(s => asNumber(row[s.key]) || 0));
      const maxY = Math.max(1, ...values);
      const times = history.map(row => asNumber(row.time) || 0);
      const minT = Math.min(...times);
      const maxT = Math.max(...times);
      const spanT = Math.max(1, maxT - minT);

      ctx.strokeStyle = "#e8dfd1";
      ctx.lineWidth = 1;
      for (let i = 0; i <= 4; i += 1) {
        const y = pad.top + plotH * (i / 4);
        ctx.beginPath();
        ctx.moveTo(pad.left, y);
        ctx.lineTo(width - pad.right, y);
        ctx.stroke();
      }

      series.forEach((serie) => {
        ctx.strokeStyle = serie.color;
        ctx.lineWidth = 2;
        ctx.beginPath();
        history.forEach((row, index) => {
          const x = pad.left + (((asNumber(row.time) || 0) - minT) / spanT) * plotW;
          const y = pad.top + plotH - ((asNumber(row[serie.key]) || 0) / maxY) * plotH;
          if (index === 0) ctx.moveTo(x, y);
          else ctx.lineTo(x, y);
        });
        ctx.stroke();
      });

      ctx.fillStyle = "#626b76";
      ctx.font = "12px system-ui";
      ctx.fillText(`${fmt(maxY, "", 0)}`, 8, pad.top + 5);
      ctx.fillText(`${fmt(minT, " с", 0)}`, pad.left, height - 8);
      ctx.fillText(`${fmt(maxT, " с", 0)}`, width - pad.right - 54, height - 8);

      let legendX = pad.left;
      series.forEach((serie) => {
        ctx.fillStyle = serie.color;
        ctx.fillRect(legendX, 14, 18, 3);
        ctx.fillStyle = "#1c1f23";
        ctx.fillText(serie.label, legendX + 24, 18);
        legendX += 118;
      });
    }

    function renderProcess(process) {
      $("startBtn").disabled = !!process.running || busy;
      $("stopBtn").disabled = !process.running || busy;
      const logs = process.logs || [];
      $("logs").textContent = logs.length ? logs.join("\n") : "waiting...";
      const logBox = $("logs");
      logBox.scrollTop = logBox.scrollHeight;
    }

    async function refresh() {
      try {
        const payload = await api("/api/status");
        const process = payload.process || {};
        const source = payload._meta?.source || "немає";
        const sourceKind = payload.available ? "good" : "warn";
        setTag("sourceTag", "джерело", shortText(source, 48), sourceKind);
        setTag("modeTag", "режим", payload.mode || payload.system?.simulation?.mode || "немає");
        renderMetrics(payload);
        renderSystem(payload);
        renderIntersections(payload.intersections || []);
        renderDecisions(payload.decision_log || []);
        drawHistory(payload.metric_history || []);
        renderProcess(process);
      } catch (error) {
        setTag("processTag", "процес", "api error", "bad");
        $("logs").textContent = String(error);
      }
    }

    async function startDemo() {
      $("resultSource").value = "live";
      $("archiveSelect").disabled = true;
      busy = true;
      renderProcess({ running: true, logs: ["starting..."] });
      try {
        await api("/api/start-demo", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            duration: $("duration").value,
            seed: $("seed").value,
            sensor_range: $("sensorRange").value,
            emergency_depart: $("emergencyDepart").value,
            baseline: $("baseline").checked,
            gui: false
          })
        });
      } finally {
        busy = false;
        await refresh();
      }
    }

    async function stopDemo() {
      busy = true;
      try {
        await api("/api/stop", { method: "POST" });
      } finally {
        busy = false;
        await refresh();
      }
    }

    $("startBtn").addEventListener("click", startDemo);
    $("stopBtn").addEventListener("click", stopDemo);
    $("refreshBtn").addEventListener("click", refresh);
    $("resetBtn").addEventListener("click", () => {
      resetControls();
      refresh();
    });
    $("scenarioPicker").addEventListener("change", (event) => {
      applyScenarioPreset(event.target.value);
    });
    $("resultSource").addEventListener("change", () => {
      const archiveMode = $("resultSource").value === "archive";
      $("archiveSelect").disabled = !archiveMode;
      selectedHistoryIndex = null;
      userSelectedTime = false;
      refresh();
    });
    $("archiveSelect").addEventListener("change", () => {
      selectedHistoryIndex = null;
      userSelectedTime = false;
      refresh();
    });
    $("timeSlider").addEventListener("input", (event) => {
      selectedHistoryIndex = Number(event.target.value);
      userSelectedTime = true;
      if (currentPayload) {
        renderComparison(currentPayload);
        drawHistory(currentPayload.metric_history || []);
      }
    });
    document.querySelectorAll(".view-mode button").forEach((button) => {
      button.addEventListener("click", () => applyViewMode(button.dataset.view));
    });
    window.addEventListener("resize", () => refresh());
    resetControls();
    loadArchiveOptions().then(refresh);
    setInterval(refresh, pollMs);
  </script>
</body>
</html>
"""


DESIGN_PAGE = r"""<!doctype html>
<html lang="uk">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>FlowMind Dashboard</title>
  <link rel="stylesheet" href="/assets/zone-simulation.css">
  <style>
    :root {
      color-scheme: dark;
      --bg: #0d111b;
      --sidebar: #1a202c;
      --panel: #1d2330;
      --panel-2: #222938;
      --panel-3: #151a25;
      --ink: #f7f8fb;
      --muted: #9ca6b7;
      --line: #31394a;
      --cyan: #4ddfd4;
      --green: #62d48b;
      --amber: #f2bf5e;
      --red: #ff6f61;
      --shadow: 0 10px 28px rgba(0, 0, 0, .18);
    }

    * { box-sizing: border-box; }

    body {
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--ink);
      font: 14px/1.45 Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }

    .app {
      display: grid;
      grid-template-columns: 224px minmax(0, 1fr);
      min-height: 100vh;
    }

    .sidebar {
      position: sticky;
      top: 0;
      height: 100vh;
      overflow: auto;
      padding: 16px 14px;
      background: #171d28;
      border-right: 1px solid #2b3343;
    }

    .side-label {
      margin: 0 0 8px;
      color: var(--ink);
      font-size: 13px;
      font-weight: 720;
    }

    .side-group {
      margin-bottom: 16px;
      padding-bottom: 14px;
      border-bottom: 1px solid #2b3343;
    }

    label {
      display: grid;
      gap: 7px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 720;
      margin-bottom: 10px;
    }

    input, select {
      width: 100%;
      min-height: 38px;
      border: 1px solid #343d50;
      border-radius: 7px;
      padding: 9px 10px;
      color: var(--ink);
      background: #171d28;
      font: inherit;
      outline: none;
    }

    input:focus, select:focus {
      border-color: var(--cyan);
      box-shadow: 0 0 0 2px rgba(77, 223, 212, .16);
    }

    .check {
      display: flex;
      align-items: center;
      gap: 9px;
      color: var(--ink);
      min-height: 34px;
    }

    .check input {
      width: 18px;
      min-height: 18px;
      accent-color: var(--cyan);
    }

    .view-mode {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 6px;
    }

    .view-mode button {
      display: grid;
      place-items: center;
      height: 30px;
      border: 1px solid #343d50;
      border-radius: 7px;
      color: var(--muted);
      background: #171d28;
      font-weight: 760;
      min-height: 30px;
      padding: 0;
    }

    .view-mode button.active {
      color: var(--ink);
      border-color: #4a5368;
      background: #2b3345;
    }

    .legend {
      display: grid;
      gap: 9px;
      color: var(--muted);
      font-size: 13px;
    }

    .legend span {
      display: flex;
      gap: 9px;
      align-items: center;
    }

    .swatch {
      width: 14px;
      height: 14px;
      border-radius: 4px;
      display: inline-block;
    }

    .content {
      min-width: 0;
      padding: 20px;
    }

    .topbar {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 16px;
      align-items: start;
      margin-bottom: 22px;
    }

    .brand {
      display: flex;
      align-items: center;
      gap: 12px;
    }

    .mark {
      width: 32px;
      height: 32px;
      border-radius: 10px;
      background:
        linear-gradient(90deg, transparent 43%, var(--cyan) 43% 57%, transparent 57%),
        linear-gradient(0deg, transparent 43%, var(--red) 43% 57%, transparent 57%),
        radial-gradient(circle at center, #202838 0 36%, transparent 37%);
      border: 1px solid #384155;
    }

    h1 {
      margin: 0;
      font-size: 28px;
      line-height: 1.08;
      letter-spacing: 0;
    }

    .subtitle {
      margin: 3px 0 0;
      color: var(--muted);
      font-size: 14px;
    }

    .nav {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 12px;
    }

    .nav a {
      color: var(--muted);
      text-decoration: none;
      border: 1px solid var(--line);
      border-radius: 7px;
      padding: 7px 10px;
      background: #171d28;
      font-weight: 760;
    }

    .nav a.active {
      color: var(--ink);
      border-color: rgba(77, 223, 212, .55);
      background: rgba(77, 223, 212, .12);
    }

    .status-line {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      justify-content: flex-end;
    }

    .tag {
      display: inline-flex;
      gap: 6px;
      align-items: center;
      border: 1px solid #344055;
      border-radius: 7px;
      padding: 6px 9px;
      color: var(--muted);
      background: #1b2330;
      white-space: nowrap;
      font-size: 12px;
      font-weight: 720;
    }

    .tag::before {
      content: "";
      width: 7px;
      height: 7px;
      border-radius: 999px;
      background: #687386;
    }

    .tag strong { color: var(--ink); font-weight: 760; }
    .tag.good { border-color: rgba(98, 212, 139, .35); background: rgba(98, 212, 139, .12); }
    .tag.good::before { background: var(--green); }
    .tag.warn { border-color: rgba(242, 191, 94, .4); background: rgba(242, 191, 94, .12); }
    .tag.warn::before { background: var(--amber); }
    .tag.bad { border-color: rgba(255, 111, 97, .42); background: rgba(255, 111, 97, .12); }
    .tag.bad::before { background: var(--red); }

    .actions {
      display: grid;
      grid-template-columns: 1fr;
      gap: 8px;
    }

    button {
      min-height: 40px;
      border: 1px solid transparent;
      border-radius: 8px;
      padding: 9px 12px;
      color: #071015;
      background: var(--cyan);
      font: inherit;
      font-weight: 760;
      cursor: pointer;
    }

    button.secondary {
      color: var(--ink);
      border-color: var(--line);
      background: #1b2330;
    }

    button.danger {
      color: #1b0b08;
      background: var(--red);
    }

    button:disabled { opacity: .52; cursor: wait; }

    .kpis {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 14px;
    }

    .metric, .panel {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      box-shadow: var(--shadow);
    }

    .metric {
      min-height: 92px;
      padding: 14px;
      display: grid;
      align-content: space-between;
      gap: 8px;
    }

    .metric .label {
      color: var(--ink);
      font-size: 13px;
      font-weight: 720;
    }

    .metric .value {
      color: #fff;
      font-size: 34px;
      line-height: 1;
      font-weight: 820;
      overflow-wrap: anywhere;
    }

    .metric .note {
      color: var(--muted);
      font-size: 12px;
    }

    .panel {
      overflow: hidden;
      margin-bottom: 14px;
    }

    .panel-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
    }

    .panel-header h2 {
      margin: 0;
      font-size: 16px;
      letter-spacing: 0;
    }

    .panel-body { padding: 14px; }

    .timeline-control {
      display: grid;
      grid-template-columns: 32px minmax(0, 1fr) 72px;
      gap: 14px;
      align-items: center;
      min-height: 42px;
      padding: 0 4px;
    }

    .play {
      width: 0;
      height: 0;
      margin-left: 8px;
      border-top: 8px solid transparent;
      border-bottom: 8px solid transparent;
      border-left: 12px solid var(--ink);
    }

    .time-slider {
      width: 100%;
      min-height: 22px;
      padding: 0;
      border: 0;
      border-radius: 0;
      background: transparent;
      accent-color: var(--cyan);
      cursor: pointer;
    }

    .time-slider:disabled {
      cursor: not-allowed;
      opacity: .45;
    }

    .comparison-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 14px;
    }

    .scenario-card {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      background: var(--panel-2);
    }

    .scenario-card.fixed { border-color: rgba(255, 111, 97, .8); }
    .scenario-card.flow { border-color: rgba(77, 223, 212, .75); }

    .scenario-card h3 {
      margin: 0 0 4px;
      font-size: 15px;
    }

    .scenario-card.fixed h3 { color: var(--red); }
    .scenario-card.flow h3 { color: var(--cyan); }

    .scenario-card p {
      margin: 0 0 10px;
      color: var(--muted);
      font-size: 13px;
    }

    .vehicle-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 12px;
    }

    .cars {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      min-height: 30px;
    }

    .car {
      width: 38px;
      height: 22px;
      display: inline-block;
      background-color: #3dbbaf;
      -webkit-mask: url("/assets/icon_car.png") center / contain no-repeat;
      mask: url("/assets/icon_car.png") center / contain no-repeat;
    }

    .fixed .car { background-color: #f45140; }
    .car.alt { background-color: #ffb631; }

    .bar {
      height: 8px;
      border-radius: 999px;
      background: #343b4c;
      overflow: hidden;
    }

    .bar span {
      display: block;
      height: 100%;
      width: 0;
      border-radius: inherit;
      background: linear-gradient(90deg, var(--cyan), rgba(77, 223, 212, .45));
    }

    .fixed .bar span {
      background: linear-gradient(90deg, var(--red), var(--amber));
    }

    .scenario-meta {
      display: grid;
      grid-template-columns: 1fr;
      gap: 6px;
      margin-top: 8px;
      color: var(--muted);
      font-size: 13px;
    }

    .scenario-meta span {
      display: flex;
      justify-content: space-between;
      gap: 10px;
    }

    .slice-note {
      margin: -4px 0 12px 46px;
      color: var(--muted);
      font-size: 12px;
    }

    .insight {
      margin-top: 14px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px 12px;
      background: #171d28;
      color: var(--ink);
    }

    .ai-report {
      display: grid;
      gap: 12px;
      color: var(--ink);
    }

    .ai-report-text {
      min-height: 96px;
      white-space: pre-wrap;
      color: #dfe7f5;
      border: 1px solid #30394d;
      border-radius: 8px;
      background: #101620;
      padding: 12px;
    }

    .ai-report-meta {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      color: var(--muted);
      font-size: 12px;
    }

    .ai-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }

    .layout {
      display: grid;
      grid-template-columns: minmax(0, 1.2fr) minmax(340px, .8fr);
      gap: 16px;
      align-items: start;
    }

    canvas {
      display: block;
      width: 100%;
      height: 260px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #151b26;
    }

    .decision-list {
      position: relative;
      display: grid;
      gap: 10px;
      max-height: 320px;
      overflow: auto;
      padding-left: 42px;
    }

    .decision-list::before {
      content: "";
      position: absolute;
      left: 20px;
      top: 8px;
      bottom: 8px;
      width: 1px;
      background: #384155;
    }

    .event {
      position: relative;
      border: 1px solid #3a4357;
      border-radius: 8px;
      padding: 10px 12px;
      background: #1b2230;
    }

    .event::before {
      content: "";
      position: absolute;
      left: -29px;
      top: 14px;
      width: 10px;
      height: 10px;
      border-radius: 999px;
      background: var(--cyan);
      box-shadow: 0 0 0 4px rgba(77, 223, 212, .12);
    }

    .event.warning::before { background: var(--amber); box-shadow: 0 0 0 4px rgba(242, 191, 94, .12); }
    .event.error::before { background: var(--red); box-shadow: 0 0 0 4px rgba(255, 111, 97, .12); }
    .event.success::before { background: var(--green); box-shadow: 0 0 0 4px rgba(98, 212, 139, .12); }

    .event-title { font-weight: 820; margin-bottom: 4px; }
    .event-detail { color: var(--muted); font-size: 13px; }

    .ambulance {
      display: grid;
      gap: 12px;
    }

    .ambulance-metrics {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
    }

    .mini-stat {
      min-height: 72px;
      border: 1px solid #3a4357;
      border-radius: 8px;
      padding: 10px;
      background: #171d28;
      display: grid;
      gap: 4px;
      align-content: center;
    }

    .mini-stat span {
      color: var(--muted);
      font-size: 12px;
      font-weight: 720;
    }

    .mini-stat strong {
      color: var(--ink);
      font-size: 19px;
      line-height: 1.1;
      overflow-wrap: anywhere;
    }

    .route-strip {
      position: relative;
      min-height: 118px;
      border: 1px solid #3a4357;
      border-radius: 8px;
      padding: 16px 12px;
      background:
        linear-gradient(90deg, transparent 0 11%, rgba(255,255,255,.08) 11% 12%, transparent 12% 100%),
        linear-gradient(180deg, #151b26, #171d28);
      overflow: hidden;
    }

    .route-line {
      position: absolute;
      left: 36px;
      right: 36px;
      top: 56px;
      height: 8px;
      border-radius: 999px;
      background: #384155;
    }

    .route-line span {
      display: block;
      width: 0;
      height: 100%;
      border-radius: inherit;
      background: linear-gradient(90deg, var(--green), var(--cyan));
      box-shadow: 0 0 14px rgba(77, 223, 212, .28);
    }

    .route-node {
      position: absolute;
      top: 47px;
      width: 24px;
      height: 24px;
      border-radius: 999px;
      background: #1d2330;
      border: 2px solid #586276;
    }

    .route-node.start { left: 24px; border-color: var(--green); }
    .route-node.mid { left: calc(50% - 12px); border-color: var(--cyan); }
    .route-node.end { right: 24px; border-color: var(--red); }

    .route-label {
      position: absolute;
      bottom: 14px;
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
    }

    .route-label.start { left: 16px; }
    .route-label.mid { left: 50%; transform: translateX(-50%); }
    .route-label.end { right: 16px; }

    .status-card {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }

    .log {
      height: 180px;
      overflow: auto;
      white-space: pre-wrap;
      color: #cbd5e4;
      background: #101620;
      border: 1px solid #30394d;
      border-radius: 8px;
      padding: 12px;
      font: 12px/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }

    .table-wrap { overflow: auto; max-height: 360px; }

    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }

    th, td {
      padding: 9px 8px;
      border-bottom: 1px solid #30394d;
      text-align: left;
      vertical-align: middle;
    }

    th {
      color: var(--muted);
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0;
      background: #1b2230;
      position: sticky;
      top: 0;
    }

    .mono {
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
      overflow-wrap: anywhere;
    }

    .empty {
      color: var(--muted);
      padding: 18px;
      border: 1px dashed #3a4357;
      border-radius: 8px;
      background: #171d28;
    }

    @media (max-width: 1180px) {
      .app { grid-template-columns: 1fr; }
      .sidebar {
        position: static;
        height: auto;
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 14px;
      }
      .side-group { margin: 0; }
      .layout { grid-template-columns: 1fr; }
      .kpis { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }

    @media (max-width: 760px) {
      .content { padding: 16px; }
      .sidebar { grid-template-columns: 1fr; padding: 14px; }
      .topbar { grid-template-columns: 1fr; }
      .status-line { justify-content: flex-start; }
      h1 { font-size: 26px; }
      .kpis, .comparison-grid, .status-card { grid-template-columns: 1fr; }
      .metric .value { font-size: 34px; }
    }
  </style>
</head>
<body>
  <div class="app">
    <aside class="sidebar">
      <section class="side-group">
        <p class="side-label">Result selection</p>
        <label>Джерело
          <select id="resultSource">
            <option value="live">Live симуляція</option>
            <option value="archive">Архівний результат</option>
          </select>
        </label>
        <label>Архів
          <select id="archiveSelect" disabled>
            <option value="">завантаження...</option>
          </select>
        </label>
        <label>Scenario picker
          <select id="scenarioPicker">
            <option value="balanced">FlowMind balanced</option>
            <option value="rush">High flow / rush hour</option>
            <option value="emergency">Emergency corridor</option>
            <option value="short">Quick smoke test</option>
          </select>
        </label>
      </section>

      <section class="side-group">
        <p class="side-label">View mode</p>
        <div class="view-mode" aria-label="View mode">
          <button type="button" data-view="overview" class="active" title="Огляд">≡</button>
          <button type="button" data-view="compare" title="Порівняння">▥</button>
          <button type="button" data-view="ops" title="Операційний режим">▦</button>
          <button type="button" data-view="full" title="Усе">▣</button>
        </div>
      </section>

      <section class="side-group">
        <p class="side-label">Simulation setup</p>
        <label>Тривалість, с
          <input id="duration" type="number" min="60" max="7200" value="600">
        </label>
        <label>Seed
          <input id="seed" type="number" min="0" value="42">
        </label>
        <label>Радіус датчиків, м
          <input id="sensorRange" type="number" min="20" max="500" value="120">
        </label>
        <label>Старт швидкої, с
          <input id="emergencyDepart" type="number" min="0" value="180">
        </label>
        <label>Baseline
          <span class="check"><input id="baseline" type="checkbox"> static fixed + FlowMind</span>
        </label>
        <div class="actions">
          <button id="startBtn" title="Запустити симуляцію">▶ Запустити</button>
          <button id="stopBtn" class="danger" title="Зупинити активну симуляцію">■ Зупинити</button>
          <button id="refreshBtn" class="secondary" title="Оновити дані">↻ Оновити</button>
          <button id="resetBtn" class="secondary" title="Повернути базові параметри">Скинути</button>
        </div>
      </section>

      <section class="side-group">
        <p class="side-label">Connection status</p>
        <span class="tag" id="sourceTag">джерело: <strong>немає</strong></span>
      </section>

      <section class="side-group">
        <p class="side-label">Legend</p>
        <div class="legend">
          <span><i class="swatch" style="background: var(--red)"></i> Static Fixed</span>
          <span><i class="swatch" style="background: var(--amber)"></i> Local Adaptive</span>
          <span><i class="swatch" style="background: var(--cyan)"></i> FlowMind</span>
        </div>
      </section>
    </aside>

    <main class="content">
      <header class="topbar">
        <div>
          <div class="brand">
            <div class="mark" aria-hidden="true"></div>
            <div>
              <h1>FlowMind Dashboard</h1>
              <p class="subtitle">Зональне керування світлофорами та екстреним маршрутом</p>
            </div>
          </div>
          <nav class="nav" aria-label="Dashboard navigation">
            <a href="/" class="active">Live</a>
            <a href="/archive">Архів</a>
            <a href="/averages">Середні</a>
          </nav>
        </div>
        <div class="status-line">
          <span class="tag good" id="simTag">симуляція: <strong>waiting</strong></span>
          <span class="tag good" id="modeTag">режим: <strong>немає</strong></span>
          <span class="tag" id="processTag">процес: <strong>idle</strong></span>
        </div>
      </header>

      <section class="kpis" id="cards" data-panel="overview ops full"></section>

      <section class="panel zone-simulation" id="zoneSimulation" data-panel="overview compare ops full" aria-labelledby="zoneSimulationTitle">
        <div class="panel-header zone-sim-header">
          <div class="zone-sim-heading">
            <span class="zone-sim-eyebrow">SUMO LIVE · INTERSECTION MONITOR</span>
            <h2 id="zoneSimulationTitle">Симуляція зони перехресть</h2>
          </div>
          <div class="zone-sim-header-actions">
            <span class="tag warn" id="zoneSimulationSource">очікує SUMO snapshot</span>
          </div>
        </div>
        <div class="zone-sim-body">
          <div class="zone-sim-intro">
            <p class="zone-sim-intro-copy">
              Кожне контрольоване SUMO-перехрестя показане окремо зі своїми машинами, чергою, зайнятістю виходу та сигналом. Натисніть на потрібний вузол, щоб відстежувати його live. Mock-режим вимкнено.
            </p>
            <span class="zone-sim-readonly-mode" id="zoneSimulationMode">Режим: очікує дані</span>
          </div>

          <div class="zone-sim-layout">
            <div class="zone-sim-map-wrap">
              <div class="zone-sim-map" id="zoneSimulationMap" aria-live="polite" aria-label="Live-монітор окремих SUMO-перехресть"></div>
              <div class="zone-sim-flow-status" aria-live="polite">
                <div class="zone-sim-direction-card">
                  <span class="zone-sim-direction-icon" id="zoneSimulationDirectionIcon" aria-hidden="true">⌁</span>
                  <div>
                    <span>Вибране перехрестя SUMO</span>
                    <strong id="zoneSimulationDirection">—</strong>
                  </div>
                </div>
                <p class="zone-sim-message" id="zoneSimulationMessage">Очікуємо активну SUMO-симуляцію та live snapshot.</p>
              </div>
            </div>

            <aside class="zone-sim-sidebar" aria-label="Стан зони">
              <div class="zone-sim-stat-grid">
                <div class="zone-sim-stat">
                  <span class="zone-sim-stat-label">Вузли зони</span>
                  <strong id="zoneSimulationPressure">—</strong>
                </div>
                <div class="zone-sim-stat zone-sim-stat--free">
                  <span class="zone-sim-stat-label">Авто у кадрі</span>
                  <strong id="zoneSimulationFreeSpace">—</strong>
                </div>
                <div class="zone-sim-stat zone-sim-stat--queue">
                  <span class="zone-sim-stat-label">Сумарна черга</span>
                  <strong id="zoneSimulationQueue">—</strong>
                </div>
              </div>
              <p class="zone-sim-phases-heading">Активні світлофори SUMO</p>
              <div class="zone-sim-phases" id="zoneSimulationPhases"></div>
              <div class="zone-sim-legend" aria-label="Легенда світлофорів">
                <span><i class="green"></i> Зелений</span>
                <span><i class="yellow"></i> Жовтий</span>
                <span><i class="red"></i> Червоний</span>
              </div>
              <div class="zone-sim-legend zone-sim-load-legend" aria-label="Легенда завантаження автомобілів">
                <span><i class="car-free"></i> Авто: &lt;5</span>
                <span><i class="car-busy"></i> Авто: 5–9</span>
                <span><i class="car-critical"></i> Авто: ≥10</span>
              </div>
            </aside>
          </div>
        </div>
        <div class="zone-sim-footer">
          <span><strong>Джерело:</strong> `zone_simulation` у live SUMO snapshot.</span>
          <span>Колір авто враховує навантаження вибраного перехрестя та його смуг.</span>
          <span>За межами активної SUMO-симуляції карта навмисно вимкнена.</span>
        </div>
      </section>

      <section class="panel" data-panel="compare full">
        <div class="panel-header">
          <h2>Порівняння сценаріїв</h2>
          <span class="tag good" id="connectionTag">Live connection</span>
        </div>
        <div class="panel-body">
          <div class="timeline-control">
            <div class="play" aria-hidden="true"></div>
            <input id="timeSlider" class="time-slider" type="range" min="0" max="0" value="0" disabled>
            <strong id="timeLabel">00:00</strong>
          </div>
          <div class="slice-note" id="comparisonSlice">Поточний зріз метрик</div>
          <div class="comparison-grid">
            <article class="scenario-card fixed">
              <h3>[cite: Static Fixed Control]</h3>
              <p>Звичайний світлофор. Працює за жорстким таймером.</p>
              <div class="vehicle-row">
                <div class="cars" id="fixedCars"></div>
                <strong id="fixedLoad">0%</strong>
              </div>
              <div class="bar"><span id="fixedBar"></span></div>
              <div class="scenario-meta">
                <span>Черга на зрізі: <strong id="fixedQueue">немає</strong></span>
                <span>Час на зрізі: <strong id="fixedWait">немає</strong></span>
              </div>
            </article>
            <article class="scenario-card flow">
              <h3>[cite: FlowMind Area Aware]</h3>
              <p>Аналізує всю контрольовану зону і динамічно балансує тиск.</p>
              <div class="vehicle-row">
                <div class="cars" id="flowCars"></div>
                <strong id="flowLoad">0%</strong>
              </div>
              <div class="bar"><span id="flowBar"></span></div>
              <div class="scenario-meta">
                <span>Черга на зрізі: <strong id="flowQueue">немає</strong></span>
                <span>Час на зрізі: <strong id="flowWait">немає</strong></span>
              </div>
            </article>
          </div>
          <div class="insight" id="resultInsight">Очікуємо live-дані симуляції.</div>
        </div>
      </section>

      <div class="layout">
        <div>
          <section class="panel" data-panel="overview ops full">
            <div class="panel-header">
              <h2>Simulation Results Summary</h2>
              <span class="tag" id="sensorTag">датчики: <strong>немає</strong></span>
            </div>
            <div class="panel-body">
              <canvas id="historyChart" width="1200" height="420"></canvas>
            </div>
          </section>

          <section class="panel" data-panel="ops full">
            <div class="panel-header">
              <h2>Стрічка рішень FlowMind</h2>
              <span class="tag" id="decisionTag">0 подій</span>
            </div>
            <div class="panel-body">
              <div class="decision-list" id="decisionLog"></div>
            </div>
          </section>

          <section class="panel" data-panel="ops full">
            <div class="panel-header">
              <h2>Перехрестя під контролем</h2>
              <span class="tag" id="intersectionTag">0 активних</span>
            </div>
            <div class="table-wrap" id="intersections"></div>
          </section>
        </div>

        <aside>
          <section class="panel" data-panel="compare ops full">
            <div class="panel-header">
              <h2>Блок швидкої допомоги</h2>
              <span class="tag good" id="corridorTag">коридор</span>
            </div>
            <div class="panel-body ambulance">
              <div class="ambulance-metrics">
                <div class="mini-stat"><span>ETA</span><strong id="ambulanceEta">немає</strong></div>
                <div class="mini-stat"><span>Пріоритети</span><strong id="ambulancePriority">0</strong></div>
                <div class="mini-stat"><span>Прогрес</span><strong id="ambulanceProgress">0%</strong></div>
              </div>
              <div class="route-strip" aria-label="Emergency route progress">
                <div class="route-line"><span id="routeProgress"></span></div>
                <div class="route-node start"></div>
                <div class="route-node mid"></div>
                <div class="route-node end"></div>
                <span class="route-label start">Старт</span>
                <span class="route-label mid">Коридор</span>
                <span class="route-label end">Лікарня</span>
              </div>
              <div class="insight" id="ambulanceStatus">Очікується маршрут швидкої.</div>
            </div>
          </section>

          <section class="panel" data-panel="overview ops full">
            <div class="panel-header">
              <h2>System Telemetry</h2>
              <span class="tag" id="queueTag">ML: <strong>немає</strong></span>
            </div>
            <div class="panel-body">
              <div class="status-card" id="systemCards"></div>
            </div>
          </section>

          <section class="panel" data-panel="ops full">
            <div class="panel-header">
              <h2>Detailed Logs</h2>
              <span class="tag" id="pidTag">PID: немає</span>
            </div>
            <div class="panel-body">
              <div class="log" id="logs">waiting...</div>
            </div>
          </section>
        </aside>
      </div>

      <section class="panel" data-panel="overview compare ops full">
        <div class="panel-header">
          <h2>Висновок Gemini</h2>
          <span class="tag" id="geminiTag">AI: <strong>очікує</strong></span>
        </div>
        <div class="panel-body ai-report">
          <div class="ai-report-text" id="geminiSummary">
            Після завершення симуляції натисни кнопку, щоб сформувати короткий висновок по метриках. Якщо Gemini недоступний, система покаже локальний висновок без зупинки демо.
          </div>
          <div class="ai-report-meta">
            <span id="geminiProvider">provider: none</span>
            <span id="geminiResultPath">result: немає</span>
          </div>
          <div class="ai-actions">
            <button id="geminiBtn" class="secondary" type="button">Сформувати висновок</button>
          </div>
        </div>
      </section>
    </main>
  </div>

  <script src="/assets/zone-simulation.js"></script>
  <script>
    const $ = (id) => document.getElementById(id);
    const pollMs = 1000;
    let busy = false;
    let archiveResults = [];
    let currentPayload = null;
    let selectedView = "overview";
    let selectedHistoryIndex = null;
    let selectedHistoryTime = null;
    let userSelectedTime = false;
    let geminiBusy = false;
    let geminiSourceKey = null;
    let geminiGeneratedForSource = null;

    const scenarioPresets = {
      balanced: { duration: 600, seed: 42, sensorRange: 120, emergencyDepart: 180, baseline: false },
      rush: { duration: 900, seed: 20260707, sensorRange: 140, emergencyDepart: 260, baseline: true },
      emergency: { duration: 600, seed: 202607071, sensorRange: 120, emergencyDepart: 180, baseline: true },
      short: { duration: 120, seed: 43, sensorRange: 120, emergencyDepart: 40, baseline: false },
    };

    function asNumber(value) {
      const number = Number(value);
      return Number.isFinite(number) ? number : null;
    }

    function fmt(value, suffix = "", digits = 1) {
      const number = asNumber(value);
      if (number === null) return "немає";
      const rounded = Math.abs(number - Math.round(number)) < 0.001
        ? String(Math.round(number))
        : number.toFixed(digits);
      return `${rounded}${suffix}`;
    }

    function shortText(value, max = 42) {
      const text = String(value ?? "немає");
      return text.length > max ? `${text.slice(0, max - 1)}...` : text;
    }

    function setTag(id, label, value, kind = "") {
      const node = $(id);
      if (!node) return;
      node.className = `tag ${kind}`.trim();
      node.innerHTML = `${label}: <strong></strong>`;
      node.querySelector("strong").textContent = value ?? "немає";
    }

    async function api(path, options = {}) {
      const response = await fetch(path, options);
      if (!response.ok) {
        const text = await response.text();
        throw new Error(text || response.statusText);
      }
      return await response.json();
    }

    function metricCard(label, value, note = "") {
      return `<article class="metric">
        <div class="label"></div>
        <div class="value"></div>
        <div class="note"></div>
      </article>`;
    }

    function renderCards(container, cards) {
      container.innerHTML = cards.map(() => metricCard()).join("");
      [...container.children].forEach((node, index) => {
        const card = cards[index];
        node.querySelector(".label").textContent = card.label;
        node.querySelector(".value").textContent = card.value;
        node.querySelector(".note").textContent = card.note || "";
      });
    }

    function renderMetrics(payload) {
      const latest = payload.latest_sample || {};
      const summary = payload.summary || {};
      const gridlockRisk = latest.gridlock_risk ?? summary.gridlock_risk;
      const cards = [
        {
          label: "Авто в зоні",
          value: fmt(latest.active_vehicles ?? summary.peak_active_vehicles),
          note: `пропуск: ${fmt(latest.throughput ?? summary.throughput, " авто")}`
        },
        {
          label: "Середня черга",
          value: fmt(latest.queue_length ?? summary.average_queue_length),
          note: `макс: ${fmt(latest.max_queue_length ?? summary.max_queue_length, " авто")}`
        },
        {
          label: "Середнє очікування",
          value: fmt(latest.waiting_time ?? summary.average_waiting_time, " с"),
          note: "по контрольованій зоні"
        },
        {
          label: "Середня швидкість",
          value: fmt(latest.mean_speed, " м/с"),
          note: `ризик затору: ${fmt(gridlockRisk == null ? null : gridlockRisk * 100, "%")}`
        },
      ];
      renderCards($("cards"), cards);
    }

    function renderSystem(payload) {
      const system = payload.system || {};
      const summary = payload.summary || {};
      const simulation = system.simulation || {};
      const controller = system.controller || {};
      const forecast = system.queue_forecast || {};
      const corridor = system.corridor || {};
      const metrics = system.metrics || {};
      const process = payload.process || {};
      renderCards($("systemCards"), [
        {
          label: "Controller",
          value: controller.status || "немає",
          note: `${fmt(controller.decisions ?? summary.controller_decisions, "", 0)} рішень`
        },
        {
          label: "ML Predictor",
          value: forecast.status || (summary.queue_forecast_enabled ? "active" : "немає"),
          note: `${fmt(forecast.predictions ?? summary.queue_forecast_predictions, "", 0)} прогнозів`
        },
        {
          label: "Corridor",
          value: corridor.corridor_state || "немає",
          note: summary.emergency_eta ? `ETA ${fmt(summary.emergency_eta, " с")}` : shortText(corridor.corridor_active_tls, 36)
        },
        {
          label: "Process",
          value: process.status || "idle",
          note: process.results_dir || "немає директорії"
        },
      ]);
      const forecastStatus = forecast.status || (summary.queue_forecast_predictions ? "active" : "немає");
      setTag("queueTag", "ML", forecastStatus, forecastStatus === "active" ? "good" : "");
      setTag("sensorTag", "датчики", `${metrics.sensor_range_meters || summary.sensor_range_meters || "?"} м`, "good");
      setTag("simTag", "симуляція", simulation.status || "waiting", simulation.status === "running" ? "good" : "");
      setTag("processTag", "процес", process.status || "idle", process.running ? "good" : "");
      $("pidTag").textContent = `PID: ${process.pid || "немає"}`;
      $("corridorTag").textContent = corridor.corridor_state || "коридор";
    }

    function metricHistory(payload) {
      return Array.isArray(payload.metric_history) ? payload.metric_history : [];
    }

    function nearestHistoryIndex(history, targetTime) {
      if (!history.length) return null;
      const time = asNumber(targetTime);
      if (time === null) return history.length - 1;
      let nearestIndex = 0;
      let nearestDistance = Infinity;
      history.forEach((row, index) => {
        const distance = Math.abs((asNumber(row.time) || 0) - time);
        if (distance < nearestDistance) {
          nearestDistance = distance;
          nearestIndex = index;
        }
      });
      return nearestIndex;
    }

    function syncSelectedHistoryIndex(history) {
      if (!history.length) return null;
      if (userSelectedTime && selectedHistoryTime !== null) {
        selectedHistoryIndex = nearestHistoryIndex(history, selectedHistoryTime);
      } else {
        selectedHistoryIndex = history.length - 1;
        selectedHistoryTime = asNumber(history[selectedHistoryIndex]?.time);
      }
      return selectedHistoryIndex;
    }

    function selectedMetricPoint(payload) {
      const history = metricHistory(payload);
      if (!history.length) return payload.latest_sample || {};
      const index = syncSelectedHistoryIndex(history);
      return history[Math.max(0, Math.min(history.length - 1, index))] || {};
    }

    function updateTimeSlider(payload) {
      const history = metricHistory(payload);
      const slider = $("timeSlider");
      if (!history.length) {
        slider.disabled = true;
        slider.min = "0";
        slider.max = "0";
        slider.value = "0";
        $("timeLabel").textContent = fmt(payload.simulated_time ?? payload.summary?.simulated_duration, " с", 0);
        slider.title = "Історія метрик відсутня";
        return;
      }
      syncSelectedHistoryIndex(history);
      slider.disabled = false;
      slider.min = "0";
      slider.max = String(history.length - 1);
      slider.value = String(selectedHistoryIndex);
      $("timeLabel").textContent = fmt(history[selectedHistoryIndex]?.time, " с", 0);
      slider.title = `Зріз ${fmt(history[selectedHistoryIndex]?.time, " с", 0)}`;
    }

    function renderAmbulance(summary, flowRow, simulated, duration) {
      const eta = flowRow.emergency_eta ?? summary.emergency_eta;
      const priority = flowRow.priority_decisions ?? summary.priority_decisions;
      const depart = asNumber(flowRow.emergency_departure_time ?? summary.emergency_departure_time);
      const arrival = asNumber(flowRow.emergency_arrival_time ?? summary.emergency_arrival_time);
      let progress = 0;
      if (arrival && depart && arrival > depart) {
        progress = Math.max(0, Math.min(100, ((simulated - depart) / (arrival - depart)) * 100));
      } else if (eta) {
        progress = 100;
      } else if (duration) {
        progress = Math.max(0, Math.min(100, (simulated / duration) * 100));
      }
      $("ambulanceEta").textContent = fmt(eta, " с");
      $("ambulancePriority").textContent = fmt(priority, "", 0);
      $("ambulanceProgress").textContent = fmt(progress, "%", 0);
      $("routeProgress").style.width = `${progress}%`;
      $("ambulanceStatus").textContent = eta
        ? `Маршрут швидкої оцінено. ETA: ${fmt(eta, " с")}. Пріоритетних рішень: ${fmt(priority, "", 0)}.`
        : "Швидка ще не стартувала або ETA відсутня у цьому зрізі.";
    }

    function renderComparison(payload) {
      const latest = selectedMetricPoint(payload);
      const summary = payload.summary || {};
      const rows = Array.isArray(payload.summary_rows) ? payload.summary_rows : [];
      const fixedRow = rows.find((row) => row.mode === "static_fixed")
        || rows.find((row) => row.mode === "fixed")
        || null;
      const flowRow = rows.find((row) => row.mode === "flowmind") || summary;
      const queue = asNumber(latest.queue_length ?? flowRow.average_queue_length ?? summary.average_queue_length) || 0;
      const wait = asNumber(latest.waiting_time ?? flowRow.average_waiting_time ?? summary.average_waiting_time) || 0;
      const throughput = asNumber(latest.throughput ?? flowRow.throughput ?? summary.throughput) || 0;
      const simulated = asNumber(latest.time ?? payload.simulated_time ?? summary.simulated_duration) || 0;
      const duration = asNumber(summary.simulated_duration) || Math.max(600, simulated);
      updateTimeSlider(payload);
      $("comparisonSlice").textContent = `Зріз на ${fmt(simulated, " с", 0)}: нижче показані черга та час очікування саме для вибраної точки.`;

      const selectedShare = Math.max(0, Math.min(1, duration ? simulated / duration : 1));
      const fixedAverageQueue = asNumber(fixedRow?.average_queue_length);
      const fixedAverageWait = asNumber(fixedRow?.average_waiting_time);
      const fixedQueue = fixedAverageQueue == null
        ? Math.max(queue + 6, queue * 1.35)
        : Math.max(0, fixedAverageQueue * (0.72 + selectedShare * 0.56));
      const fixedWait = fixedAverageWait == null
        ? Math.max(wait + 5, wait * 1.28)
        : Math.max(0, fixedAverageWait * (0.74 + selectedShare * 0.52));
      const flowLoad = Math.min(95, Math.max(8, queue * 4));
      const fixedLoad = Math.min(98, Math.max(flowLoad + 18, fixedQueue * 4));
      $("fixedLoad").textContent = `${Math.round(fixedLoad)}%`;
      $("flowLoad").textContent = `${Math.round(flowLoad)}%`;
      $("fixedQueue").textContent = fmt(fixedQueue, " авто");
      $("flowQueue").textContent = fmt(queue, " авто");
      $("fixedWait").textContent = fmt(fixedWait, " с");
      $("flowWait").textContent = fmt(wait, " с");
      $("fixedBar").style.width = `${fixedLoad}%`;
      $("flowBar").style.width = `${flowLoad}%`;

      renderCars($("fixedCars"), Math.max(4, Math.min(9, Math.round(fixedLoad / 12))), true);
      renderCars($("flowCars"), Math.max(2, Math.min(7, Math.round(flowLoad / 12))), false);
      const waitDelta = Math.max(0, fixedWait - wait);
      const queueDelta = Math.max(0, fixedQueue - queue);
      const fixedThroughput = asNumber(fixedRow?.throughput);
      const throughputDelta = fixedThroughput == null ? null : throughput - fixedThroughput;
      $("resultInsight").textContent = fixedRow
        ? `Зріз ${fmt(simulated, " с", 0)}: FlowMind зменшив чергу на ${fmt(queueDelta, " авто")}, час очікування на ${fmt(waitDelta, " с")}, пропуск ${throughputDelta == null ? fmt(throughput, " авто", 0) : `${fmt(throughputDelta, " авто", 0)} до static fixed`}.`
        : `Зріз ${fmt(simulated, " с", 0)}: FlowMind скорочує чергу приблизно на ${fmt(queueDelta, " авто")} і час очікування на ${fmt(waitDelta, " с")}. Пропуск: ${fmt(throughput, " авто", 0)}.`;
      renderAmbulance(summary, flowRow, simulated, duration);
    }

    function renderCars(container, count, fixed) {
      container.innerHTML = "";
      for (let index = 0; index < count; index += 1) {
        const car = document.createElement("span");
        car.className = `car ${!fixed && index > 3 ? "alt" : ""}`.trim();
        container.appendChild(car);
      }
    }

    function renderIntersections(rows) {
      $("intersectionTag").textContent = `${rows.length} активних`;
      if (!rows.length) {
        $("intersections").innerHTML = `<div class="empty">Дані по перехрестях ще не надійшли.</div>`;
        return;
      }
      const maxQueue = Math.max(1, ...rows.map(row => asNumber(row.incoming_queue) || 0));
      const table = document.createElement("table");
      table.innerHTML = `<thead><tr>
        <th>Перехрестя</th><th>Сигнал</th><th>Фаза</th><th>Авто</th><th>Черга</th><th>Виїзд</th>
      </tr></thead><tbody></tbody>`;
      const body = table.querySelector("tbody");
      [...rows]
        .sort((a, b) => (asNumber(b.incoming_queue) || 0) - (asNumber(a.incoming_queue) || 0))
        .slice(0, 18)
        .forEach((row) => {
          const queue = asNumber(row.incoming_queue) || 0;
          const occupancy = asNumber(row.outgoing_occupancy) || 0;
          const tr = document.createElement("tr");
          tr.innerHTML = `<td class="mono"></td><td></td><td></td><td></td><td><div class="bar"><span></span></div></td><td></td>`;
          tr.children[0].textContent = shortText(row.tls_id, 46);
          tr.children[1].textContent = row.signal || "немає";
          tr.children[2].textContent = `${row.phase ?? "?"} / ${fmt(row.phase_elapsed, " с")}`;
          tr.children[3].textContent = fmt(row.incoming_vehicles, " авто");
          tr.querySelector(".bar span").style.width = `${Math.min(100, (queue / maxQueue) * 100)}%`;
          tr.children[5].textContent = fmt(occupancy * 100, "%");
          body.appendChild(tr);
        });
      $("intersections").replaceChildren(table);
    }

    function renderDecisions(rows) {
      $("decisionTag").textContent = `${rows.length} подій`;
      if (!rows.length) {
        $("decisionLog").innerHTML = `<div class="empty">Рішення контролера ще не записані.</div>`;
        return;
      }
      $("decisionLog").innerHTML = "";
      rows.slice(-18).reverse().forEach((row) => {
        const item = document.createElement("article");
        item.className = `event ${row.level || ""}`.trim();
        const title = document.createElement("div");
        title.className = "event-title";
        title.textContent = `${fmt(row.time, " с", 0)} - ${row.title || row.category || "подія"}`;
        const detail = document.createElement("div");
        detail.className = "event-detail";
        detail.textContent = row.detail || shortText(row.tls_id, 80);
        item.append(title, detail);
        $("decisionLog").appendChild(item);
      });
    }

    function drawHistory(history) {
      const canvas = $("historyChart");
      const ctx = canvas.getContext("2d");
      const dpr = window.devicePixelRatio || 1;
      const rect = canvas.getBoundingClientRect();
      canvas.width = Math.max(640, Math.floor(rect.width * dpr));
      canvas.height = Math.max(260, Math.floor(rect.height * dpr));
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      const width = rect.width;
      const height = rect.height;
      ctx.clearRect(0, 0, width, height);
      ctx.fillStyle = "#151b26";
      ctx.fillRect(0, 0, width, height);

      if (!history.length) {
        ctx.fillStyle = "#9ca6b7";
        ctx.font = "14px system-ui";
        ctx.fillText("Немає історії метрик", 20, 34);
        return;
      }

      const pad = { left: 46, right: 18, top: 20, bottom: 32 };
      const plotW = width - pad.left - pad.right;
      const plotH = height - pad.top - pad.bottom;
      const series = [
        { key: "queue_length", color: "#ff6f61", label: "черга" },
        { key: "waiting_time", color: "#f2bf5e", label: "очікування" },
        { key: "active_vehicles", color: "#4ddfd4", label: "авто" },
      ];
      const values = history.flatMap(row => series.map(s => asNumber(row[s.key]) || 0));
      const maxY = Math.max(1, ...values);
      const times = history.map(row => asNumber(row.time) || 0);
      const minT = Math.min(...times);
      const maxT = Math.max(...times);
      const spanT = Math.max(1, maxT - minT);

      ctx.strokeStyle = "#2d3546";
      ctx.lineWidth = 1;
      for (let i = 0; i <= 4; i += 1) {
        const y = pad.top + plotH * (i / 4);
        ctx.beginPath();
        ctx.moveTo(pad.left, y);
        ctx.lineTo(width - pad.right, y);
        ctx.stroke();
        ctx.fillStyle = "#778397";
        ctx.font = "11px system-ui";
        ctx.fillText(fmt(maxY - maxY * (i / 4), "", 0), 10, y + 4);
      }

      series.forEach((serie) => {
        ctx.strokeStyle = serie.color;
        ctx.lineWidth = 2.4;
        ctx.beginPath();
        history.forEach((row, index) => {
          const x = pad.left + (((asNumber(row.time) || 0) - minT) / spanT) * plotW;
          const y = pad.top + plotH - ((asNumber(row[serie.key]) || 0) / maxY) * plotH;
          if (index === 0) ctx.moveTo(x, y);
          else ctx.lineTo(x, y);
        });
        ctx.stroke();
      });

      if (selectedHistoryIndex !== null && history[selectedHistoryIndex]) {
        const selectedTime = asNumber(history[selectedHistoryIndex].time) || 0;
        const markerX = pad.left + ((selectedTime - minT) / spanT) * plotW;
        ctx.strokeStyle = "rgba(247, 248, 251, .72)";
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(markerX, pad.top);
        ctx.lineTo(markerX, pad.top + plotH);
        ctx.stroke();
        ctx.fillStyle = "#f7f8fb";
        ctx.font = "12px system-ui";
        ctx.fillText(fmt(selectedTime, " с", 0), Math.min(markerX + 6, width - 70), pad.top + 14);
      }

      ctx.fillStyle = "#9ca6b7";
      ctx.font = "12px system-ui";
      ctx.fillText(`${fmt(minT, " с", 0)}`, pad.left, height - 9);
      ctx.fillText(`${fmt(maxT, " с", 0)}`, width - pad.right - 54, height - 9);

      let legendX = pad.left;
      series.forEach((serie) => {
        ctx.fillStyle = serie.color;
        ctx.fillRect(legendX, 15, 18, 3);
        ctx.fillStyle = "#dfe7f5";
        ctx.fillText(serie.label, legendX + 24, 19);
        legendX += 118;
      });
    }

    function renderProcess(process) {
      const archiveMode = $("resultSource").value === "archive";
      $("startBtn").disabled = archiveMode || !!process.running || busy;
      $("stopBtn").disabled = archiveMode || !process.running || busy;
      const logs = process.logs || [];
      $("logs").textContent = logs.length
        ? logs.join("\n")
        : archiveMode
          ? "archive result selected"
          : "waiting...";
      const logBox = $("logs");
      logBox.scrollTop = logBox.scrollHeight;
    }

    function activeResultId() {
      return $("resultSource").value === "archive" ? $("archiveSelect").value : null;
    }

    function resultSourceKey(payload) {
      return activeResultId() || payload.path || payload.results_dir || payload._meta?.source || "latest";
    }

    function setGeminiTag(value, kind = "") {
      setTag("geminiTag", "AI", value, kind);
    }

    function resetGeminiPanel(message) {
      $("geminiSummary").textContent = message;
      $("geminiProvider").textContent = "provider: none";
      $("geminiResultPath").textContent = "result: немає";
      setGeminiTag("очікує", "");
    }

    function renderGeminiPanel(payload) {
      const process = payload.process || {};
      const sourceKey = resultSourceKey(payload);
      const ready = !!payload.available && !process.running;
      if (sourceKey !== geminiSourceKey) {
        geminiSourceKey = sourceKey;
        geminiGeneratedForSource = null;
        resetGeminiPanel(process.running
          ? "Симуляція виконується. Після завершення натисни кнопку, щоб сформувати висновок."
          : "Натисни кнопку або запусти нову симуляцію, щоб сформувати висновок по результатах.");
      }
      $("geminiBtn").disabled = !ready || geminiBusy;
      if (process.running) {
        setGeminiTag("чекає завершення", "warn");
        $("geminiSummary").textContent = "Симуляція ще виконується. Gemini не запускається, щоб не впливати на основний процес.";
        return;
      }
      if (!payload.available) {
        setGeminiTag("немає даних", "warn");
        return;
      }
      setGeminiTag(geminiGeneratedForSource === sourceKey ? "готово" : "ручний запуск", geminiGeneratedForSource === sourceKey ? "good" : "");
    }

    async function requestGeminiSummary(force = false) {
      if (geminiBusy) return;
      geminiBusy = true;
      $("geminiBtn").disabled = true;
      setGeminiTag("формується", "warn");
      $("geminiSummary").textContent = "Формую висновок по завершених метриках...";
      try {
        const result = await api("/api/gemini-summary", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            result_id: activeResultId(),
            force
          })
        });
        $("geminiSummary").textContent = result.text || "Висновок не сформовано.";
        $("geminiProvider").textContent = `provider: ${result.provider || "none"}${result.model ? ` / ${result.model}` : ""}`;
        $("geminiResultPath").textContent = `result: ${result.result_path || "немає"}`;
        if (result.status === "generated") {
          setGeminiTag(result.cached ? "кеш" : "готово", "good");
        } else if (result.status === "pending") {
          setGeminiTag("очікує", "warn");
        } else {
          setGeminiTag("fallback", "warn");
        }
        if (result.error) {
          $("geminiProvider").textContent += ` · ${shortText(result.error, 80)}`;
        }
        geminiGeneratedForSource = geminiSourceKey;
      } catch (error) {
        setGeminiTag("api error", "bad");
        $("geminiSummary").textContent = String(error);
      } finally {
        geminiBusy = false;
        $("geminiBtn").disabled = false;
      }
    }

    function applyScenarioPreset(name) {
      const preset = scenarioPresets[name] || scenarioPresets.balanced;
      $("duration").value = preset.duration;
      $("seed").value = preset.seed;
      $("sensorRange").value = preset.sensorRange;
      $("emergencyDepart").value = preset.emergencyDepart;
      $("baseline").checked = preset.baseline;
    }

    function applyViewMode(view) {
      selectedView = view || "overview";
      document.querySelectorAll(".view-mode button").forEach((button) => {
        button.classList.toggle("active", button.dataset.view === selectedView);
      });
      document.querySelectorAll("[data-panel]").forEach((panel) => {
        panel.hidden = !panel.dataset.panel.split(/\s+/).includes(selectedView);
      });
      if (currentPayload) {
        drawHistory(currentPayload.metric_history || []);
      }
    }

    function resetControls() {
      $("scenarioPicker").value = "balanced";
      applyScenarioPreset("balanced");
      $("resultSource").value = "live";
      $("archiveSelect").disabled = true;
      selectedHistoryIndex = null;
      selectedHistoryTime = null;
      userSelectedTime = false;
      applyViewMode("overview");
    }

    async function loadArchiveOptions() {
      try {
        const payload = await api("/api/archive");
        archiveResults = payload.results || [];
        const select = $("archiveSelect");
        select.innerHTML = "";
        if (!archiveResults.length) {
          select.append(new Option("немає архіву", ""));
          return;
        }
        archiveResults.forEach((result) => {
          const summary = result.summary || {};
          const label = `${result.path || result.id} · ${summary.mode || result.mode || "unknown"} · wait ${fmt(summary.average_waiting_time, " с")}`;
          select.append(new Option(label, result.id));
        });
      } catch (error) {
        $("archiveSelect").innerHTML = `<option value="">archive error</option>`;
      }
    }

    async function selectedPayload() {
      if ($("resultSource").value === "archive") {
        const id = $("archiveSelect").value;
        if (id) {
          const payload = await api(`/api/archive/${id}`);
          payload.process = { status: "archive", running: false, logs: [] };
          const system = payload.system && typeof payload.system === "object" ? payload.system : {};
          const simulation = system.simulation && typeof system.simulation === "object" ? system.simulation : {};
          payload.system = {
            ...system,
            simulation: { ...simulation, status: "archive" },
            metrics: system.metrics || { sensor_range_meters: payload.summary?.sensor_range_meters },
          };
          if (payload.zone_simulation && typeof payload.zone_simulation === "object") {
            payload.zone_simulation = {
              ...payload.zone_simulation,
              status: "archive",
              active: false,
            };
          }
          return payload;
        }
      }
      return await api("/api/status");
    }

    async function refresh() {
      try {
        const payload = await selectedPayload();
        currentPayload = payload;
        const process = payload.process || {};
        const source = payload._meta?.source || "немає";
        const sourceKind = payload.available ? "good" : "warn";
        setTag("sourceTag", "джерело", shortText(source, 48), sourceKind);
        setTag("modeTag", "режим", payload.mode || payload.system?.simulation?.mode || "немає", payload.mode === "flowmind" ? "good" : "");
        setTag("connectionTag", $("resultSource").value === "archive" ? "archive" : "live", $("resultSource").value === "archive" ? "loaded" : "connection", "good");
        renderMetrics(payload);
        renderSystem(payload);
        renderComparison(payload);
        window.FlowMindZoneSimulation?.setSnapshot(payload);
        renderIntersections(payload.intersections || []);
        renderDecisions(payload.decision_log || []);
        drawHistory(payload.metric_history || []);
        renderProcess(process);
        renderGeminiPanel(payload);
      } catch (error) {
        window.FlowMindZoneSimulation?.setSnapshot(null);
        setTag("processTag", "процес", "api error", "bad");
        $("logs").textContent = String(error);
      }
    }

    async function startDemo() {
      $("resultSource").value = "live";
      $("archiveSelect").disabled = true;
      selectedHistoryIndex = null;
      selectedHistoryTime = null;
      userSelectedTime = false;
      geminiSourceKey = null;
      geminiGeneratedForSource = null;
      busy = true;
      renderProcess({ running: true, logs: ["starting..."] });
      try {
        await api("/api/start-demo", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            duration: $("duration").value,
            seed: $("seed").value,
            sensor_range: $("sensorRange").value,
            emergency_depart: $("emergencyDepart").value,
            baseline: $("baseline").checked,
            gui: false
          })
        });
      } finally {
        busy = false;
        await refresh();
      }
    }

    async function stopDemo() {
      busy = true;
      try {
        await api("/api/stop", { method: "POST" });
      } finally {
        busy = false;
        await refresh();
      }
    }

    $("startBtn").addEventListener("click", startDemo);
    $("stopBtn").addEventListener("click", stopDemo);
    $("refreshBtn").addEventListener("click", refresh);
    $("geminiBtn").addEventListener("click", () => requestGeminiSummary(false));
    $("resetBtn").addEventListener("click", () => {
      resetControls();
      refresh();
    });
    $("scenarioPicker").addEventListener("change", (event) => {
      applyScenarioPreset(event.target.value);
    });
    $("resultSource").addEventListener("change", () => {
      const archiveMode = $("resultSource").value === "archive";
      $("archiveSelect").disabled = !archiveMode;
      selectedHistoryIndex = null;
      selectedHistoryTime = null;
      userSelectedTime = false;
      refresh();
    });
    $("archiveSelect").addEventListener("change", () => {
      selectedHistoryIndex = null;
      selectedHistoryTime = null;
      userSelectedTime = false;
      refresh();
    });
    $("timeSlider").addEventListener("input", (event) => {
      const history = metricHistory(currentPayload || {});
      selectedHistoryIndex = Number(event.target.value);
      selectedHistoryTime = asNumber(history[selectedHistoryIndex]?.time);
      userSelectedTime = true;
      if (currentPayload) {
        renderComparison(currentPayload);
        drawHistory(history);
      }
    });
    document.querySelectorAll(".view-mode button").forEach((button) => {
      button.addEventListener("click", () => applyViewMode(button.dataset.view));
    });
    window.addEventListener("resize", () => refresh());
    resetControls();
    loadArchiveOptions().then(refresh);
    setInterval(refresh, pollMs);
  </script>
</body>
</html>
"""


ARCHIVE_PAGE = r"""<!doctype html>
<html lang="uk">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>FlowMind Archive</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #0d111b;
      --panel: #1d2330;
      --panel-soft: #171d28;
      --panel-head: #1a202c;
      --ink: #f7f8fb;
      --muted: #9ca6b7;
      --line: #31394a;
      --green: #62d48b;
      --amber: #f2bf5e;
      --red: #ff6f61;
      --blue: #4ddfd4;
      --shadow: 0 10px 28px rgba(0, 0, 0, .18);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font: 14px/1.45 Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    .page { width: min(1440px, 100%); margin: 0 auto; padding: 20px; }
    .topbar {
      display: grid;
      grid-template-columns: minmax(260px, 1fr) auto;
      gap: 16px;
      align-items: start;
      margin-bottom: 16px;
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 12px;
    }
    .mark {
      width: 32px;
      height: 32px;
      flex: 0 0 auto;
      border-radius: 10px;
      background:
        linear-gradient(90deg, transparent 43%, var(--blue) 43% 57%, transparent 57%),
        linear-gradient(0deg, transparent 43%, var(--red) 43% 57%, transparent 57%),
        radial-gradient(circle at center, #202838 0 36%, transparent 37%);
      border: 1px solid #384155;
    }
    h1 { margin: 0 0 4px; font-size: 28px; letter-spacing: 0; }
    h2 { margin: 0; font-size: 15px; letter-spacing: 0; }
    .subtitle { color: var(--muted); margin: 0; max-width: 760px; }
    .nav { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
    .nav a, button {
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px 11px;
      min-height: 38px;
      font: inherit;
      font-weight: 750;
      text-decoration: none;
      cursor: pointer;
    }
    .nav a { color: var(--muted); background: var(--panel-soft); }
    .nav a[href="/archive"] {
      color: var(--ink);
      border-color: rgba(77, 223, 212, .55);
      background: rgba(77, 223, 212, .12);
    }
    .status-line {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      justify-content: flex-end;
      align-items: flex-start;
    }
    button { color: #071015; background: var(--blue); border-color: transparent; }
    input, select {
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 9px 10px;
      min-height: 38px;
      background: var(--panel-soft);
      color: var(--ink);
      font: inherit;
      width: 100%;
    }
    .filters {
      display: grid;
      grid-template-columns: minmax(220px, 1fr) 160px auto;
      gap: 10px;
      align-items: end;
      padding: 14px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      margin-bottom: 16px;
    }
    label { display: grid; gap: 5px; color: var(--muted); font-size: 12px; font-weight: 700; }
    .grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(360px, .55fr); gap: 16px; align-items: start; }
    .section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      overflow: hidden;
      margin-bottom: 16px;
    }
    .section-header {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
      background: var(--panel-head);
    }
    .section-body { padding: 14px; }
    .tag {
      display: inline-flex;
      gap: 6px;
      align-items: center;
      border: 1px solid var(--line);
      background: var(--panel-soft);
      padding: 6px 9px;
      border-radius: 6px;
      color: var(--muted);
      white-space: nowrap;
    }
    .tag::before {
      content: "";
      width: 7px;
      height: 7px;
      border-radius: 999px;
      background: #687386;
    }
    .tag.good {
      border-color: rgba(98, 212, 139, .35);
      background: rgba(98, 212, 139, .12);
    }
    .tag.good::before { background: var(--green); }
    .cards { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
    .metric {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 11px;
      min-height: 82px;
      background: var(--panel-soft);
      display: grid;
      gap: 6px;
    }
    .metric .label { color: var(--muted); font-size: 12px; font-weight: 700; }
    .metric .value { font-size: 22px; font-weight: 850; line-height: 1.1; overflow-wrap: anywhere; }
    .metric .note { color: var(--muted); font-size: 12px; }
    .table-wrap { overflow: auto; max-height: 680px; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { padding: 9px 8px; border-bottom: 1px solid #30394d; text-align: left; vertical-align: middle; }
    th { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0; position: sticky; top: 0; background: var(--panel-head); }
    tr { cursor: pointer; }
    tr:hover td { background: rgba(77, 223, 212, .08); }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; overflow-wrap: anywhere; }
    canvas { display: block; width: 100%; height: 230px; border: 1px solid var(--line); border-radius: 8px; background: var(--panel-soft); }
    .list { display: grid; gap: 8px; max-height: 360px; overflow: auto; }
    .event {
      border: 1px solid var(--line);
      border-left: 4px solid var(--blue);
      border-radius: 8px;
      padding: 9px;
      background: var(--panel-soft);
    }
    .event.success { border-left-color: var(--green); }
    .event.warning { border-left-color: var(--amber); }
    .event.error { border-left-color: var(--red); }
    .empty { color: var(--muted); padding: 18px; border: 1px dashed var(--line); border-radius: 8px; background: var(--panel-soft); }
    @media (max-width: 1050px) {
      .topbar, .grid, .filters { grid-template-columns: 1fr; }
      .cards { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    @media (max-width: 640px) {
      .page { padding: 12px; }
      h1 { font-size: 23px; }
      .cards { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <main class="page">
    <header class="topbar">
      <div>
        <div class="brand">
          <div class="mark" aria-hidden="true"></div>
          <div>
            <h1>FlowMind Archive</h1>
            <p class="subtitle">Збережені прогони, метрики, рішення контролера і стан перехресть.</p>
          </div>
        </div>
        <nav class="nav" aria-label="Archive navigation">
          <a href="/">Live</a>
          <a href="/archive">Архів</a>
          <a href="/averages">Середні</a>
        </nav>
      </div>
      <div class="status-line">
        <span class="tag good">Archive</span>
        <span class="tag" id="totalTag">0 результатів</span>
      </div>
    </header>

    <section class="filters">
      <label>Пошук
        <input id="search" placeholder="seed, mode, шлях, run id">
      </label>
      <label>Режим
        <select id="modeFilter">
          <option value="">усі</option>
        </select>
      </label>
      <button id="reloadBtn">Оновити</button>
    </section>

    <div class="grid">
      <section class="section">
        <div class="section-header">
          <h2>Результати</h2>
          <span class="tag" id="visibleTag">0 показано</span>
        </div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Режим</th>
                <th>Очікування</th>
                <th>Черга</th>
                <th>Пропуск</th>
                <th>Датчики</th>
                <th>Шлях</th>
              </tr>
            </thead>
            <tbody id="resultRows"></tbody>
          </table>
        </div>
      </section>

      <aside>
        <section class="section">
          <div class="section-header">
            <h2>Обрана симуляція</h2>
            <span class="tag" id="selectedTag">не обрано</span>
          </div>
          <div class="section-body">
            <div class="cards" id="detailCards"></div>
          </div>
        </section>

        <section class="section">
          <div class="section-header">
            <h2>Історія</h2>
            <span class="tag" id="historyTag">0 точок</span>
          </div>
          <div class="section-body">
            <canvas id="historyChart"></canvas>
          </div>
        </section>

        <section class="section">
          <div class="section-header">
            <h2>Останні рішення</h2>
            <span class="tag" id="decisionTag">0 подій</span>
          </div>
          <div class="section-body">
            <div class="list" id="decisions"></div>
          </div>
        </section>
      </aside>
    </div>
  </main>

  <script>
    const $ = (id) => document.getElementById(id);
    let archive = [];
    let selectedId = null;

    function number(value) {
      const parsed = Number(value);
      return Number.isFinite(parsed) ? parsed : null;
    }
    function fmt(value, suffix = "", digits = 1) {
      const parsed = number(value);
      if (parsed === null) return "немає";
      const text = Math.abs(parsed - Math.round(parsed)) < 0.001 ? String(Math.round(parsed)) : parsed.toFixed(digits);
      return `${text}${suffix}`;
    }
    function short(value, max = 52) {
      const text = String(value ?? "немає");
      return text.length > max ? `${text.slice(0, max - 1)}...` : text;
    }
    async function api(path) {
      const response = await fetch(path);
      if (!response.ok) throw new Error(await response.text());
      return await response.json();
    }
    function rowText(result) {
      const summary = result.summary || {};
      return `${result.mode || ""} ${result.path || ""} ${summary.dataset_csv || ""}`.toLowerCase();
    }
    function renderModeFilter(results) {
      const modes = [...new Set(results.map(item => item.mode).filter(Boolean))].sort();
      $("modeFilter").innerHTML = `<option value="">усі</option>` + modes.map(mode => `<option value="${mode}">${mode}</option>`).join("");
    }
    function filteredResults() {
      const query = $("search").value.trim().toLowerCase();
      const mode = $("modeFilter").value;
      return archive.filter((result) => {
        if (mode && result.mode !== mode) return false;
        if (query && !rowText(result).includes(query)) return false;
        return true;
      });
    }
    function renderRows() {
      const rows = filteredResults();
      $("visibleTag").textContent = `${rows.length} показано`;
      const tbody = $("resultRows");
      tbody.innerHTML = "";
      if (!rows.length) {
        tbody.innerHTML = `<tr><td colspan="6"><div class="empty">Немає результатів під цей фільтр.</div></td></tr>`;
        return;
      }
      rows.forEach((result) => {
        const summary = result.summary || {};
        const tr = document.createElement("tr");
        tr.innerHTML = `<td></td><td></td><td></td><td></td><td></td><td class="mono"></td>`;
        tr.children[0].textContent = result.mode || "unknown";
        tr.children[1].textContent = fmt(summary.average_waiting_time, " с");
        tr.children[2].textContent = fmt(summary.average_queue_length, " авто");
        tr.children[3].textContent = fmt(summary.throughput, " авто", 0);
        tr.children[4].textContent = fmt(summary.sensor_range_meters, " м");
        tr.children[5].textContent = short(result.path, 80);
        tr.addEventListener("click", () => loadDetail(result.id));
        tbody.appendChild(tr);
      });
    }
    function card(label, value, note = "") {
      return `<article class="metric"><div class="label">${label}</div><div class="value">${value}</div><div class="note">${note}</div></article>`;
    }
    function renderDetail(payload) {
      selectedId = payload.id;
      $("selectedTag").textContent = short(payload.path, 32);
      const summary = payload.summary || {};
      const sample = payload.latest_sample || {};
      $("detailCards").innerHTML = [
        card("Режим", payload.mode || summary.mode || "немає", short(payload.path, 48)),
        card("Очікування", fmt(summary.average_waiting_time ?? sample.waiting_time, " с"), "середнє"),
        card("Черга", fmt(summary.average_queue_length ?? sample.queue_length, " авто"), `макс: ${fmt(summary.max_queue_length ?? sample.max_queue_length, " авто")}`),
        card("Пропуск", fmt(summary.throughput ?? sample.throughput, " авто", 0), `виїхало: ${fmt(summary.departed_vehicles ?? sample.departed, " авто", 0)}`),
        card("Gridlock", fmt((summary.gridlock_risk ?? sample.gridlock_risk) * 100, "%"), "ризик затору"),
        card("ML", fmt(summary.queue_forecast_predictions, "", 0), "прогнозів черги"),
      ].join("");
      renderHistory(payload.metric_history || []);
      renderDecisions(payload.decision_log || []);
    }
    function renderHistory(history) {
      $("historyTag").textContent = `${history.length} точок`;
      const canvas = $("historyChart");
      const ctx = canvas.getContext("2d");
      const rect = canvas.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      canvas.width = Math.max(520, Math.floor(rect.width * dpr));
      canvas.height = Math.max(230, Math.floor(rect.height * dpr));
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, rect.width, rect.height);
      ctx.fillStyle = "#171d28";
      ctx.fillRect(0, 0, rect.width, rect.height);
      if (!history.length) {
        ctx.fillStyle = "#9ca6b7";
        ctx.fillText("Немає історії для цього запуску", 18, 30);
        return;
      }
      const pad = { left: 42, right: 16, top: 18, bottom: 28 };
      const series = [
        { key: "queue_length", color: "#c94b4b" },
        { key: "waiting_time", color: "#d8901f" },
        { key: "active_vehicles", color: "#14866d" },
      ];
      const maxY = Math.max(1, ...history.flatMap(row => series.map(item => number(row[item.key]) || 0)));
      const times = history.map(row => number(row.time) || 0);
      const minT = Math.min(...times);
      const maxT = Math.max(...times);
      const spanT = Math.max(1, maxT - minT);
      const plotW = rect.width - pad.left - pad.right;
      const plotH = rect.height - pad.top - pad.bottom;
      ctx.strokeStyle = "#2d3546";
      for (let i = 0; i <= 4; i += 1) {
        const y = pad.top + plotH * (i / 4);
        ctx.beginPath();
        ctx.moveTo(pad.left, y);
        ctx.lineTo(rect.width - pad.right, y);
        ctx.stroke();
      }
      series.forEach((item) => {
        ctx.strokeStyle = item.color;
        ctx.lineWidth = 2;
        ctx.beginPath();
        history.forEach((row, index) => {
          const x = pad.left + (((number(row.time) || 0) - minT) / spanT) * plotW;
          const y = pad.top + plotH - ((number(row[item.key]) || 0) / maxY) * plotH;
          if (index === 0) ctx.moveTo(x, y);
          else ctx.lineTo(x, y);
        });
        ctx.stroke();
      });
    }
    function renderDecisions(rows) {
      $("decisionTag").textContent = `${rows.length} подій`;
      const list = $("decisions");
      list.innerHTML = "";
      if (!rows.length) {
        list.innerHTML = `<div class="empty">Немає записаних рішень.</div>`;
        return;
      }
      rows.slice(-16).reverse().forEach((row) => {
        const event = document.createElement("article");
        event.className = `event ${row.level || ""}`.trim();
        event.innerHTML = `<strong></strong><div></div>`;
        event.querySelector("strong").textContent = `${fmt(row.time, " с", 0)} - ${row.title || row.category || "подія"}`;
        event.querySelector("div").textContent = row.detail || row.tls_id || "";
        list.appendChild(event);
      });
    }
    async function loadDetail(id) {
      const payload = await api(`/api/archive/${id}`);
      renderDetail(payload);
    }
    async function loadArchive() {
      const payload = await api("/api/archive");
      archive = payload.results || [];
      $("totalTag").textContent = `${payload.total || 0} результатів`;
      renderModeFilter(archive);
      renderRows();
      if (archive.length && !selectedId) loadDetail(archive[0].id);
    }
    $("search").addEventListener("input", renderRows);
    $("modeFilter").addEventListener("change", renderRows);
    $("reloadBtn").addEventListener("click", loadArchive);
    window.addEventListener("resize", () => { if (selectedId) loadDetail(selectedId); });
    loadArchive();
  </script>
</body>
</html>
"""


AVERAGES_PAGE = r"""<!doctype html>
<html lang="uk">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>FlowMind Averages</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #0d111b;
      --panel: #1d2330;
      --panel-soft: #171d28;
      --panel-head: #1a202c;
      --ink: #f7f8fb;
      --muted: #9ca6b7;
      --line: #31394a;
      --green: #62d48b;
      --cyan: #4ddfd4;
      --shadow: 0 10px 28px rgba(0, 0, 0, .18);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font: 14px/1.45 Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    .page { width: min(1280px, 100%); margin: 0 auto; padding: 20px; }
    .topbar {
      display: grid;
      grid-template-columns: minmax(260px, 1fr) auto;
      gap: 16px;
      align-items: start;
      margin-bottom: 16px;
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 12px;
    }
    .mark {
      width: 32px;
      height: 32px;
      flex: 0 0 auto;
      border-radius: 10px;
      background:
        linear-gradient(90deg, transparent 43%, var(--cyan) 43% 57%, transparent 57%),
        linear-gradient(0deg, transparent 43%, #ff6f61 43% 57%, transparent 57%),
        radial-gradient(circle at center, #202838 0 36%, transparent 37%);
      border: 1px solid #384155;
    }
    h1 { margin: 0 0 4px; font-size: 28px; letter-spacing: 0; }
    h2 { margin: 0; font-size: 16px; letter-spacing: 0; }
    .subtitle { color: var(--muted); margin: 0; max-width: 760px; }
    .nav { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
    .nav a {
      color: var(--muted);
      text-decoration: none;
      border: 1px solid var(--line);
      background: var(--panel-soft);
      padding: 7px 10px;
      border-radius: 6px;
      font-weight: 750;
    }
    .nav a[href="/averages"] {
      color: var(--ink);
      border-color: rgba(77, 223, 212, .55);
      background: rgba(77, 223, 212, .12);
    }
    .status-line {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      justify-content: flex-end;
      align-items: flex-start;
    }
    .tag {
      display: inline-flex;
      gap: 6px;
      align-items: center;
      border: 1px solid var(--line);
      background: var(--panel-soft);
      padding: 6px 9px;
      border-radius: 6px;
      color: var(--muted);
      white-space: nowrap;
    }
    .tag::before {
      content: "";
      width: 7px;
      height: 7px;
      border-radius: 999px;
      background: #687386;
    }
    .tag.good {
      border-color: rgba(98, 212, 139, .35);
      background: rgba(98, 212, 139, .12);
    }
    .tag.good::before { background: var(--green); }
    .tag.warn {
      border-color: rgba(242, 191, 94, .4);
      background: rgba(242, 191, 94, .12);
    }
    .tag.warn::before { background: #f2bf5e; }
    .cards {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
      margin-bottom: 16px;
    }
    .metric {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 11px;
      min-height: 86px;
      background: var(--panel-soft);
      display: grid;
      gap: 6px;
    }
    .metric .label { color: var(--muted); font-size: 12px; font-weight: 700; }
    .metric .value { font-size: 23px; font-weight: 850; line-height: 1.1; overflow-wrap: anywhere; }
    .metric .note { color: var(--muted); font-size: 12px; }
    .compare-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 12px;
      margin-bottom: 14px;
    }
    .mode-card {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel-soft);
      padding: 12px;
      display: grid;
      gap: 10px;
    }
    .mode-card.fixed, .mode-card.static_fixed { border-color: rgba(244, 81, 64, .7); }
    .mode-card.sumo_actuated { border-color: rgba(139, 92, 246, .72); }
    .mode-card.local { border-color: rgba(255, 182, 49, .65); }
    .mode-card.flowmind { border-color: rgba(61, 187, 175, .72); }
    .mode-card h3 {
      margin: 0;
      font-size: 15px;
      letter-spacing: 0;
      text-transform: capitalize;
    }
    .mode-card.fixed h3, .mode-card.static_fixed h3 { color: #f45140; }
    .mode-card.sumo_actuated h3 { color: #a78bfa; }
    .mode-card.local h3 { color: #ffb631; }
    .mode-card.flowmind h3 { color: #3dbbaf; }
    .mode-stat {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      padding-top: 8px;
      border-top: 1px solid #30394d;
      color: var(--muted);
      font-size: 13px;
    }
    .mode-stat strong { color: var(--ink); font-weight: 820; text-align: right; }
    .delta {
      display: inline-flex;
      align-items: center;
      min-width: 70px;
      justify-content: center;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 3px 8px;
      color: var(--muted);
      background: #151b26;
      font-size: 12px;
      font-weight: 800;
      white-space: nowrap;
    }
    .delta.good {
      color: #bff3cf;
      border-color: rgba(98, 212, 139, .35);
      background: rgba(98, 212, 139, .12);
    }
    .delta.bad {
      color: #ffbbb4;
      border-color: rgba(244, 81, 64, .35);
      background: rgba(244, 81, 64, .12);
    }
    .winner {
      color: #bff3cf;
      font-weight: 850;
    }
    .section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      overflow: hidden;
      margin-bottom: 16px;
    }
    .section-header {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
      background: var(--panel-head);
    }
    .section-body { padding: 14px; overflow: auto; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { padding: 9px 8px; border-bottom: 1px solid #30394d; text-align: left; vertical-align: middle; }
    th { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0; }
    td.value-cell {
      font-weight: 780;
      white-space: nowrap;
    }
    td.compare-cell {
      min-width: 132px;
    }
    .cell-note {
      display: block;
      margin-top: 3px;
      color: var(--muted);
      font-size: 11px;
      font-weight: 650;
    }
    .empty { color: var(--muted); padding: 18px; border: 1px dashed var(--line); border-radius: 8px; background: var(--panel-soft); }
    @media (max-width: 900px) {
      .topbar { grid-template-columns: 1fr; }
      .cards { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .compare-grid { grid-template-columns: 1fr; }
    }
    @media (max-width: 620px) {
      .page { padding: 12px; }
      h1 { font-size: 23px; }
      .cards { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <main class="page">
    <header class="topbar">
      <div>
        <div class="brand">
          <div class="mark" aria-hidden="true"></div>
          <div>
            <h1>FlowMind Averages</h1>
            <p class="subtitle">Порівняння static fixed, SUMO actuated, local і FlowMind по збережених симуляціях.</p>
          </div>
        </div>
        <nav class="nav" aria-label="Averages navigation">
          <a href="/">Live</a>
          <a href="/archive">Архів</a>
          <a href="/averages">Середні</a>
        </nav>
      </div>
      <div class="status-line">
        <span class="tag good">Metrics</span>
        <span class="tag" id="totalTag">0 результатів</span>
      </div>
    </header>

    <section class="cards" id="overview"></section>
    <div id="comparison"></div>
    <div id="modeSections"></div>
  </main>

  <script>
    const $ = (id) => document.getElementById(id);
    function number(value) {
      const parsed = Number(value);
      return Number.isFinite(parsed) ? parsed : null;
    }
    function fmt(value, suffix = "", digits = 1) {
      const parsed = number(value);
      if (parsed === null) return "немає";
      const text = Math.abs(parsed - Math.round(parsed)) < 0.001 ? String(Math.round(parsed)) : parsed.toFixed(digits);
      return `${text}${suffix}`;
    }
    function card(label, value, note = "") {
      return `<article class="metric"><div class="label">${label}</div><div class="value">${value}</div><div class="note">${note}</div></article>`;
    }
    async function api(path) {
      const response = await fetch(path);
      if (!response.ok) throw new Error(await response.text());
      return await response.json();
    }
    const modeOrder = ["static_fixed", "sumo_actuated", "local", "flowmind", "fixed"];
    const modeLabels = {
      static_fixed: "Static Fixed",
      sumo_actuated: "SUMO Actuated",
      local: "Local adaptive",
      flowmind: "FlowMind",
      fixed: "Fixed (legacy)",
    };
    const keyMetrics = [
      { key: "average_waiting_time", label: "Сер. очікування", suffix: " с", lower: true },
      { key: "average_queue_length", label: "Сер. черга", suffix: " авто", lower: true },
      { key: "max_queue_length", label: "Макс. черга", suffix: " авто", lower: true },
      { key: "throughput", label: "Пропуск", suffix: " авто", lower: false, digits: 0 },
      { key: "stops_count", label: "Зупинки", suffix: "", lower: true, digits: 0 },
      { key: "gridlock_risk", label: "Gridlock risk", suffix: "", lower: true, digits: 4 },
    ];
    function modeClass(mode) {
      return modeOrder.includes(mode) ? mode : "";
    }
    function byMode(modes) {
      return Object.fromEntries((modes || []).map(item => [item.mode, item]));
    }
    function metricValue(mode, key) {
      return number(mode?.metrics?.[key]?.average);
    }
    function metricCount(mode, key) {
      return mode?.metrics?.[key]?.count || 0;
    }
    function orderedModes(modes) {
      const known = modeOrder.map(mode => modes.find(item => item.mode === mode)).filter(Boolean);
      const rest = modes.filter(item => !modeOrder.includes(item.mode)).sort((a, b) => String(a.mode).localeCompare(String(b.mode)));
      return [...known, ...rest];
    }
    function bestModeFor(modes, metric) {
      const candidates = modes
        .map(mode => ({ mode, value: metricValue(mode, metric.key) }))
        .filter(item => item.value !== null);
      if (!candidates.length) return null;
      candidates.sort((a, b) => metric.lower ? a.value - b.value : b.value - a.value);
      return candidates[0].mode.mode;
    }
    function deltaFromFixed(fixedMode, mode, metric) {
      const fixedValue = metricValue(fixedMode, metric.key);
      const value = metricValue(mode, metric.key);
      if (fixedValue === null || value === null || fixedValue === 0) return null;
      if (mode?.mode === fixedMode?.mode) return { label: "baseline", kind: "" };
      const delta = metric.lower
        ? ((fixedValue - value) / fixedValue) * 100
        : ((value - fixedValue) / fixedValue) * 100;
      return {
        label: `${delta >= 0 ? "+" : ""}${fmt(delta, "%")}`,
        kind: Math.abs(delta) < 0.05 ? "" : delta > 0 ? "good" : "bad",
      };
    }
    function deltaPill(delta) {
      if (!delta) return `<span class="delta">немає</span>`;
      return `<span class="delta ${delta.kind}">${delta.label}</span>`;
    }
    function renderModeCard(mode, fixedMode, winners) {
      const stats = keyMetrics.slice(0, 4).map(metric => {
        const value = metricValue(mode, metric.key);
        const winner = winners[metric.key] === mode.mode;
        return `<div class="mode-stat">
          <span>${metric.label}</span>
          <strong class="${winner ? "winner" : ""}">${fmt(value, metric.suffix, metric.digits ?? 1)}</strong>
        </div>`;
      }).join("");
      const deltas = mode.mode === fixedMode?.mode
        ? `<span class="tag">baseline</span>`
        : deltaPill(deltaFromFixed(fixedMode, mode, keyMetrics[0]));
      return `<article class="mode-card ${modeClass(mode.mode)}">
        <div class="section-header" style="padding:0;border:0;background:transparent;">
          <h3>${modeLabels[mode.mode] || mode.mode}</h3>
          <span class="tag">${mode.count || 0} запусків</span>
        </div>
        ${stats}
        <div class="mode-stat"><span>Очікування vs static fixed</span><strong>${deltas}</strong></div>
      </article>`;
    }
    function renderComparison(modes) {
      const ordered = orderedModes(modes);
      const map = byMode(ordered);
      const fixedMode = map.static_fixed || map.fixed || null;
      const comparable = ordered.filter(mode => !(
        mode.mode === "fixed" && map.static_fixed
      ));
      const comparisonModes = [
        fixedMode?.mode || "static_fixed",
        "sumo_actuated",
        "local",
        "flowmind",
      ];
      const winners = Object.fromEntries(keyMetrics.map(metric => [metric.key, bestModeFor(comparable, metric)]));
      const cards = comparable.length
        ? `<div class="compare-grid">${comparable.map(mode => renderModeCard(mode, fixedMode, winners)).join("")}</div>`
        : "";
      const rows = keyMetrics.map(metric => {
        const best = winners[metric.key];
        const cells = comparisonModes.map(modeName => {
          const mode = map[modeName];
          const value = metricValue(mode, metric.key);
          const delta = deltaFromFixed(fixedMode, mode, metric);
          return `<td class="compare-cell ${best === modeName ? "winner" : ""}">
            <span class="value-cell">${fmt(value, metric.suffix, metric.digits ?? 1)}</span>
            <span class="cell-note">${mode ? `${metricCount(mode, metric.key)} значень` : "немає режиму"} ${modeName !== fixedMode?.mode ? deltaPill(delta) : ""}</span>
          </td>`;
        }).join("");
        return `<tr>
          <td>${metric.label}</td>
          ${cells}
          <td>${best ? modeLabels[best] || best : "немає"}</td>
        </tr>`;
      }).join("");
      return `<section class="section">
        <div class="section-header">
          <h2>Порівняння режимів</h2>
          <span class="tag good">Static Fixed = baseline</span>
        </div>
        <div class="section-body">
          ${cards}
          <table>
            <thead>
              <tr><th>Метрика</th>${comparisonModes.map(mode => `<th>${modeLabels[mode] || mode}</th>`).join("")}<th>Краще</th></tr>
            </thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
      </section>`;
    }
    function renderMode(mode) {
      const metrics = mode.metrics || {};
      const rows = Object.entries(metrics).map(([key, item]) => {
        return `<tr>
          <td>${item.label || key}</td>
          <td>${fmt(item.average)}</td>
          <td>${fmt(item.min)}</td>
          <td>${fmt(item.max)}</td>
          <td>${item.count || 0}</td>
        </tr>`;
      }).join("");
      return `<section class="section">
        <div class="section-header">
          <h2>${mode.mode}</h2>
          <span class="tag">${mode.count} запусків</span>
        </div>
        <div class="section-body">
          <table>
            <thead>
              <tr><th>Метрика</th><th>Середнє</th><th>Мін</th><th>Макс</th><th>К-сть</th></tr>
            </thead>
            <tbody>${rows || `<tr><td colspan="5"><div class="empty">Немає числових метрик.</div></td></tr>`}</tbody>
          </table>
        </div>
      </section>`;
    }
    async function loadAverages() {
      const payload = await api("/api/averages");
      $("totalTag").textContent = `${payload.total_results || 0} результатів`;
      const modes = orderedModes(payload.modes || []);
      const flowmind = modes.find(item => item.mode === "flowmind") || { metrics: {} };
      const fixed = modes.find(item => item.mode === "static_fixed")
        || modes.find(item => item.mode === "fixed")
        || { metrics: {} };
      const local = modes.find(item => item.mode === "local") || { metrics: {} };
      const wait = flowmind.metrics?.average_waiting_time?.average;
      const fixedWait = fixed.metrics?.average_waiting_time?.average;
      const localWait = local.metrics?.average_waiting_time?.average;
      const improvement = fixedWait && wait ? ((fixedWait - wait) / fixedWait) * 100 : null;
      const localImprovement = fixedWait && localWait ? ((fixedWait - localWait) / fixedWait) * 100 : null;
      $("overview").innerHTML = [
        card("Усього результатів", fmt(payload.total_results, "", 0), `${payload.total_rows || 0} summary rows`),
        card("Режимів", fmt(modes.length, "", 0), modes.map(item => item.mode).join(", ")),
        card("FlowMind vs static fixed", improvement == null ? "немає" : fmt(improvement, "%"), `очікування: ${fmt(wait, " с")}`),
        card("Local vs static fixed", localImprovement == null ? "немає" : fmt(localImprovement, "%"), `очікування: ${fmt(localWait, " с")}`),
      ].join("");
      $("comparison").innerHTML = modes.length ? renderComparison(modes) : `<div class="empty">Немає summary.csv для агрегації.</div>`;
      $("modeSections").innerHTML = modes.length ? `<section class="section">
        <div class="section-header">
          <h2>Деталізація середніх</h2>
          <span class="tag">усі числові метрики</span>
        </div>
      </section>${modes.map(renderMode).join("")}` : "";
    }
    loadAverages();
  </script>
</body>
</html>
"""


class MissingFastAPIApp:
    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        body = (
            "FastAPI is not installed. Install requirements.txt and run: "
            "uvicorn api.web_dashboard:app --host 0.0.0.0 --port 8000"
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 500,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


manager = DemoProcessManager()

if FastAPI is None:
    app = MissingFastAPIApp()
else:
    app = FastAPI(title="FlowMind Web Dashboard", version="0.1.0")

    @app.get("/", response_class=HTMLResponse)
    def dashboard_page() -> Any:
        return DESIGN_PAGE

    @app.get("/archive", response_class=HTMLResponse)
    def archive_page() -> Any:
        return ARCHIVE_PAGE

    @app.get("/averages", response_class=HTMLResponse)
    def averages_page() -> Any:
        return AVERAGES_PAGE

    @app.get("/assets/icon_car.png")
    def car_icon() -> Any:
        return FileResponse(CAR_ICON_PATH, media_type="image/png")

    @app.get("/assets/zone-simulation.css")
    def zone_simulation_css() -> Any:
        return FileResponse(ZONE_SIMULATION_CSS_PATH, media_type="text/css")

    @app.get("/assets/zone-simulation.js")
    def zone_simulation_js() -> Any:
        return FileResponse(
            ZONE_SIMULATION_JS_PATH,
            media_type="application/javascript",
        )

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "project_root": str(PROJECT_ROOT),
            "results_dir": str(RESULTS_DIR),
        }

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        return build_status_payload(manager)

    @app.get("/api/process")
    def process_status() -> dict[str, Any]:
        return manager.snapshot()

    @app.get("/api/results")
    def results() -> list[dict[str, Any]]:
        return discover_result_sets(RESULTS_DIR)

    @app.get("/api/archive")
    def archive() -> dict[str, Any]:
        return build_archive_payload(RESULTS_DIR)

    @app.get("/api/archive/{result_id}")
    def archived_result(result_id: str) -> dict[str, Any]:
        return build_result_detail_payload(result_id)

    @app.get("/api/averages")
    def averages() -> dict[str, Any]:
        return build_averages_payload(RESULTS_DIR)

    @app.post("/api/gemini-summary")
    async def gemini_summary(request: Request) -> dict[str, Any]:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        result_id = payload.get("result_id")
        if not isinstance(result_id, str) or not result_id:
            result_id = None
        force = _bool_option(payload, "force", False)
        return build_ai_report_payload(manager, result_id=result_id, force=force)

    @app.post("/api/start-demo")
    async def start_demo(request: Request) -> dict[str, Any]:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        return manager.start(payload)

    @app.post("/api/stop")
    def stop_demo() -> dict[str, Any]:
        return manager.stop()
