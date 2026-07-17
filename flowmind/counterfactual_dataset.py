from __future__ import annotations

import hashlib
import json


COUNTERFACTUAL_DATASET_SCHEMA_VERSION = 5


def counterfactual_dataset_schema_sha256() -> str:
    """Return the contract hash for snapshot-branch action outcomes."""

    payload = {
        "version": COUNTERFACTUAL_DATASET_SCHEMA_VERSION,
        "action_contract": "counterfactual",
        "branching": "sumo_full_reload_with_rng",
        "transition": "fixed_yellow_all_red_clearance_then_candidate_green",
        "targets": (
            "incoming_queue",
            "incoming_occupancy",
            "outgoing_occupancy",
            "downstream_blocked",
            "delta_queue",
            "queue_reduction",
            "future_waiting",
            "discharged_vehicles",
        ),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "COUNTERFACTUAL_DATASET_SCHEMA_VERSION",
    "counterfactual_dataset_schema_sha256",
]
