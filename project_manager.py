from __future__ import annotations

import argparse
import os
import queue
import shutil
import signal
import socket
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parent
SIMULATION_ROOT = PROJECT_ROOT / "simulation"
DEFAULT_SCENARIO_NAME = "rivne_area"
SCENARIO_DIR = SIMULATION_ROOT / DEFAULT_SCENARIO_NAME
DEFAULT_RESULTS = PROJECT_ROOT / "results"

MODE_SCRIPTS = {
    "Fixed": PROJECT_ROOT / "experiments" / "run_fixed.py",
    "Local Adaptive": PROJECT_ROOT / "experiments" / "run_local_adaptive.py",
    "FlowMind": PROJECT_ROOT / "experiments" / "run_flowmind.py",
}


@dataclass(frozen=True)
class ScenarioProfile:
    name: str
    directory: Path
    config_path: Path
    zone_path: Path
    emergency_path: Path
    net_path: Path
    results_path: Path

    @property
    def label(self) -> str:
        return f"{self.name} — {display_path(self.directory)}"

    @property
    def is_ready(self) -> bool:
        return (
            self.directory.is_dir()
            and self.config_path.is_file()
            and self.zone_path.is_file()
            and self.net_path.is_file()
        )


def display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path.resolve())


def _first_existing(paths: Sequence[Path]) -> Path:
    for path in paths:
        if path.exists():
            return path
    return paths[0]


def scenario_profile_from_directory(directory: Path) -> ScenarioProfile:
    directory = directory.expanduser().resolve()
    name = directory.name
    config_path = _first_existing(
        (
            directory / "focused.sumocfg",
            directory / "osm.sumocfg",
            *sorted(directory.glob("*.sumocfg")),
        )
    )
    return ScenarioProfile(
        name=name,
        directory=directory,
        config_path=config_path,
        zone_path=_first_existing(
            (
                directory / "central_zone.json",
                directory / "zone.json",
                *sorted(directory.glob("*zone*.json")),
            )
        ),
        emergency_path=_first_existing(
            (
                directory / "emergency.json",
                *sorted(directory.glob("*emergency*.json")),
            )
        ),
        net_path=_first_existing(
            (
                directory / "osm.net.xml.gz",
                directory / "osm.net.xml",
                *sorted(directory.glob("*.net.xml.gz")),
                *sorted(directory.glob("*.net.xml")),
            )
        ),
        results_path=DEFAULT_RESULTS / name,
    )


def discover_scenarios(
    simulation_root: Path = SIMULATION_ROOT,
) -> tuple[ScenarioProfile, ...]:
    if not simulation_root.exists():
        return ()
    profiles = [
        scenario_profile_from_directory(path)
        for path in sorted(simulation_root.iterdir(), key=lambda item: item.name)
        if path.is_dir()
        and any(
            candidate.exists()
            for candidate in (
                path / "focused.sumocfg",
                path / "osm.sumocfg",
                path / "osm.net.xml.gz",
                path / "osm.net.xml",
            )
        )
    ]
    return tuple(
        sorted(
            profiles,
            key=lambda item: (
                item.name != DEFAULT_SCENARIO_NAME,
                not item.is_ready,
                item.name,
            ),
        )
    )


def default_scenario() -> ScenarioProfile:
    discovered = discover_scenarios()
    if discovered:
        return discovered[0]
    return scenario_profile_from_directory(SCENARIO_DIR)


def project_python(project_root: Path = PROJECT_ROOT) -> Path:
    """Prefer the project's virtual environment without requiring activation."""
    candidates = (
        project_root / ".venv" / "Scripts" / "python.exe",
        project_root / ".venv" / "bin" / "python",
    )
    for candidate in candidates:
        if candidate.is_file():
            # Do not resolve this symlink: the .venv path is how Python detects
            # and activates the virtual environment.
            return candidate.absolute()
    return Path(sys.executable).resolve()


def command_text(command: Sequence[str | os.PathLike[str]]) -> str:
    return subprocess.list2cmdline([os.fspath(part) for part in command])


