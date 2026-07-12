from __future__ import annotations

import argparse
import json
import math
import os
import sys
import warnings
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flowmind.config import PROJECT_ROOT, ControlConfig
from flowmind.queue_forecast import (
    dataset_sha256,
    feature_schema_sha256,
    file_sha256,
)


os.environ.setdefault("MPLCONFIGDIR", "/tmp/flowmind_matplotlib")
warnings.filterwarnings(
    "ignore",
    message="X does not have valid feature names.*",
    category=UserWarning,
)


NUMERIC_FEATURES = (
    "sample_interval",
    "decision_interval",
    "sensor_range_meters",
    "min_green",
    "max_green",
    "use_default_phase_timing",
    "default_green_extension",
    "blocked_occupancy",
    "downstream_weight",
    "area_pressure_weight",
    "queue_forecast_weight",
    "empty_approach_penalty",
    "empty_phase_penalty",
    "congested_queue_threshold",
    "congested_occupancy_threshold",
    "congested_approach_bonus",
    "demand_timer_seconds",
    "demand_wait_weight",
    "max_demand_wait_bonus",
    "hysteresis",
    "priority_distance",
    "max_priority_override",
    "clearance_seconds",
    "time",
    "signal_index",
    "is_green",
    "current_phase",
    "phase_elapsed",
    "phase_count",
    "incoming_queue",
    "incoming_vehicle_count",
    "incoming_occupancy",
    "incoming_mean_speed",
    "incoming_free_slots",
    "outgoing_queue",
    "outgoing_vehicle_count",
    "outgoing_occupancy",
    "outgoing_mean_speed",
    "outgoing_free_slots",
    "downstream_blocked",
)

CATEGORICAL_FEATURES = (
    "mode",
    "tls_id",
    "movement_id",
    "incoming_lane",
    "outgoing_lane",
    "signal_state",
    "phase_state",
)

DEFAULT_CONTROL = ControlConfig()
OPTIONAL_NUMERIC_DEFAULTS = {
    "sample_interval": 5,
    "decision_interval": DEFAULT_CONTROL.decision_interval,
    "sensor_range_meters": DEFAULT_CONTROL.sensor_range_meters,
    "min_green": DEFAULT_CONTROL.min_green,
    "max_green": DEFAULT_CONTROL.max_green,
    "use_default_phase_timing": int(DEFAULT_CONTROL.use_default_phase_timing),
    "default_green_extension": DEFAULT_CONTROL.default_green_extension,
    "blocked_occupancy": DEFAULT_CONTROL.blocked_occupancy,
    "downstream_weight": DEFAULT_CONTROL.downstream_weight,
    "area_pressure_weight": DEFAULT_CONTROL.area_pressure_weight,
    "queue_forecast_weight": DEFAULT_CONTROL.queue_forecast_weight,
    "empty_approach_penalty": DEFAULT_CONTROL.empty_approach_penalty,
    "empty_phase_penalty": DEFAULT_CONTROL.empty_phase_penalty,
    "congested_queue_threshold": DEFAULT_CONTROL.congested_queue_threshold,
    "congested_occupancy_threshold": DEFAULT_CONTROL.congested_occupancy_threshold,
    "congested_approach_bonus": DEFAULT_CONTROL.congested_approach_bonus,
    "demand_timer_seconds": DEFAULT_CONTROL.demand_timer_seconds,
    "demand_wait_weight": DEFAULT_CONTROL.demand_wait_weight,
    "max_demand_wait_bonus": DEFAULT_CONTROL.max_demand_wait_bonus,
    "hysteresis": DEFAULT_CONTROL.hysteresis,
    "priority_distance": DEFAULT_CONTROL.priority_distance,
    "max_priority_override": DEFAULT_CONTROL.max_priority_override,
    "clearance_seconds": DEFAULT_CONTROL.clearance_seconds,
}

