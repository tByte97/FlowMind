from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class DecisionEvent:
    """A judge-friendly explanation of an action taken during a simulation."""

    time: float
    category: str
    title: str
    detail: str
    level: str = "info"
    tls_id: str | None = None

    def as_payload(self) -> dict[str, object]:
        return asdict(self)


def merged_decision_log(
    *event_groups: object,
    limit: int = 120,
) -> list[dict[str, object]]:
    events = [
        event
        for group in event_groups
        for event in list(group or ())
        if isinstance(event, DecisionEvent)
    ]
    category_priority = {
        "controller": 0,
        "route": 1,
        "corridor": 2,
        "system": 3,
    }
    events.sort(
        key=lambda event: (
            event.time,
            category_priority.get(event.category, 0),
        )
    )
    return [event.as_payload() for event in events[-limit:]]
