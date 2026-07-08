from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence
from xml.etree import ElementTree as ET


OSM_SUFFIXES = (".osm", ".osm.xml", ".osm.gz", ".osm.xml.gz")


class BuildError(RuntimeError):
    """Raised when an OSM map cannot be converted into a SUMO scenario."""


@dataclass(frozen=True)
class BuildConfig:
    source_osm: Path
    output_dir: Path
    prefix: str = "osm"
    duration: int = 1_800
    insertion_rate: int = 1_200
    seed: int = 42
    min_distance: float = 300.0
    fringe_factor: float = 5.0
    polygons: bool = True
    traffic: bool = True
    force: bool = False

    @property
    def network_path(self) -> Path:
        return self.output_dir / f"{self.prefix}.net.xml.gz"

    @property
    def polygons_path(self) -> Path:
        return self.output_dir / f"{self.prefix}.poly.xml.gz"

    @property
    def trips_path(self) -> Path:
        return self.output_dir / f"{self.prefix}.passenger.trips.xml"

    @property
    def routes_path(self) -> Path:
        return self.output_dir / f"{self.prefix}.passenger.rou.xml"

    @property
    def sumo_config_path(self) -> Path:
        return self.output_dir / f"{self.prefix}.sumocfg"

    @property
    def manifest_path(self) -> Path:
        return self.output_dir / f"{self.prefix}.build.json"

    def output_paths(self) -> tuple[Path, ...]:
        paths = [self.network_path, self.sumo_config_path, self.manifest_path]
        if self.polygons:
            paths.append(self.polygons_path)
        if self.traffic:
            paths.extend((self.trips_path, self.routes_path))
        return tuple(paths)


@dataclass(frozen=True)
class SumoTools:
    home: Path
    netconvert: Path
    polyconvert: Path
    sumo: Path
    random_trips: Path
    net_typemap: Path
    urban_typemap: Path
    polygon_typemap: Path


def is_osm_file(path: Path) -> bool:
    name = path.name.lower()
    return any(name.endswith(suffix) for suffix in OSM_SUFFIXES)


def positive_int(value: str) -> int:
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("значення має бути більше нуля")
    return result


