from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .tls_safety import TlsSafetyCatalog, TlsSafetyReport, catalog_payload


def write_tls_safety_startup_audit(
    results_dir: Path,
    mode: str,
    catalog: TlsSafetyCatalog,
    report: TlsSafetyReport,
) -> Path:
    """Persist both the sparse conflict matrix and validation outcome."""

    results_dir.mkdir(parents=True, exist_ok=True)
    output_path = results_dir / f"{mode}_tls_safety_startup.json"
    payload = {
        "schema_version": 1,
        "audited_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "report": report.as_payload(),
        "catalog": catalog_payload(catalog),
    }
    temporary_path = output_path.with_suffix(".json.tmp")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_path.replace(output_path)
    return output_path
