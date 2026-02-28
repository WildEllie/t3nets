"""
WebSocket Connection Manager — API Gateway WebSocket push via Management API.

Thread-safe in-memory registry of WebSocket connection IDs. Same pattern as
SSEConnectionManager but tracks API Gateway connection IDs instead of wfile
objects, and pushes via ApiGatewayManagementApi.post_to_connection().

Part of the AWS adapter layer — uses boto3 for the Management API.
"""

import json
import logging
import os
import threading

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


class WebSocketConnectionManager:
    """Thread-safe in-memory registry of WebSocket connection IDs.

    Mirrors SSEConnectionManager.send_event() so AsyncResultRouter doesn't
    need to know which transport is in use.
    """

    def __init__(self, management_endpoint: str | None = None):
        self._lock = threading.Lock()
        # user_key → [connection_id, ...]
        self._connections: dict[str, list[str]] = {}
        # connection_id → user_key (reverse map for fast $disconnect)
        self._reverse: dict[str, str] = {}

        endpoint = management_endpoint or os.environ.get("WS_MANAGEMENT_ENDPOINT", "")
        self._mgmt_client = (
            boto3.client("apigatewaymanagementapi", endpoint_url=endpoint)
            if endpoint
            else None
        )

    def register(self, user_key: str, connection_id: str) -> None:
        """Register a WebSocket connection for a user."""
        with self._lock:
            if user_key not in self._connections:
                self._connections[user_key] = []
            self._connections[user_key].append(connection_id)
            self._reverse[connection_id] = user_key
            logger.info(
                f"WS: registered {connection_id[:12]} for {user_key} "
                f"(total: {len(self._connections[user_key])})"
            )

    def unregister_by_connection_id(self, connection_id: str) -> None:
        """Remove a WebSocket connection by its connection ID."""
        with self._lock:
            user_key = self._reverse.pop(connection_id, None)
            if user_key and user_key in self._connections:
                try:
                    self._connections[user_key].remove(connection_id)
                except ValueError:
                    pass
                if not self._connections[user_key]:
                    del self._connections[user_key]
                logger.info(f"WS: unregistered {connection_id[:12]} for {user_key}")

    def get_connections(self, user_key: str) -> list[str]:
        """Get all connection IDs for a user."""
        with self._lock:
            return list(self._connections.get(user_key, []))

    def send_event(self, user_key: str, event_type: str, data: dict) -> int:
        """Push event via ApiGatewayManagementApi. Returns delivery count.

        Matches SSEConnectionManager.send_event() signature so result_router
        doesn't need to know which transport is in use.
        """
        if not self._mgmt_client:
            logger.warning("WS: no management client configured, cannot push")
            return 0

        connections = self.get_connections(user_key)
        if not connections:
            logger.debug(f"WS: no connections for {user_key}")
            return 0

        payload = json.dumps({"event": event_type, **data}).encode()
        sent = 0
        gone: list[str] = []

        for conn_id in connections:
            try:
                self._mgmt_client.post_to_connection(
                    ConnectionId=conn_id,
                    Data=payload,
                )
                sent += 1
            except ClientError as e:
                error_code = e.response.get("Error", {}).get("Code", "")
                if error_code == "GoneException":
                    gone.append(conn_id)
                    logger.debug(f"WS: stale connection {conn_id[:12]}, removing")
                else:
                    logger.error(f"WS: post_to_connection failed for {conn_id[:12]}: {e}")

        for conn_id in gone:
            self.unregister_by_connection_id(conn_id)

        return sent

    @property
    def connection_count(self) -> int:
        with self._lock:
            return sum(len(c) for c in self._connections.values())