def project_environment(python: Path) -> dict[str, str]:
    """Prepare SUMO variables for a child process when eclipse-sumo is installed."""
    environment = os.environ.copy()
    environment["PYTHONUNBUFFERED"] = "1"
    try:
        result = subprocess.run(
            [
                os.fspath(python),
                "-c",
                "import sumo; print(sumo.SUMO_HOME)",
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return environment

    sumo_home = result.stdout.strip()
    if result.returncode == 0 and sumo_home:
        environment["SUMO_HOME"] = sumo_home
        environment["PATH"] = os.pathsep.join(
            [str(Path(sumo_home) / "bin"), environment.get("PATH", "")]
        )
    return environment


def diagnose(python: Path | None = None) -> int:
    python = python or project_python()
    scenario = default_scenario()
    print(f"Project: {PROJECT_ROOT}")
    print(f"Python:  {python}")
    print(f"Scenario: {scenario.name} ({scenario.directory})")

    missing_files = [
        path
        for path in (
            PROJECT_ROOT / "requirements.txt",
            scenario.config_path,
            scenario.zone_path,
            scenario.net_path,
        )
        if not path.exists()
    ]
    for path in missing_files:
        print(f"[FAIL] Missing file: {path.relative_to(PROJECT_ROOT)}")

    modules = ("sumo", "sumolib", "traci", "streamlit", "pandas", "plotly")
    probe = (
        "import importlib.util\n"
        f"names = {modules!r}\n"
        "print('\\n'.join("
        "f'{name}:OK' if importlib.util.find_spec(name) else f'{name}:MISSING' "
        "for name in names))"
    )
    try:
        result = subprocess.run(
            [os.fspath(python), "-c", probe],
            cwd=PROJECT_ROOT,
            env=project_environment(python),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        print(f"[FAIL] Could not run Python: {error}")
        return 1

    missing_modules = []
    for line in result.stdout.splitlines():
        name, _, status = line.partition(":")
        print(f"[{'OK' if status == 'OK' else 'FAIL'}] Python module: {name}")
        if status != "OK":
            missing_modules.append(name)
    if result.stderr.strip():
        print(result.stderr.strip())

    environment = project_environment(python)
    sumo_binary = shutil.which("sumo", path=environment.get("PATH"))
    sumo_gui_binary = shutil.which("sumo-gui", path=environment.get("PATH"))
    print(f"[{'OK' if sumo_binary else 'FAIL'}] SUMO: {sumo_binary or 'not found'}")
    print(
        f"[{'OK' if sumo_gui_binary else 'FAIL'}] "
        f"SUMO GUI: {sumo_gui_binary or 'not found'}"
    )

    failed = bool(
        missing_files
        or missing_modules
        or result.returncode != 0
        or not sumo_binary
        or not sumo_gui_binary
    )
    print("\nEnvironment is ready." if not failed else "\nEnvironment needs attention.")
    return int(failed)


def positive_int(value: str, field: str, minimum: int = 1) -> int:
    try:
        result = int(value)
    except ValueError as error:
        raise ValueError(f'"{field}" має бути цілим числом.') from error
    if result < minimum:
        raise ValueError(f'"{field}" має бути не менше {minimum}.')
    return result


def find_free_port(preferred: int, attempts: int = 50) -> int:
    for port in range(preferred, preferred + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise ValueError(
        f"Не знайдено вільний порт у діапазоні "
        f"{preferred}–{preferred + attempts - 1}."
    )


def run_gui() -> bool:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    class FlowMindManager:
        def __init__(self, root: tk.Tk) -> None:
            self.root = root
            self.python = project_python()
            self.events: queue.Queue[tuple[str, object]] = queue.Queue()
            self.process: subprocess.Popen[str] | None = None
            self.process_lock = threading.Lock()
            self.busy = False
            self.stopping = False
            self.cancel_start = False
            self.after_success: tuple[list[str], str, dict[str, str]] | None = None
            self.scenarios = discover_scenarios()
            self.current_scenario = default_scenario()

            self.root.title("FlowMind — керування проєктом")
            self.root.geometry("1040x760")
            self.root.minsize(850, 650)
            self.root.protocol("WM_DELETE_WINDOW", self.on_close)

            self.scenario_choice = tk.StringVar(value=self.current_scenario.label)
            self.mode = tk.StringVar(value="FlowMind")
            self.duration = tk.StringVar(value="900")
            self.seed = tk.StringVar(value="42")
            self.zone_size = tk.StringVar(value="6")
            self.gui_enabled = tk.BooleanVar(value=True)
            self.emergency_enabled = tk.BooleanVar(value=False)
            self.emergency_depart = tk.StringVar(value="180")
            self.gui_delay = tk.StringVar(value="75")
            self.dashboard_port = tk.StringVar(value="8501")
            self.config_path = tk.StringVar(value=str(self.current_scenario.config_path))
            self.zone_path = tk.StringVar(value=str(self.current_scenario.zone_path))
            self.emergency_config_path = tk.StringVar(
                value=str(self.current_scenario.emergency_path)
            )
            self.results_path = tk.StringVar(value=str(self.current_scenario.results_path))

            self.traffic_duration = tk.StringVar(value="1800")
            self.vehicles_per_hour = tk.StringVar(value="2400")
            self.route_count = tk.StringVar(value="8")

            self.status = tk.StringVar(value="Готово")
            self._build_ui()
            self.root.after(100, self._drain_events)

        def _build_ui(self) -> None:
            style = ttk.Style()
            if "clam" in style.theme_names():
                style.theme_use("clam")
            style.configure("Title.TLabel", font=("TkDefaultFont", 16, "bold"))
            style.configure("Status.TLabel", padding=(8, 5))

            outer = ttk.Frame(self.root, padding=12)
            outer.pack(fill="both", expand=True)

            header = ttk.Frame(outer)
            header.pack(fill="x", pady=(0, 10))
            ttk.Label(header, text="FlowMind Project Manager", style="Title.TLabel").pack(
                side="left"
            )
            ttk.Label(
                header,
                text=f"Python: {self.python}",
                foreground="#555555",
            ).pack(side="right")

            notebook = ttk.Notebook(outer)
            notebook.pack(fill="x")
            launch_tab = ttk.Frame(notebook, padding=12)
            tools_tab = ttk.Frame(notebook, padding=12)
            notebook.add(launch_tab, text="Експерименти")
            notebook.add(tools_tab, text="Сценарій та система")
            self._build_launch_tab(launch_tab)
            self._build_tools_tab(tools_tab)

            log_header = ttk.Frame(outer)
            log_header.pack(fill="x", pady=(12, 4))
            ttk.Label(log_header, text="Журнал виконання").pack(side="left")
            ttk.Button(log_header, text="Очистити", command=self.clear_log).pack(
                side="right"
            )

            log_frame = ttk.Frame(outer)
            log_frame.pack(fill="both", expand=True)
            self.log = tk.Text(
                log_frame,
                height=15,
                wrap="word",
                font=("TkFixedFont", 10),
                state="disabled",
            )
            scrollbar = ttk.Scrollbar(
                log_frame, orient="vertical", command=self.log.yview
            )
            self.log.configure(yscrollcommand=scrollbar.set)
            self.log.pack(side="left", fill="both", expand=True)
            scrollbar.pack(side="right", fill="y")

            footer = ttk.Frame(outer)
            footer.pack(fill="x", pady=(8, 0))
            ttk.Label(footer, textvariable=self.status, style="Status.TLabel").pack(
                side="left"
            )
            self.stop_button = ttk.Button(
                footer,
                text="Зупинити процес",
                command=self.stop_process,
                state="disabled",
            )
            self.stop_button.pack(side="right")

        def _build_launch_tab(self, parent: ttk.Frame) -> None:
            for column in range(6):
                parent.columnconfigure(column, weight=1 if column in (1, 3, 5) else 0)

            ttk.Label(parent, text="Карта / сценарій").grid(row=0, column=0, sticky="w")
            self.scenario_combobox = ttk.Combobox(
                parent,
                textvariable=self.scenario_choice,
                values=self._scenario_labels(),
                state="readonly",
            )
            self.scenario_combobox.grid(
                row=0,
                column=1,
                columnspan=3,
                sticky="ew",
                padx=(6, 14),
                pady=4,
            )
            self.scenario_combobox.bind(
                "<<ComboboxSelected>>", lambda _event: self.select_scenario()
            )
            scenario_buttons = ttk.Frame(parent)
            scenario_buttons.grid(row=0, column=4, columnspan=2, sticky="ew")
            ttk.Button(
                scenario_buttons, text="Оновити", command=self.refresh_scenarios
            ).pack(side="left", padx=(0, 6))
            ttk.Button(
                scenario_buttons,
                text="Обрати папку…",
                command=self.choose_scenario_directory,
            ).pack(side="left")

            ttk.Label(parent, text="Режим").grid(row=1, column=0, sticky="w")
            ttk.Combobox(
                parent,
                textvariable=self.mode,
                values=tuple(MODE_SCRIPTS),
                state="readonly",
                width=20,
            ).grid(row=1, column=1, sticky="ew", padx=(6, 14))
            self._entry(parent, "Тривалість, с", self.duration, 1, 2)
            self._entry(parent, "Seed", self.seed, 1, 4)

            self._entry(parent, "Розмір зони", self.zone_size, 2, 0)
            self._entry(parent, "GUI delay, мс", self.gui_delay, 2, 2)
            self._entry(parent, "Порт dashboard", self.dashboard_port, 2, 4)

            ttk.Checkbutton(
                parent, text="Відкрити SUMO GUI", variable=self.gui_enabled
            ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))
            ttk.Checkbutton(
                parent,
                text="Створити швидку",
                variable=self.emergency_enabled,
            ).grid(row=3, column=2, sticky="w", pady=(8, 0))
            ttk.Label(parent, text="Виїзд швидкої, с").grid(
                row=3, column=3, sticky="e", pady=(8, 0)
            )
            ttk.Entry(parent, textvariable=self.emergency_depart, width=10).grid(
                row=3, column=4, sticky="ew", padx=(6, 14), pady=(8, 0)
            )

            self._path_row(parent, "SUMO config", self.config_path, 4, "file")
            self._path_row(parent, "Конфігурація зони", self.zone_path, 5, "file")
            self._path_row(
                parent, "Швидка / emergency", self.emergency_config_path, 6, "file"
            )
            self._path_row(parent, "Результати", self.results_path, 7, "directory")

            actions = ttk.Frame(parent)
            actions.grid(row=8, column=0, columnspan=6, sticky="ew", pady=(12, 0))
            ttk.Button(
                actions, text="Запустити режим", command=self.run_experiment
            ).pack(side="left", padx=(0, 7))
            ttk.Button(
                actions, text="Порівняти 3 режими", command=self.run_comparison
            ).pack(side="left", padx=7)
            ttk.Button(
                actions, text="Demo зі швидкою", command=self.run_demo
            ).pack(side="left", padx=7)
            ttk.Button(
                actions, text="Відкрити dashboard", command=self.run_dashboard
            ).pack(side="left", padx=7)

        def _build_tools_tab(self, parent: ttk.Frame) -> None:
            for column in range(6):
                parent.columnconfigure(column, weight=1 if column in (1, 3, 5) else 0)

            self._entry(parent, "Тривалість трафіку, с", self.traffic_duration, 0, 0)
            self._entry(parent, "Авто/год", self.vehicles_per_hour, 0, 2)
            self._entry(parent, "Кількість маршрутів", self.route_count, 0, 4)

            scenario_actions = ttk.Frame(parent)
            scenario_actions.grid(
                row=1, column=0, columnspan=6, sticky="ew", pady=(12, 16)
            )
            ttk.Button(
                scenario_actions,
                text="Згенерувати трафік",
                command=self.generate_traffic,
            ).pack(side="left", padx=(0, 7))
            ttk.Button(
                scenario_actions,
                text="Відкрити сценарій у SUMO",
                command=self.open_sumo,
            ).pack(side="left", padx=7)
            ttk.Button(
                scenario_actions,
                text="Інфо по карті",
                command=self.show_scenario_info,
            ).pack(side="left", padx=7)

            ttk.Separator(parent).grid(
                row=2, column=0, columnspan=6, sticky="ew", pady=(0, 14)
            )
            system_actions = ttk.Frame(parent)
            system_actions.grid(row=3, column=0, columnspan=6, sticky="ew")
            ttk.Button(
                system_actions,
                text="Перевірити середовище",
                command=self.check_environment,
            ).pack(side="left", padx=(0, 7))
            ttk.Button(
                system_actions, text="Запустити тести", command=self.run_tests
            ).pack(side="left", padx=7)
            ttk.Button(
                system_actions,
                text="Встановити / оновити залежності",
                command=self.install_dependencies,
            ).pack(side="left", padx=7)

            ttk.Label(
                parent,
                text=(
                    "Тула запускає наявні скрипти окремими процесами. "
                    "Для нової карти достатньо вибрати її папку або покласти її "
                    "в simulation/<назва>; manager підхопить SUMO config, "
                    "central_zone.json, emergency.json і папку результатів."
                ),
                foreground="#555555",
                wraplength=820,
            ).grid(row=4, column=0, columnspan=6, sticky="w", pady=(18, 0))

        def _scenario_labels(self) -> tuple[str, ...]:
            return tuple(profile.label for profile in self.scenarios)

        def _profile_by_label(self, label: str) -> ScenarioProfile | None:
            for profile in self.scenarios:
                if profile.label == label:
                    return profile
            return None

        def _apply_scenario(self, profile: ScenarioProfile) -> None:
            self.current_scenario = profile
            self.scenario_choice.set(profile.label)
            self.config_path.set(str(profile.config_path))
            self.zone_path.set(str(profile.zone_path))
            self.emergency_config_path.set(str(profile.emergency_path))
            self.results_path.set(str(profile.results_path))
            if not profile.is_ready:
                self._append_log(
                    "\n[manager] Увага: сценарій ще не повний. "
                    "Потрібні SUMO config, central_zone.json і net-файл.\n"
                )

        def select_scenario(self) -> None:
            profile = self._profile_by_label(self.scenario_choice.get())
            if profile is not None:
                self._apply_scenario(profile)

        def refresh_scenarios(self) -> None:
            selected_directory = self.current_scenario.directory
            self.scenarios = discover_scenarios()
            if hasattr(self, "scenario_combobox"):
                self.scenario_combobox.configure(values=self._scenario_labels())
            selected = next(
                (
                    profile
                    for profile in self.scenarios
                    if profile.directory == selected_directory
                ),
                self.scenarios[0] if self.scenarios else default_scenario(),
            )
            self._apply_scenario(selected)
            self._append_log("\n[manager] Список карт оновлено.\n")

        def choose_scenario_directory(self) -> None:
            selected = filedialog.askdirectory(initialdir=SIMULATION_ROOT)
            if not selected:
                return
            profile = scenario_profile_from_directory(Path(selected))
            known = [item for item in self.scenarios if item.directory != profile.directory]
            self.scenarios = tuple(sorted([*known, profile], key=lambda item: item.name))
            self.scenario_combobox.configure(values=self._scenario_labels())
            self._apply_scenario(profile)
            self._append_log(
                f"\n[manager] Обрано карту: {profile.directory}\n"
            )

        @staticmethod
        def _scenario_directory_from_config(config: Path) -> Path:
            return config.expanduser().resolve().parent

        def _scenario_net_path(self) -> Path:
            directory = self._scenario_directory_from_config(Path(self.config_path.get()))
            return scenario_profile_from_directory(directory).net_path

        def _scenario_output_dir(self) -> Path:
            return self._scenario_directory_from_config(Path(self.config_path.get()))

        def _traffic_generation_paths(self) -> tuple[Path, Path, Path]:
            net_path = self._scenario_net_path()
            zone_path = Path(self.zone_path.get()).expanduser()
            output_dir = self._scenario_output_dir()
            if not net_path.is_file():
                raise ValueError(f"Не знайдено SUMO net-файл: {net_path}")
            if not zone_path.is_file():
                raise ValueError(f"Не знайдено конфігурацію зони: {zone_path}")
            return net_path, zone_path, output_dir

        def _validate_emergency_config(self) -> Path:
            emergency_path = Path(self.emergency_config_path.get()).expanduser()
            if not emergency_path.is_file():
                raise ValueError(f"Не знайдено emergency config: {emergency_path}")
            return emergency_path

        @staticmethod
        def _describe_profile(profile: ScenarioProfile) -> str:
            parts = [
                f"Папка: {display_path(profile.directory)}",
                f"SUMO: {display_path(profile.config_path)}",
                f"Зона: {display_path(profile.zone_path)}",
                f"Net: {display_path(profile.net_path)}",
                f"Швидка: {display_path(profile.emergency_path)}",
            ]
            missing = [
                label
                for label, path in (
                    ("SUMO config", profile.config_path),
                    ("central_zone.json", profile.zone_path),
                    ("net-file", profile.net_path),
                    ("emergency.json", profile.emergency_path),
                )
                if not path.exists()
            ]
            if missing:
                parts.append("Ще треба: " + ", ".join(missing))
            return "\n".join(parts)

        def show_scenario_info(self) -> None:
            profile = scenario_profile_from_directory(
                self._scenario_directory_from_config(Path(self.config_path.get()))
            )
            messagebox.showinfo("Поточна карта", self._describe_profile(profile))

        @staticmethod
        def _entry(
            parent: ttk.Frame,
            label: str,
            variable: tk.StringVar,
            row: int,
            column: int,
        ) -> None:
            ttk.Label(parent, text=label).grid(row=row, column=column, sticky="w")
            ttk.Entry(parent, textvariable=variable, width=13).grid(
                row=row,
                column=column + 1,
                sticky="ew",
                padx=(6, 14),
                pady=4,
            )

        def _path_row(
            self,
            parent: ttk.Frame,
            label: str,
            variable: tk.StringVar,
            row: int,
            kind: str,
        ) -> None:
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w")
            ttk.Entry(parent, textvariable=variable).grid(
                row=row,
                column=1,
                columnspan=4,
                sticky="ew",
                padx=(6, 8),
                pady=4,
            )

            def choose() -> None:
                selected = (
                    filedialog.askopenfilename(initialdir=PROJECT_ROOT)
                    if kind == "file"
                    else filedialog.askdirectory(initialdir=PROJECT_ROOT)
                )
                if selected:
                    variable.set(selected)

            ttk.Button(parent, text="Обрати…", command=choose).grid(
                row=row, column=5, sticky="ew"
            )

        def _common_values(self) -> tuple[int, int, int, int, int]:
            return (
                positive_int(self.duration.get(), "Тривалість"),
                positive_int(self.seed.get(), "Seed", 0),
                positive_int(self.zone_size.get(), "Розмір зони"),
                positive_int(self.gui_delay.get(), "GUI delay", 0),
                positive_int(self.dashboard_port.get(), "Порт dashboard"),
            )

        def _experiment_paths(self) -> list[str]:
            config = Path(self.config_path.get()).expanduser()
            zone = Path(self.zone_path.get()).expanduser()
            if not config.is_file():
                raise ValueError(f"Не знайдено SUMO config: {config}")
            if not zone.is_file():
                raise ValueError(f"Не знайдено конфігурацію зони: {zone}")
            return [
                "--config",
                str(config),
                "--zone",
                str(zone),
                "--results-dir",
                str(Path(self.results_path.get()).expanduser()),
            ]

        def run_experiment(self) -> None:
            try:
                duration, seed, zone_size, gui_delay, _ = self._common_values()
                command = [
                    str(self.python),
                    "-u",
                    str(MODE_SCRIPTS[self.mode.get()]),
                    "--duration",
                    str(duration),
                    "--seed",
                    str(seed),
                    "--zone-size",
                    str(zone_size),
                    "--gui-delay",
                    str(gui_delay),
                    *self._experiment_paths(),
                ]
                if self.gui_enabled.get():
                    command.append("--gui")
                if self.emergency_enabled.get():
                    depart = positive_int(
                        self.emergency_depart.get(), "Виїзд швидкої", 0
                    )
                    if depart >= duration:
                        raise ValueError(
                            "Швидка повинна виїхати до завершення симуляції."
                        )
                    command.extend(
                        [
                            "--emergency",
                            "--emergency-config",
                            str(self._validate_emergency_config()),
                            "--emergency-depart",
                            str(depart),
                        ]
                    )
                self.start_process(command, f"Режим {self.mode.get()}")
            except ValueError as error:
                messagebox.showerror("Некоректні параметри", str(error))

        def run_comparison(self) -> None:
            try:
                duration, seed, zone_size, _, _ = self._common_values()
                command = [
                    str(self.python),
                    "-u",
                    str(PROJECT_ROOT / "experiments" / "run_comparison.py"),
                    "--duration",
                    str(duration),
                    "--seed",
                    str(seed),
                    "--zone-size",
                    str(zone_size),
                    *self._experiment_paths(),
                ]
                self.start_process(command, "Порівняння режимів")
            except ValueError as error:
                messagebox.showerror("Некоректні параметри", str(error))

        def run_demo(self) -> None:
            try:
                duration, seed, _, gui_delay, port = self._common_values()
                depart = positive_int(self.emergency_depart.get(), "Виїзд швидкої", 0)
                if depart >= duration:
                    raise ValueError(
                        "Швидка повинна виїхати до завершення симуляції."
                    )
                command = [
                    str(self.python),
                    "-u",
                    str(PROJECT_ROOT / "experiments" / "run_demo.py"),
                    "--duration",
                    str(duration),
                    "--seed",
                    str(seed),
                    "--emergency-depart",
                    str(depart),
                    "--gui-delay",
                    str(gui_delay),
                    "--dashboard-port",
                    str(port),
                    "--no-dashboard",
                    "--emergency-config",
                    str(self._validate_emergency_config()),
                    *self._experiment_paths(),
                ]
                if not self.gui_enabled.get():
                    command.append("--headless")
                (
                    dashboard_command,
                    dashboard_environment,
                    actual_port,
                ) = self._dashboard_spec(port)
                if actual_port != port:
                    self.dashboard_port.set(str(actual_port))
                    self._append_log(
                        f"\n[manager] Порт {port} зайнятий, "
                        f"після demo відкрию dashboard на {actual_port}.\n"
                    )
                self.start_process(
                    command,
                    "Demo зі швидкою",
                    after_success=(
                        dashboard_command,
                        "Dashboard demo",
                        dashboard_environment,
                    ),
                )
            except ValueError as error:
                messagebox.showerror("Некоректні параметри", str(error))

        def _dashboard_spec(self, port: int) -> tuple[list[str], dict[str, str], int]:
            actual_port = find_free_port(port)
            environment = {
                "FLOWMIND_RESULTS_DIR": str(
                    Path(self.results_path.get()).expanduser().resolve()
                )
            }
            command = [
                str(self.python),
                "-u",
                "-m",
                "streamlit",
                "run",
                str(PROJECT_ROOT / "dashboard" / "app.py"),
                "--server.port",
                str(actual_port),
            ]
            return command, environment, actual_port

        def run_dashboard(self) -> None:
            try:
                port = positive_int(self.dashboard_port.get(), "Порт dashboard")
                command, environment, actual_port = self._dashboard_spec(port)
                if actual_port != port:
                    self.dashboard_port.set(str(actual_port))
                    self._append_log(
                        f"\n[manager] Порт {port} зайнятий, "
                        f"використовую {actual_port}.\n"
                    )
                self.start_process(command, "Dashboard", environment)
            except ValueError as error:
                messagebox.showerror("Некоректні параметри", str(error))

        def generate_traffic(self) -> None:
            try:
                duration = positive_int(
                    self.traffic_duration.get(), "Тривалість трафіку"
                )
                vehicles = positive_int(self.vehicles_per_hour.get(), "Авто/год")
                routes = positive_int(self.route_count.get(), "Кількість маршрутів", 2)
                net_path, zone_path, output_dir = self._traffic_generation_paths()
                command = [
                    str(self.python),
                    "-u",
                    str(PROJECT_ROOT / "tools" / "generate_focused_traffic.py"),
                    "--net",
                    str(net_path),
                    "--zone",
                    str(zone_path),
                    "--output-dir",
                    str(output_dir),
                    "--duration",
                    str(duration),
                    "--vehicles-per-hour",
                    str(vehicles),
                    "--routes",
                    str(routes),
                ]
                self.start_process(command, "Генерація сфокусованого трафіку")
            except ValueError as error:
                messagebox.showerror("Некоректні параметри", str(error))

        def open_sumo(self) -> None:
            config = Path(self.config_path.get()).expanduser()
            if not config.is_file():
                messagebox.showerror("Файл не знайдено", str(config))
                return
            self.start_process(
                ["sumo-gui", "-c", str(config.resolve())],
                "SUMO GUI",
                cwd=config.resolve().parent,
            )

        def check_environment(self) -> None:
            self.start_process(
                [str(self.python), "-u", str(Path(__file__).resolve()), "--diagnose"],
                "Перевірка середовища",
            )

        def run_tests(self) -> None:
            self.start_process(
                [
                    str(self.python),
                    "-u",
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    "tests",
                    "-v",
                ],
                "Тести",
            )

        def install_dependencies(self) -> None:
            if not messagebox.askyesno(
                "Встановлення залежностей",
                f"Оновити пакети у середовищі?\n\n{self.python}",
            ):
                return
            self.start_process(
                [
                    str(self.python),
                    "-u",
                    "-m",
                    "pip",
                    "install",
                    "-r",
                    str(PROJECT_ROOT / "requirements.txt"),
                ],
                "Встановлення залежностей",
            )

        def start_process(
            self,
            command: Sequence[str],
            label: str,
            extra_environment: dict[str, str] | None = None,
            cwd: Path = PROJECT_ROOT,
            after_success: tuple[list[str], str, dict[str, str]] | None = None,
        ) -> None:
            with self.process_lock:
                if self.busy:
                    messagebox.showwarning(
                        "Процес уже працює",
                        "Спочатку зупиніть або дочекайтеся поточного процесу.",
                    )
                    return
                self.busy = True
                self.cancel_start = False
                self.after_success = after_success
            self.stopping = False
            self.stop_button.configure(state="normal")
            self.status.set(f"Виконується: {label}")
            self._append_log(f"\n$ {command_text(command)}\n\n")
            threading.Thread(
                target=self._process_worker,
                args=(list(command), label, extra_environment or {}, cwd),
                daemon=True,
            ).start()

        def _process_worker(
            self,
            command: list[str],
            label: str,
            extra_environment: dict[str, str],
            cwd: Path,
        ) -> None:
            environment = project_environment(self.python)
            environment.update(extra_environment)
            popen_options: dict[str, object] = {}
            if os.name == "nt":
                popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                popen_options["start_new_session"] = True
            try:
                with self.process_lock:
                    if self.cancel_start:
                        self.events.put(("done", (label, 130)))
                        return
                    process = subprocess.Popen(
                        command,
                        cwd=cwd,
                        env=environment,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        bufsize=1,
                        **popen_options,
                    )
                    self.process = process
                assert process.stdout is not None
                for line in process.stdout:
                    self.events.put(("line", line))
                return_code = process.wait()
                self.events.put(("done", (label, return_code)))
            except OSError as error:
                self.events.put(("error", (label, str(error))))

        def stop_process(self) -> None:
            with self.process_lock:
                process = self.process
                if not self.busy:
                    return
                self.stopping = True
                self.after_success = None
                if process is None:
                    self.cancel_start = True
            self.status.set("Зупинка процесу…")
            self._append_log("\n[manager] Надіслано команду зупинки.\n")
            if process is None or process.poll() is not None:
                return
            try:
                self._terminate_process_tree(process, force=False)
            except (OSError, ProcessLookupError):
                pass
            threading.Thread(
                target=self._force_stop_after_timeout,
                args=(process,),
                daemon=True,
            ).start()

        @staticmethod
        def _terminate_process_tree(
            process: subprocess.Popen[str], force: bool
        ) -> None:
            if os.name == "nt":
                command = ["taskkill", "/PID", str(process.pid), "/T"]
                if force:
                    command.append("/F")
                subprocess.run(
                    command,
                    capture_output=True,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    check=False,
                )
            else:
                os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)

        @staticmethod
        def _force_stop_after_timeout(process: subprocess.Popen[str]) -> None:
            try:
                process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                try:
                    FlowMindManager._terminate_process_tree(process, force=True)
                except (OSError, ProcessLookupError):
                    pass

        def _drain_events(self) -> None:
            try:
                while True:
                    event, payload = self.events.get_nowait()
                    if event == "line":
                        self._append_log(str(payload))
                    elif event == "done":
                        label, return_code = payload  # type: ignore[misc]
                        self._process_finished(str(label), int(return_code))
                    elif event == "error":
                        label, error = payload  # type: ignore[misc]
                        self._process_error(str(label), str(error))
            except queue.Empty:
                pass
            self.root.after(100, self._drain_events)

        def _process_finished(self, label: str, return_code: int) -> None:
            with self.process_lock:
                self.process = None
                self.busy = False
                follow_up = (
                    self.after_success
                    if return_code == 0 and not self.stopping
                    else None
                )
                self.after_success = None
            self.stop_button.configure(state="disabled")
            if self.stopping:
                self.status.set(f"Зупинено: {label}")
                self._append_log(f"\n[manager] {label}: зупинено.\n")
            elif return_code == 0:
                self.status.set(f"Завершено: {label}")
                self._append_log(f"\n[manager] {label}: успішно завершено.\n")
            else:
                self.status.set(f"Помилка: {label} (код {return_code})")
                self._append_log(
                    f"\n[manager] {label}: процес завершився з кодом {return_code}.\n"
                )
            if follow_up is not None:
                command, next_label, environment = follow_up
                self._append_log(
                    f"\n[manager] Запуск наступного кроку: {next_label}.\n"
                )
                self.start_process(command, next_label, environment)

        def _process_error(self, label: str, error: str) -> None:
            with self.process_lock:
                self.process = None
                self.busy = False
                self.after_success = None
            self.stop_button.configure(state="disabled")
            self.status.set(f"Не вдалося запустити: {label}")
            self._append_log(f"\n[manager] Помилка запуску: {error}\n")
            messagebox.showerror("Помилка запуску", error)

        def _append_log(self, text: str) -> None:
            self.log.configure(state="normal")
            self.log.insert("end", text)
            self.log.see("end")
            self.log.configure(state="disabled")

        def clear_log(self) -> None:
            self.log.configure(state="normal")
            self.log.delete("1.0", "end")
            self.log.configure(state="disabled")

        def on_close(self) -> None:
            with self.process_lock:
                running = self.busy
            if running and not messagebox.askyesno(
                "Процес працює",
                "Зупинити запущений процес і закрити менеджер?",
            ):
                return
            if running:
                self.stop_process()
            self.root.destroy()

    try:
        root = tk.Tk()
    except tk.TclError as error:
        print(f"Could not open the graphical interface: {error}", file=sys.stderr)
        return False
    FlowMindManager(root)
    root.mainloop()
    return True


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Standalone FlowMind project manager")
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="check the project environment without opening the GUI",
    )
    args = parser.parse_args(argv)
    if args.diagnose:
        return diagnose()
    try:
        import tkinter  # noqa: F401
    except ModuleNotFoundError:
        python = project_python()
        current = os.path.normcase(os.path.abspath(sys.executable))
        selected = os.path.normcase(os.path.abspath(python))
        if selected != current:
            forwarded = list(argv) if argv is not None else sys.argv[1:]
            os.execv(
                os.fspath(python),
                [os.fspath(python), os.fspath(Path(__file__).resolve()), *forwarded],
            )
        print(
            "Tkinter is not available. Install the Python Tk package "
            "(for example: python3-tk) and try again.",
            file=sys.stderr,
        )
        return 1
    return 0 if run_gui() else 1


if __name__ == "__main__":
    raise SystemExit(main())