DROP_COLUMNS = (
    "run_id",
    "scenario",
    "seed",
    "duration",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Train a queue forecast model from FlowMind dataset CSV files."
    )
    parser.add_argument(
        "--dataset-dir",
        type=Path,
        default=PROJECT_ROOT / "results" / "dataset",
    )
    parser.add_argument(
        "--samples-dir",
        type=Path,
        help="Defaults to <dataset-dir>/samples.",
    )
    parser.add_argument(
        "--target",
        default="target_incoming_queue_60s",
        help="Target column to predict.",
    )
    parser.add_argument(
        "--model-type",
        choices=("lightgbm", "xgboost"),
        default="lightgbm",
    )
    parser.add_argument(
        "--modes",
        nargs="+",
        choices=("static_fixed", "sumo_actuated", "local", "flowmind", "fixed"),
        default=("static_fixed", "sumo_actuated", "local", "flowmind"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "models" / "queue_lgbm_60s.joblib",
    )
    parser.add_argument(
        "--metadata-output",
        type=Path,
        help="Defaults to <output stem>_metadata.json.",
    )
    parser.add_argument(
        "--network",
        type=Path,
        default=PROJECT_ROOT / "simulation" / "rivne_area" / "osm.net.xml.gz",
        help="Network used by the training/demo scenario (stored by SHA-256).",
    )
    parser.add_argument("--max-files", type=int)
    parser.add_argument("--max-rows", type=int)
    parser.add_argument(
        "--rows-per-file",
        type=int,
        default=2000,
        help=(
            "Sample this many rows from each CSV before concatenation. "
            "Use --full-dataset to load every row."
        ),
    )
    parser.add_argument(
        "--full-dataset",
        action="store_true",
        help="Load all rows from all CSV files. Requires a lot of RAM.",
    )
    parser.add_argument("--random-state", type=int, default=42)
    parser.add_argument("--train-ratio", type=float, default=0.7)
    parser.add_argument("--valid-ratio", type=float, default=0.15)
    parser.add_argument("--n-estimators", type=int, default=700)
    parser.add_argument("--learning-rate", type=float, default=0.05)
    parser.add_argument("--num-leaves", type=int, default=64)
    parser.add_argument("--max-depth", type=int, default=-1)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Load and split data, but do not fit or write a model.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    dependencies = import_training_dependencies()

    samples_dir = args.samples_dir or args.dataset_dir / "samples"
    sample_paths = discover_sample_paths(samples_dir, args.modes, args.max_files)
    if not sample_paths:
        raise SystemExit(f"No sample CSV files found in {samples_dir}")

    pd = dependencies["pd"]
    rows_per_file = None if args.full_dataset else args.rows_per_file
    df = load_dataset(
        pd,
        sample_paths,
        args.target,
        args.max_rows,
        rows_per_file,
        args.random_state,
    )
    train_seeds, valid_seeds, test_seeds = split_seeds(
        sorted(int(seed) for seed in df["seed"].dropna().unique()),
        args.train_ratio,
        args.valid_ratio,
    )

    train_df = df[df["seed"].isin(train_seeds)].copy()
    valid_df = df[df["seed"].isin(valid_seeds)].copy()
    test_df = df[df["seed"].isin(test_seeds)].copy()
    if train_df.empty or valid_df.empty or test_df.empty:
        raise SystemExit(
            "Train/validation/test split produced an empty partition. "
            "Collect more seeds or adjust --train-ratio/--valid-ratio."
        )

    feature_columns = validate_feature_columns(df, args.target)
    print_dataset_report(args, sample_paths, df, train_df, valid_df, test_df)

    if args.dry_run:
        return

    pipeline = build_pipeline(
        dependencies,
        args.model_type,
        args.n_estimators,
        args.learning_rate,
        args.num_leaves,
        args.max_depth,
        args.jobs,
        args.random_state,
    )
    x_train, y_train = split_xy(train_df, feature_columns, args.target)
    x_valid, y_valid = split_xy(valid_df, feature_columns, args.target)
    x_test, y_test = split_xy(test_df, feature_columns, args.target)

    print("Training model...")
    pipeline.fit(x_train, y_train)
    metrics = {
        "train": evaluate(dependencies, pipeline, x_train, y_train),
        "validation": evaluate(dependencies, pipeline, x_valid, y_valid),
        "test": evaluate(dependencies, pipeline, x_test, y_test),
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    metadata_path = args.metadata_output or args.output.with_name(
        f"{args.output.stem}_metadata.json"
    )
    metadata = {
        "artifact_format_version": 2,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "model_type": args.model_type,
        "forecast_contract": "current_policy",
        "target": args.target,
        "dataset_dir": str(args.dataset_dir),
        "dataset_sha256": dataset_sha256(sample_paths),
        "dataset_files": [path.name for path in sample_paths],
        "network_path": str(args.network),
        "network_sha256": file_sha256(args.network),
        "sample_file_count": len(sample_paths),
        "row_count": int(len(df)),
        "rows_per_file": rows_per_file,
        "max_rows": args.max_rows,
        "feature_columns": feature_columns,
        "numeric_features": list(NUMERIC_FEATURES),
        "categorical_features": list(CATEGORICAL_FEATURES),
        "feature_schema_sha256": feature_schema_sha256(
            feature_columns,
            NUMERIC_FEATURES,
            CATEGORICAL_FEATURES,
            "current_policy",
        ),
        "feature_ranges": numeric_feature_ranges(train_df),
        "known_tls_ids": sorted(
            str(value) for value in train_df["tls_id"].dropna().unique()
        ),
        "known_lane_ids": sorted(
            {
                str(value)
                for column in ("incoming_lane", "outgoing_lane")
                for value in train_df[column].dropna().unique()
            }
        ),
        "training_modes": sorted(
            str(value) for value in train_df["mode"].dropna().unique()
        ),
        "dropped_columns": list(DROP_COLUMNS),
        "train_seeds": train_seeds,
        "validation_seeds": valid_seeds,
        "test_seeds": test_seeds,
        "metrics": metrics,
        "top_feature_importances": feature_importances(pipeline, limit=50),
    }
    artifact = {
        "pipeline": pipeline,
        "metadata": metadata,
    }

    dependencies["joblib"].dump(artifact, args.output)
    metadata["artifact_sha256"] = file_sha256(args.output)
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Model saved:    {args.output}")
    print(f"Metadata saved: {metadata_path}")
    print(json.dumps(metrics, ensure_ascii=False, indent=2))


def import_training_dependencies() -> dict[str, Any]:
    missing: list[str] = []
    try:
        import pandas as pd
    except ImportError:
        missing.append("pandas")
        pd = None
    try:
        import joblib
    except ImportError:
        missing.append("joblib")
        joblib = None
    try:
        from sklearn.compose import ColumnTransformer
        from sklearn.impute import SimpleImputer
        from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import OneHotEncoder
    except ImportError:
        missing.append("scikit-learn")
        ColumnTransformer = None
        SimpleImputer = None
        mean_absolute_error = None
        mean_squared_error = None
        OneHotEncoder = None
        Pipeline = None
        r2_score = None

    if missing:
        raise SystemExit(
            "Missing ML dependencies: "
            + ", ".join(sorted(set(missing)))
            + "\nInstall them with:\n"
            + "  pip install scikit-learn lightgbm joblib"
        )

    return {
        "pd": pd,
        "joblib": joblib,
        "ColumnTransformer": ColumnTransformer,
        "SimpleImputer": SimpleImputer,
        "mean_absolute_error": mean_absolute_error,
        "mean_squared_error": mean_squared_error,
        "OneHotEncoder": OneHotEncoder,
        "Pipeline": Pipeline,
        "r2_score": r2_score,
    }


def discover_sample_paths(
    samples_dir: Path,
    modes: tuple[str, ...] | list[str],
    max_files: int | None,
) -> list[Path]:
    mode_tokens = {
        token
        for mode in modes
        for token in (
            ("_static_fixed_", "_fixed_")
            if mode in {"static_fixed", "fixed"}
            else (f"_{mode}_",)
        )
    }
    paths = [
        path
        for path in sorted(samples_dir.glob("*.csv"))
        if any(token in path.name for token in mode_tokens)
    ]
    if max_files is None or max_files >= len(paths):
        return paths
    if max_files <= 0:
        return []
    # Pick evenly across the sorted mode/run list instead of taking a prefix,
    # which would silently train on only the alphabetically first mode.
    return [
        paths[int(index * len(paths) / max_files)]
        for index in range(max_files)
    ]


def load_dataset(
    pd: Any,
    sample_paths: list[Path],
    target: str,
    max_rows: int | None,
    rows_per_file: int | None,
    random_state: int,
) -> Any:
    if rows_per_file is not None and rows_per_file <= 0:
        raise SystemExit("--rows-per-file must be positive unless --full-dataset is used.")

    frames = []
    use_columns = set(NUMERIC_FEATURES) | set(CATEGORICAL_FEATURES)
    use_columns |= {target, "seed"}
    for index, path in enumerate(sample_paths, start=1):
        frame = pd.read_csv(
            path,
            usecols=lambda column: column in use_columns,
            low_memory=False,
        )
        if target not in frame.columns:
            raise SystemExit(f"Target column not found in {path}: {target}")
        frame = add_optional_feature_defaults(frame)
        frame[target] = pd.to_numeric(frame[target], errors="coerce")
        frame = frame.dropna(subset=[target])
        if rows_per_file is not None and len(frame) > rows_per_file:
            frame = frame.sample(
                n=rows_per_file,
                random_state=random_state + index,
            )
        frames.append(frame)
        if index % 25 == 0 or index == len(sample_paths):
            loaded_rows = sum(len(item) for item in frames)
            print(
                f"Loaded {index}/{len(sample_paths)} files "
                f"({loaded_rows:,} sampled rows)",
                flush=True,
            )
    df = pd.concat(frames, ignore_index=True)
    if max_rows is not None and len(df) > max_rows:
        df = df.sample(n=max_rows, random_state=random_state)
    return df.reset_index(drop=True)


def add_optional_feature_defaults(df: Any) -> Any:
    df = df.copy()
    for column, default in OPTIONAL_NUMERIC_DEFAULTS.items():
        if column not in df.columns:
            df[column] = default
    return df


def split_seeds(
    seeds: list[int],
    train_ratio: float,
    valid_ratio: float,
) -> tuple[list[int], list[int], list[int]]:
    if len(seeds) < 3:
        raise SystemExit("At least 3 unique seeds are required.")
    if not 0 < train_ratio < 1 or not 0 < valid_ratio < 1:
        raise SystemExit("Split ratios must be between 0 and 1.")
    train_count = max(1, int(len(seeds) * train_ratio))
    valid_count = max(1, int(len(seeds) * valid_ratio))
    if train_count + valid_count >= len(seeds):
        train_count = max(1, len(seeds) - 2)
        valid_count = 1
    train = seeds[:train_count]
    valid = seeds[train_count : train_count + valid_count]
    test = seeds[train_count + valid_count :]
    return train, valid, test


def validate_feature_columns(df: Any, target: str) -> list[str]:
    missing = [
        column
        for column in list(NUMERIC_FEATURES) + list(CATEGORICAL_FEATURES)
        if column not in df.columns
    ]
    if missing:
        raise SystemExit("Missing feature columns: " + ", ".join(missing))
    return list(NUMERIC_FEATURES) + list(CATEGORICAL_FEATURES)


def build_pipeline(
    dependencies: dict[str, Any],
    model_type: str,
    n_estimators: int,
    learning_rate: float,
    num_leaves: int,
    max_depth: int,
    jobs: int,
    random_state: int,
) -> Any:
    Pipeline = dependencies["Pipeline"]
    ColumnTransformer = dependencies["ColumnTransformer"]
    SimpleImputer = dependencies["SimpleImputer"]

    numeric_pipeline = Pipeline(
        steps=[
            ("imputer", SimpleImputer(strategy="median")),
        ]
    )
    categorical_pipeline = Pipeline(
        steps=[
            ("imputer", SimpleImputer(strategy="constant", fill_value="missing")),
            ("onehot", make_one_hot_encoder(dependencies)),
        ]
    )
    preprocessor = ColumnTransformer(
        transformers=[
            ("num", numeric_pipeline, list(NUMERIC_FEATURES)),
            ("cat", categorical_pipeline, list(CATEGORICAL_FEATURES)),
        ],
        remainder="drop",
    )

    if model_type == "lightgbm":
        try:
            from lightgbm import LGBMRegressor
        except ImportError as exc:
            raise SystemExit(
                "lightgbm is not installed.\n"
                "Install it with:\n"
                "  pip install lightgbm"
            ) from exc
        model = LGBMRegressor(
            objective="regression",
            n_estimators=n_estimators,
            learning_rate=learning_rate,
            num_leaves=num_leaves,
            max_depth=max_depth,
            n_jobs=jobs,
            random_state=random_state,
            verbosity=-1,
        )
    else:
        try:
            from xgboost import XGBRegressor
        except ImportError as exc:
            raise SystemExit(
                "xgboost is not installed.\n"
                "Install it with:\n"
                "  pip install xgboost"
            ) from exc
        model = XGBRegressor(
            objective="reg:squarederror",
            n_estimators=n_estimators,
            learning_rate=learning_rate,
            max_depth=max_depth if max_depth > 0 else 8,
            n_jobs=jobs,
            random_state=random_state,
            tree_method="hist",
        )

    return Pipeline(
        steps=[
            ("preprocess", preprocessor),
            ("model", model),
        ]
    )


def make_one_hot_encoder(dependencies: dict[str, Any]) -> Any:
    OneHotEncoder = dependencies["OneHotEncoder"]
    try:
        return OneHotEncoder(handle_unknown="ignore", sparse_output=True)
    except TypeError:
        return OneHotEncoder(handle_unknown="ignore", sparse=True)


def split_xy(df: Any, feature_columns: list[str], target: str) -> tuple[Any, Any]:
    return df[feature_columns], df[target]


def evaluate(dependencies: dict[str, Any], pipeline: Any, x: Any, y: Any) -> dict[str, float]:
    predictions = pipeline.predict(x)
    mse = dependencies["mean_squared_error"](y, predictions)
    return {
        "mae": round(float(dependencies["mean_absolute_error"](y, predictions)), 5),
        "rmse": round(float(math.sqrt(mse)), 5),
        "r2": round(float(dependencies["r2_score"](y, predictions)), 5),
        "rows": int(len(y)),
    }


def feature_importances(pipeline: Any, limit: int) -> list[dict[str, Any]]:
    model = pipeline.named_steps["model"]
    if not hasattr(model, "feature_importances_"):
        return []
    preprocessor = pipeline.named_steps["preprocess"]
    try:
        names = preprocessor.get_feature_names_out()
    except Exception:
        names = [f"feature_{index}" for index in range(len(model.feature_importances_))]
    pairs = sorted(
        zip(names, model.feature_importances_, strict=False),
        key=lambda item: float(item[1]),
        reverse=True,
    )
    return [
        {"feature": str(name), "importance": float(importance)}
        for name, importance in pairs[:limit]
    ]


def numeric_feature_ranges(df: Any) -> dict[str, list[float]]:
    ranges: dict[str, list[float]] = {}
    for feature in NUMERIC_FEATURES:
        values = df[feature].dropna()
        if values.empty:
            continue
        minimum = float(values.min())
        maximum = float(values.max())
        if math.isfinite(minimum) and math.isfinite(maximum):
            ranges[feature] = [minimum, maximum]
    return ranges


def print_dataset_report(
    args: argparse.Namespace,
    sample_paths: list[Path],
    df: Any,
    train_df: Any,
    valid_df: Any,
    test_df: Any,
) -> None:
    print(f"Samples: {len(sample_paths)} files")
    print(f"Rows after target cleanup: {len(df):,}")
    print(f"Target: {args.target}")
    print(f"Modes: {', '.join(args.modes)}")
    print(
        "Split rows: "
        f"train={len(train_df):,}, "
        f"validation={len(valid_df):,}, "
        f"test={len(test_df):,}"
    )
    print(
        "Split seeds: "
        f"train={seed_range(train_df)}, "
        f"validation={seed_range(valid_df)}, "
        f"test={seed_range(test_df)}"
    )


def seed_range(df: Any) -> str:
    seeds = sorted(int(seed) for seed in df["seed"].dropna().unique())
    if not seeds:
        return "none"
    return f"{seeds[0]}..{seeds[-1]} ({len(seeds)})"


if __name__ == "__main__":
    main()
