"""
AWS Event Bus — Amazon EventBridge.

Publishes skill invocation events to a custom EventBridge bus.
Results come back through SQS (not through EventBridge).
"""

import json
import logging
from typing import Any

import boto3  # type: ignore[import-untyped]

from agent.interfaces.event_bus import EventBus

logger = logging.getLogger(__name__)


class EventBridgeBus(EventBus):
    """
    Publishes skill.invoke events to EventBridge.

    Unlike DirectBus, this is truly async — publish() returns immediately.
    Results arrive later via SQS, picked up by the SQS poller thread.
    """

    def __init__(self, bus_name: str, region: str = "us-east-1"):
        self.bus_name = bus_name
        self.client = boto3.client("events", region_name=region)

    async def publish(
        self,
        source: str,
        detail_type: str,
        detail: dict[str, Any],
    ) -> None:
        """Put an event to EventBridge. Returns immediately (async invocation)."""
        logger.info(
            f"EventBridgeBus: publishing {detail_type} "
            f"(skill={detail.get('skill_name')}, request={detail.get('request_id', '')[:8]})"
        )

        self.client.put_events(
            Entries=[
                {
                    "Source": source,
                    "DetailType": detail_type,
                    "Detail": json.dumps(detail),
                    "EventBusName": self.bus_name,
                }
            ]
        )

        logger.info(f"EventBridgeBus: event published to {self.bus_name}")
