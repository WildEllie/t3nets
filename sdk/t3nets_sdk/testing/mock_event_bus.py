"""
MockEventBus — in-memory EventBus that records every published event.

Tests inspect `.events` to assert what the system under test published, and
can use `.find()` to filter by source / detail_type.
"""

import copy
from dataclasses import dataclass, field
from typing import Any, Optional

from t3nets_sdk.interfaces.event_bus import EventBus


@dataclass
class PublishedEvent:
    """A single event captured by MockEventBus."""

    source: str
    detail_type: str
    detail: dict[str, Any] = field(default_factory=dict)


class MockEventBus(EventBus):
    """In-memory EventBus. Captures every publish for assertions in tests."""

    def __init__(self) -> None:
        self.events: list[PublishedEvent] = []

    async def publish(
        self,
        source: str,
        detail_type: str,
        detail: dict[str, Any],
    ) -> None:
        # Defensive copy so later mutations of the input dict don't change the record.
        self.events.append(
            PublishedEvent(
                source=source,
                detail_type=detail_type,
                detail=copy.deepcopy(detail),
            )
        )

    # --- Test helpers ---

    def find(
        self,
        source: Optional[str] = None,
        detail_type: Optional[str] = None,
    ) -> list[PublishedEvent]:
        """Return captured events matching the given filters."""
        return [
            e
            for e in self.events
            if (source is None or e.source == source)
            and (detail_type is None or e.detail_type == detail_type)
        ]

    def last(self) -> Optional[PublishedEvent]:
        """Most recently published event, or None if nothing has been published."""
        return self.events[-1] if self.events else None

    def clear(self) -> None:
        """Drop all captured events."""
        self.events.clear()

    def __len__(self) -> int:
        return len(self.events)