def positive_float(value: str) -> float:
    result = float(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("значення має бути більше нуля")
    return result


def resolve_sumo_home(explicit_home: Path | None = None) -> Path:
    if explicit_home is not None:
        home = explicit_home.expanduser().resolve()
    elif os.environ.get("SUMO_HOME"):
        home = Path(os.environ["SUMO_HOME"]).expanduser().resolve()
    else:
        try:
            import sumo
        except ImportError as error:
            raise BuildError(
                "SUMO не знайдено. Активуйте .venv, встановіть requirements.txt "
                "або передайте --sumo-home."
            ) from error
        home = Path(sumo.SUMO_HOME).resolve()

    if not home.is_dir():
        raise BuildError(f"SUMO_HOME не існує або не є директорією: {home}")
    return home


def _find_executable(name: str, sumo_home: Path) -> Path:
    candidates = (
        sumo_home / "bin" / name,
        sumo_home / "bin" / f"{name}.exe",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    discovered = shutil.which(name)
    if discovered:
        return Path(discovered).resolve()
    raise BuildError(f"Не знайдено SUMO-інструмент: {name}")


def discover_sumo_tools(explicit_home: Path | None = None) -> SumoTools:
    home = resolve_sumo_home(explicit_home)
    tools = SumoTools(
        home=home,
        netconvert=_find_executable("netconvert", home),
        polyconvert=_find_executable("polyconvert", home),
        sumo=_find_executable("sumo", home),
        random_trips=home / "tools" / "randomTrips.py",
        net_typemap=home / "data" / "typemap" / "osmNetconvert.typ.xml",
        urban_typemap=home
        / "data"
        / "typemap"
        / "osmNetconvertUrbanDe.typ.xml",
        polygon_typemap=home / "data" / "typemap" / "osmPolyconvert.typ.xml",
    )
    required_files = (
        tools.random_trips,
        tools.net_typemap,
        tools.urban_typemap,
        tools.polygon_typemap,
    )
    missing = [path for path in required_files if not path.is_file()]
    if missing:
        raise BuildError(
            "У SUMO_HOME відсутні необхідні файли: "
            + ", ".join(str(path) for path in missing)
        )
    return tools


def validate_config(config: BuildConfig) -> None:
    if not config.source_osm.is_file():
        raise BuildError(f"OSM-файл не знайдено: {config.source_osm}")
    if not is_osm_file(config.source_osm):
        raise BuildError(
            "Очікується файл .osm, .osm.xml, .osm.gz або .osm.xml.gz: "
            f"{config.source_osm}"
        )
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", config.prefix):
        raise BuildError(
            "Prefix може містити лише латинські літери, цифри, '.', '_' і '-'."
        )
    if config.duration <= 0 or config.insertion_rate <= 0:
        raise BuildError("Duration та insertion rate мають бути більше нуля.")
    if config.min_distance <= 0 or config.fringe_factor <= 0:
        raise BuildError("Min distance та fringe factor мають бути більше нуля.")


def ensure_outputs_available(config: BuildConfig) -> None:
    existing = [path for path in config.output_paths() if path.exists()]
    if existing and not config.force:
        formatted = "\n".join(f"  - {path}" for path in existing)
        raise BuildError(
            "Вихідні файли вже існують:\n"
            f"{formatted}\n"
            "Використайте іншу папку/prefix або явно передайте --force."
        )


def build_environment(tools: SumoTools) -> dict[str, str]:
    environment = os.environ.copy()
    environment["SUMO_HOME"] = str(tools.home)
    environment["PATH"] = os.pathsep.join(
        (str(tools.home / "bin"), environment.get("PATH", ""))
    )
    return environment


def netconvert_command(config: BuildConfig, tools: SumoTools) -> list[str]:
    return [
        str(tools.netconvert),
        "--osm-files",
        str(config.source_osm),
        "--output-file",
        str(config.network_path),
        "--type-files",
        f"{tools.net_typemap},{tools.urban_typemap}",
        "--geometry.remove",
        "--roundabouts.guess",
        "--ramps.guess",
        "--junctions.join",
        "--tls.guess-signals",
        "--tls.discard-simple",
        "--tls.join",
        "--tls.default-type",
        "actuated",
        "--osm.turn-lanes",
        "--remove-edges.isolated",
        "--output.street-names",
        "--output.original-names",
        "--verbose",
    ]


def polyconvert_command(config: BuildConfig, tools: SumoTools) -> list[str]:
    return [
        str(tools.polyconvert),
        "--net-file",
        str(config.network_path),
        "--osm-files",
        str(config.source_osm),
        "--type-file",
        str(tools.polygon_typemap),
        "--output-file",
        str(config.polygons_path),
        "--osm.keep-full-type",
        "--verbose",
    ]


def random_trips_command(config: BuildConfig, tools: SumoTools) -> list[str]:
    return [
        sys.executable,
        str(tools.random_trips),
        "--net-file",
        str(config.network_path),
        "--output-trip-file",
        str(config.trips_path),
        "--route-file",
        str(config.routes_path),
        "--begin",
        "0",
        "--end",
        str(config.duration),
        "--insertion-rate",
        str(config.insertion_rate),
        "--vehicle-class",
        "passenger",
        "--edge-permission",
        "passenger",
        "--min-distance",
        str(config.min_distance),
        "--fringe-factor",
        str(config.fringe_factor),
        "--remove-loops",
        "--validate",
        "--seed",
        str(config.seed),
        "--trip-attributes",
        'departLane="best" departSpeed="max"',
    ]


def run_command(
    label: str,
    command: Sequence[str],
    environment: dict[str, str],
) -> None:
    print(f"\n[{label}]", flush=True)
    print(shlex.join(command), flush=True)
    try:
        subprocess.run(command, env=environment, check=True)
    except FileNotFoundError as error:
        raise BuildError(f"Не вдалося запустити {command[0]}") from error
    except subprocess.CalledProcessError as error:
        raise BuildError(
            f"{label} завершився з кодом {error.returncode}."
        ) from error


def write_sumo_config(config: BuildConfig) -> None:
    root = ET.Element(
        "sumoConfiguration",
        {
            "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
            "xsi:noNamespaceSchemaLocation": (
                "http://sumo.dlr.de/xsd/sumoConfiguration.xsd"
            ),
        },
    )
    inputs = ET.SubElement(root, "input")
    ET.SubElement(inputs, "net-file", {"value": config.network_path.name})
    if config.traffic:
        ET.SubElement(inputs, "route-files", {"value": config.routes_path.name})
    if config.polygons:
        ET.SubElement(
            inputs,
            "additional-files",
            {"value": config.polygons_path.name},
        )

    time = ET.SubElement(root, "time")
    ET.SubElement(time, "begin", {"value": "0"})
    ET.SubElement(time, "end", {"value": str(config.duration)})

    processing = ET.SubElement(root, "processing")
    ET.SubElement(processing, "ignore-route-errors", {"value": "false"})
    ET.SubElement(processing, "time-to-teleport", {"value": "300"})

    report = ET.SubElement(root, "report")
    ET.SubElement(report, "no-step-log", {"value": "true"})
    ET.SubElement(report, "duration-log.statistics", {"value": "true"})

    ET.indent(root, space="    ")
    ET.ElementTree(root).write(
        config.sumo_config_path,
        encoding="utf-8",
        xml_declaration=True,
    )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def network_stats(network_path: Path) -> dict[str, int | str]:
    try:
        import sumolib

        network = sumolib.net.readNet(
            str(network_path),
            withPrograms=True,
            withConnections=True,
        )
    except Exception as error:  # pragma: no cover - defensive manifest fallback
        return {"read_error": str(error)}
    return {
        "nodes": len(network.getNodes()),
        "edges": len([edge for edge in network.getEdges() if not edge.isSpecial()]),
        "traffic_lights": len(network.getTrafficLights()),
    }


def sumo_version(tools: SumoTools, environment: dict[str, str]) -> str:
    try:
        result = subprocess.run(
            [str(tools.sumo), "--version"],
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    lines = result.stdout.splitlines()
    return lines[0].strip() if lines else "unknown"


def write_manifest(
    config: BuildConfig,
    tools: SumoTools,
    environment: dict[str, str],
) -> None:
    generated_files = []
    for path in config.output_paths():
        if path == config.manifest_path or not path.exists():
            continue
        generated_files.append(
            {
                "name": path.name,
                "size_bytes": path.stat().st_size,
            }
        )

    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_osm": str(config.source_osm),
        "source_sha256": sha256(config.source_osm),
        "sumo_version": sumo_version(tools, environment),
        "parameters": {
            "prefix": config.prefix,
            "duration": config.duration,
            "insertion_rate": config.insertion_rate,
            "seed": config.seed,
            "min_distance": config.min_distance,
            "fringe_factor": config.fringe_factor,
            "polygons": config.polygons,
            "traffic": config.traffic,
        },
        "network": network_stats(config.network_path),
        "generated_files": generated_files,
    }
    config.manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def build_map(config: BuildConfig, tools: SumoTools) -> None:
    validate_config(config)
    config.output_dir.mkdir(parents=True, exist_ok=True)
    ensure_outputs_available(config)
    environment = build_environment(tools)

    run_command(
        "1/3 Створення дорожньої мережі",
        netconvert_command(config, tools),
        environment,
    )
    if config.polygons:
        run_command(
            "2/3 Створення polygons",
            polyconvert_command(config, tools),
            environment,
        )
    else:
        print("\n[2/3 Polygons пропущено]")

    if config.traffic:
        run_command(
            "3/3 Генерація випадкового трафіку",
            random_trips_command(config, tools),
            environment,
        )
    else:
        print("\n[3/3 Генерацію трафіку пропущено]")

    write_sumo_config(config)
    write_manifest(config, tools, environment)

    print("\nSUMO-карту створено.")
    print(f"Конфігурація: {config.sumo_config_path}")
    print(f"Manifest:      {config.manifest_path}")
    print("\nПеревірка:")
    print(
        shlex.join(
            [
                str(tools.sumo),
                "-c",
                str(config.sumo_config_path),
                "--end",
                str(min(config.duration, 120)),
            ]
        )
    )
    print("\nВідкрити в GUI:")
    print(shlex.join(["sumo-gui", "-c", str(config.sumo_config_path)]))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Автоматично перетворити OpenStreetMap .osm на готову "
            "SUMO-карту та сценарій."
        )
    )
    parser.add_argument("osm_file", type=Path, help="шлях до .osm/.osm.xml")
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="папка результату; за замовчуванням папка вхідного файла",
    )
    parser.add_argument("--prefix", default="osm", help="prefix вихідних файлів")
    parser.add_argument("--duration", type=positive_int, default=1_800)
    parser.add_argument(
        "--insertion-rate",
        type=positive_int,
        default=1_200,
        help="приблизна кількість автомобілів на годину",
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--min-distance", type=positive_float, default=300.0)
    parser.add_argument("--fringe-factor", type=positive_float, default=5.0)
    parser.add_argument(
        "--polygons",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="створити будівлі, воду й парки для SUMO GUI",
    )
    parser.add_argument(
        "--traffic",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="створити випадкові trips/routes через randomTrips.py",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="дозволити перезапис наявних вихідних файлів",
    )
    parser.add_argument(
        "--sumo-home",
        type=Path,
        help="явний шлях SUMO_HOME, якщо він не визначається автоматично",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    source_osm = args.osm_file.expanduser().resolve()
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir is not None
        else source_osm.parent
    )
    config = BuildConfig(
        source_osm=source_osm,
        output_dir=output_dir,
        prefix=args.prefix,
        duration=args.duration,
        insertion_rate=args.insertion_rate,
        seed=args.seed,
        min_distance=args.min_distance,
        fringe_factor=args.fringe_factor,
        polygons=args.polygons,
        traffic=args.traffic,
        force=args.force,
    )
    try:
        tools = discover_sumo_tools(args.sumo_home)
        build_map(config, tools)
    except BuildError as error:
        parser.exit(1, f"Помилка: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
