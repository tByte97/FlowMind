from __future__ import annotations

import csv
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
    from fastapi.responses import HTMLResponse
except ImportError:
    FastAPI = None  # type: ignore[assignment]
    Request = None  # type: ignore[assignment]
    HTMLResponse = None  # type: ignore[assignment]


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_RESULTS_DIR = PROJECT_ROOT / "results"
RESULTS_DIR = Path(os.environ.get("FLOWMIND_RESULTS_DIR", PROJECT_RESULTS_DIR))
WEB_RESULTS_DIR = Path(
    os.environ.get("FLOWMIND_WEB_RESULTS_DIR", PROJECT_RESULTS_DIR / "web_demo")
)
MAX_HISTORY_POINTS = 180
MAX_DECISION_ROWS = 80
MAX_TABLE_ROWS = 120


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
    if WEB_RESULTS_DIR.exists() and WEB_RESULTS_DIR != base_dir:
        markers.extend(WEB_RESULTS_DIR.glob("**/live_status.json"))
        markers.extend(WEB_RESULTS_DIR.glob("**/summary.csv"))

    result_dirs: dict[Path, dict[str, Any]] = {}
    for marker in markers:
        result_dir = marker.parent.resolve()
        entry = result_dirs.setdefault(
            result_dir,
            {
                "path": _display_path(result_dir),
                "updated_at": 0.0,
                "has_live_status": False,
                "has_summary": False,
                "mode": None,
            },
        )
        entry["updated_at"] = max(entry["updated_at"], _safe_stat_mtime(marker))
        if marker.name == "live_status.json":
            entry["has_live_status"] = True
            payload = _read_json(marker)
            entry["mode"] = payload.get("mode") or entry["mode"]
        if marker.name == "summary.csv":
            entry["has_summary"] = True
            entry["modes"] = _summary_modes(marker)

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
    payload["_meta"] = {
        "source": _display_path(path),
        "updated_at": datetime.fromtimestamp(
            _safe_stat_mtime(path), tz=timezone.utc
        ).isoformat(),
    }
    return payload


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
            return self.snapshot_unlocked()

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
        <span class="check"><input id="baseline" type="checkbox"> fixed + FlowMind</span>
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
    window.addEventListener("resize", () => refresh());
    refresh();
    setInterval(refresh, pollMs);
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
        return HTML_PAGE

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
